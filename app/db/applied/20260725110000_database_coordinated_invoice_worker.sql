-- Database-coordinated claiming and recovery for dedicated invoice workers.

alter table public.document_processing_jobs
  add column if not exists claimed_by text,
  add column if not exists claimed_at timestamptz,
  add column if not exists lease_expires_at timestamptz,
  add column if not exists attempt_count integer not null default 0;

create index if not exists document_processing_jobs_claim_idx
  on public.document_processing_jobs(status, job_type, lease_expires_at, priority, created_at);

create or replace function public.claim_next_document_processing_job(
  p_worker_id text,
  p_organisation_id uuid default null,
  p_lease_seconds integer default 1800
)
returns setof public.document_processing_jobs
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.role() is distinct from 'service_role' then
    raise exception 'Only the service role may claim document processing jobs';
  end if;
  if nullif(trim(p_worker_id), '') is null then
    raise exception 'Worker id is required';
  end if;
  if p_lease_seconds < 60 or p_lease_seconds > 7200 then
    raise exception 'Job lease must be between 60 and 7200 seconds';
  end if;

  return query
  with candidate as (
    select j.id
    from public.document_processing_jobs j
    where j.job_type = 'invoice_extract'
      and j.status = 'queued'
      and (p_organisation_id is null or j.organisation_id = p_organisation_id)
    order by j.priority, j.created_at, j.id
    for update skip locked
    limit 1
  )
  update public.document_processing_jobs j
  set status = 'processing',
      current_stage = 'starting',
      claimed_by = p_worker_id,
      claimed_at = now(),
      lease_expires_at = now() + make_interval(secs => p_lease_seconds),
      attempt_count = j.attempt_count + 1,
      started_at = coalesce(j.started_at, now()),
      updated_at = now()
  from candidate
  where j.id = candidate.id
  returning j.*;
end;
$$;

revoke all on function public.claim_next_document_processing_job(text,uuid,integer) from public, anon, authenticated;
grant execute on function public.claim_next_document_processing_job(text,uuid,integer) to service_role;

create or replace function public.maintain_invoice_processing_queue()
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_recovered integer := 0;
  v_expired_failed integer := 0;
  v_orphans_reset integer := 0;
  v_orphans_failed integer := 0;
  v_created integer := 0;
begin
  if auth.role() is distinct from 'service_role' then
    raise exception 'Only the service role may maintain the invoice queue';
  end if;

  if not pg_try_advisory_xact_lock(hashtext('apflow.invoice.queue.maintenance')) then
    return jsonb_build_object('status', 'already_running');
  end if;

  with recovered as (
    update public.document_processing_jobs
    set status = 'queued', current_stage = 'queued', claimed_by = null,
        claimed_at = null, lease_expires_at = null, updated_at = now()
    where job_type = 'invoice_extract'
      and status = 'processing'
      and lease_expires_at is not null
      and lease_expires_at < now()
      and attempt_count < max_retries
    returning invoice_raw_id
  )
  select count(*) into v_recovered from recovered;

  with exhausted as (
    update public.document_processing_jobs
    set status = 'failed', current_stage = 'failed',
        last_error = 'Worker lease expired and maximum attempts were reached',
        failed_at = now(), claimed_by = null, claimed_at = null,
        lease_expires_at = null, updated_at = now()
    where job_type = 'invoice_extract'
      and status = 'processing'
      and lease_expires_at is not null
      and lease_expires_at < now()
      and attempt_count >= max_retries
    returning invoice_raw_id
  )
  update public.invoices_raw r
  set parse_status = 'failed', updated_at = now()
  where r.id in (select invoice_raw_id from exhausted);
  get diagnostics v_expired_failed = row_count;

  update public.invoices_raw r
  set parse_status = 'pending', updated_at = now()
  where r.parse_status = 'completed'
    and not exists (
      select 1 from public.invoices_extracted e where e.invoice_raw_id = r.id
    )
    and (
      select count(*) from public.document_processing_jobs j where j.invoice_raw_id = r.id
    ) < 3;
  get diagnostics v_orphans_reset = row_count;

  update public.invoices_raw r
  set parse_status = 'failed', updated_at = now()
  where r.parse_status = 'completed'
    and not exists (
      select 1 from public.invoices_extracted e where e.invoice_raw_id = r.id
    )
    and (
      select count(*) from public.document_processing_jobs j where j.invoice_raw_id = r.id
    ) >= 3;
  get diagnostics v_orphans_failed = row_count;

  with created as (
    insert into public.document_processing_jobs (
      organisation_id, invoice_raw_id, job_type, status, current_stage,
      priority, created_at, updated_at
    )
    select r.organisation_id, r.id, 'invoice_extract', 'queued', 'queued',
           100, now(), now()
    from public.invoices_raw r
    where r.organisation_id is not null
      and r.parse_status in ('pending', 'queued')
      and not exists (
        select 1 from public.document_processing_jobs j
        where j.invoice_raw_id = r.id
          and j.job_type = 'invoice_extract'
          and j.status in ('queued', 'processing')
      )
    returning invoice_raw_id
  )
  select count(*) into v_created from created;

  update public.invoices_raw r
  set parse_status = 'queued', parse_started_at = null,
      parse_completed_at = null, updated_at = now()
  where r.parse_status in ('pending', 'processing')
    and exists (
      select 1 from public.document_processing_jobs j
      where j.invoice_raw_id = r.id
        and j.job_type = 'invoice_extract'
        and j.status = 'queued'
    );

  return jsonb_build_object(
    'status', 'completed',
    'recovered_expired_jobs', v_recovered,
    'expired_jobs_failed', v_expired_failed,
    'orphaned_completed_reset', v_orphans_reset,
    'orphaned_completed_failed', v_orphans_failed,
    'jobs_created', v_created
  );
end;
$$;

revoke all on function public.maintain_invoice_processing_queue() from public, anon, authenticated;
grant execute on function public.maintain_invoice_processing_queue() to service_role;
