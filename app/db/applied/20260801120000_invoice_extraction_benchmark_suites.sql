begin;

create table if not exists public.invoice_extraction_benchmark_suites (
    id uuid primary key default gen_random_uuid(),
    dataset_split text not null default 'locked'
        check (dataset_split in ('development', 'validation', 'locked')),
    document_kind text
        check (document_kind is null or document_kind in ('invoice', 'credit_note', 'receipt')),
    source_format text
        check (source_format is null or source_format in ('pdf', 'image')),
    status text not null default 'queued'
        check (status in ('queued', 'running', 'completed', 'completed_with_errors', 'cancelled', 'failed')),
    extractor_version text,
    total_documents integer not null default 0,
    processed_documents integer not null default 0,
    quality_passed_documents integer not null default 0,
    failed_documents integer not null default 0,
    current_gold_document_id uuid references public.invoice_extraction_gold_documents(id) on delete set null,
    requested_by uuid references auth.users(id) on delete set null,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.invoice_extraction_benchmark_suite_items (
    id uuid primary key default gen_random_uuid(),
    suite_id uuid not null references public.invoice_extraction_benchmark_suites(id) on delete cascade,
    gold_document_id uuid not null references public.invoice_extraction_gold_documents(id) on delete cascade,
    status text not null default 'queued'
        check (status in ('queued', 'running', 'completed', 'failed', 'skipped')),
    run_id uuid references public.invoice_extraction_benchmark_runs(id) on delete set null,
    accuracy numeric(8,6),
    correction_count integer,
    critical_error_count integer,
    within_two_corrections boolean,
    quality_passed boolean,
    error text,
    claimed_by text,
    claimed_at timestamptz,
    lease_expires_at timestamptz,
    attempt_count integer not null default 0,
    max_attempts integer not null default 2,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (suite_id, gold_document_id)
);

create index if not exists invoice_extraction_benchmark_suites_status_idx
    on public.invoice_extraction_benchmark_suites(status, created_at);
create index if not exists invoice_extraction_benchmark_suite_items_claim_idx
    on public.invoice_extraction_benchmark_suite_items(status, lease_expires_at, created_at);
create index if not exists invoice_extraction_benchmark_suite_items_suite_idx
    on public.invoice_extraction_benchmark_suite_items(suite_id, status, created_at);

alter table public.invoice_extraction_benchmark_suites enable row level security;
alter table public.invoice_extraction_benchmark_suite_items enable row level security;
revoke all on public.invoice_extraction_benchmark_suites from anon, authenticated;
revoke all on public.invoice_extraction_benchmark_suite_items from anon, authenticated;
grant all on public.invoice_extraction_benchmark_suites to service_role;
grant all on public.invoice_extraction_benchmark_suite_items to service_role;

create or replace function public.refresh_invoice_extraction_benchmark_suite()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    target_suite_id uuid;
    item_counts record;
begin
    target_suite_id := case when tg_op = 'DELETE' then old.suite_id else new.suite_id end;
    select
        count(*)::integer as total,
        count(*) filter (where status in ('completed', 'failed', 'skipped'))::integer as processed,
        count(*) filter (where quality_passed is true)::integer as quality_passed,
        count(*) filter (where status = 'failed')::integer as failed,
        count(*) filter (where status = 'running')::integer as running,
        count(*) filter (where status = 'queued')::integer as queued,
        (array_agg(gold_document_id order by started_at desc nulls last)
            filter (where status = 'running'))[1] as current_gold_document_id
    into item_counts
    from public.invoice_extraction_benchmark_suite_items
    where suite_id = target_suite_id;

    update public.invoice_extraction_benchmark_suites suite
    set total_documents = item_counts.total,
        processed_documents = item_counts.processed,
        quality_passed_documents = item_counts.quality_passed,
        failed_documents = item_counts.failed,
        current_gold_document_id = item_counts.current_gold_document_id,
        status = case
            when suite.status = 'cancelled' then 'cancelled'
            when item_counts.total > 0 and item_counts.processed >= item_counts.total
                then case when item_counts.failed > 0 then 'completed_with_errors' else 'completed' end
            when item_counts.running > 0 or item_counts.processed > 0 then 'running'
            else 'queued'
        end,
        started_at = case
            when item_counts.running > 0 or item_counts.processed > 0 then coalesce(suite.started_at, now())
            else suite.started_at
        end,
        completed_at = case
            when suite.status = 'cancelled' then coalesce(suite.completed_at, now())
            when item_counts.total > 0 and item_counts.processed >= item_counts.total then coalesce(suite.completed_at, now())
            else null
        end,
        updated_at = now()
    where suite.id = target_suite_id;

    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end;
$$;

drop trigger if exists refresh_invoice_extraction_benchmark_suite_trigger
    on public.invoice_extraction_benchmark_suite_items;
create trigger refresh_invoice_extraction_benchmark_suite_trigger
after insert or update or delete on public.invoice_extraction_benchmark_suite_items
for each row execute function public.refresh_invoice_extraction_benchmark_suite();

revoke all on function public.refresh_invoice_extraction_benchmark_suite()
    from public, anon, authenticated;

create or replace function public.claim_next_invoice_extraction_benchmark_item(
    p_worker_id text,
    p_lease_seconds integer default 7200
)
returns setof public.invoice_extraction_benchmark_suite_items
language plpgsql
security definer
set search_path = public
as $$
declare
    selected_id uuid;
begin
    if auth.role() is distinct from 'service_role' then
        raise exception 'Only the service role may claim invoice benchmark items';
    end if;
    if nullif(trim(p_worker_id), '') is null then
        raise exception 'Worker id is required';
    end if;
    if p_lease_seconds < 60 or p_lease_seconds > 7200 then
        raise exception 'Benchmark lease must be between 60 and 7200 seconds';
    end if;

    update public.invoice_extraction_benchmark_suite_items item
    set status = 'failed',
        error = 'Benchmark worker lease expired and maximum attempts were reached',
        completed_at = now(),
        claimed_by = null,
        claimed_at = null,
        lease_expires_at = null,
        updated_at = now()
    where item.status = 'running'
      and item.lease_expires_at < now()
      and item.attempt_count >= item.max_attempts;

    select item.id
    into selected_id
    from public.invoice_extraction_benchmark_suite_items item
    join public.invoice_extraction_benchmark_suites suite on suite.id = item.suite_id
    where suite.status in ('queued', 'running')
      and (
          item.status = 'queued'
          or (
              item.status = 'running'
              and item.lease_expires_at < now()
              and item.attempt_count < item.max_attempts
          )
      )
    order by suite.created_at, item.created_at, item.id
    for update of item skip locked
    limit 1;

    if selected_id is null then
        return;
    end if;

    return query
    update public.invoice_extraction_benchmark_suite_items item
    set status = 'running',
        claimed_by = p_worker_id,
        claimed_at = now(),
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        attempt_count = item.attempt_count + 1,
        started_at = coalesce(item.started_at, now()),
        error = null,
        updated_at = now()
    where item.id = selected_id
    returning item.*;
end;
$$;

revoke all on function public.claim_next_invoice_extraction_benchmark_item(text,integer)
    from public, anon, authenticated;
grant execute on function public.claim_next_invoice_extraction_benchmark_item(text,integer)
    to service_role;

commit;

select '20260801120000_invoice_extraction_benchmark_suites_applied' as migration_note;
