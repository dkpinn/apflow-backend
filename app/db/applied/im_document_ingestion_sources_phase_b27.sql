-- ============================================================
-- Document Ingestion Sources — Phase B27
-- Adds source tracking to invoices_raw (email, whatsapp, mobile,
-- cloud_folder in addition to existing 'upload'), assigns a unique
-- inbound email address per organisation for email-based document
-- submission, and adds sender identity columns to organisation_users
-- for WhatsApp and email resolution.
-- ============================================================

-- 1) source_type on invoices_raw ---------------------------------
-- The frontend already sets source_type = 'upload' on new rows;
-- this adds the column formally so it is persisted.
alter table if exists public.invoices_raw
  add column if not exists source_type text not null default 'upload';

-- 2) inbound_email on organisations ------------------------------
-- Each organisation receives a unique email address of the form
-- inv-{12hexchars}@mail.apflow.com that is used as the TO address
-- for Mailgun Inbound Routes.  The token is derived from the org UUID
-- so it is stable, collision-free, and reproducible.
alter table if exists public.organisations
  add column if not exists inbound_email text unique;

-- Backfill existing organisations
update public.organisations
set inbound_email =
  'inv-' || substring(replace(id::text, '-', '') from 1 for 12) || '@mail.apflow.com'
where inbound_email is null;

-- Trigger: auto-assign inbound_email for all future organisations
create or replace function public.assign_org_inbound_email()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.inbound_email is null then
    new.inbound_email :=
      'inv-' || pg_catalog.substring(pg_catalog.replace(new.id::text, '-', '') from 1 for 12) || '@mail.apflow.com';
  end if;
  return new;
end;
$$;

drop trigger if exists trg_assign_org_inbound_email on public.organisations;
create trigger trg_assign_org_inbound_email
  before insert on public.organisations
  for each row execute function public.assign_org_inbound_email();

-- 3) Sender identity columns on organisation_users ---------------
-- phone                  — mobile number for WhatsApp sender resolution (Phase 3)
-- external_sender_emails — extra FROM addresses a member may use when
--                          forwarding invoices (email resolution)
alter table if exists public.organisation_users
  add column if not exists phone text,
  add column if not exists external_sender_emails text[] not null default '{}';

-- Record migration application
select 'im_document_ingestion_sources_phase_b27_applied' as migration_note;
