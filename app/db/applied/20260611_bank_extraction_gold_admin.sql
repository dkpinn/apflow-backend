-- ============================================================
-- Bank statement extraction: platform-wide gold library
-- Adds support for a single, cross-organisation library of
-- anonymised PDF + gold-JSON pairs used to benchmark the bank
-- statement extraction pipeline (managed via /api/admin/bank-extraction).
--
--   - public.is_platform_admin(): SECURITY DEFINER helper for RLS
--     policies that need to check platform_admin_users.
--   - bank_statement_gold_files: adds gold_pdf_storage_bucket /
--     gold_pdf_storage_path columns.
--   - storage bucket "bank-extraction-gold" (private) + RLS,
--     restricted to platform admins.
--   - Tightens bank_statement_gold_files / bank_statement_extraction_runs
--     RLS so organisation_id IS NULL ("platform library") rows are only
--     readable/writable by platform admins, not any org member.
--
-- Idempotent. Apply manually in external Supabase.
-- ============================================================

create or replace function public.is_platform_admin()
returns boolean
language sql stable security definer
set search_path to 'public'
as $$
  select exists (
    select 1 from public.platform_admin_users
    where user_id = auth.uid()
      and role = 'owner'
      and status = 'active'
  );
$$;

alter table public.bank_statement_gold_files
  add column if not exists gold_pdf_storage_bucket text null,
  add column if not exists gold_pdf_storage_path text null;

-- Storage bucket for the platform gold library -----------------------------

insert into storage.buckets (
  id,
  name,
  public,
  file_size_limit,
  allowed_mime_types
)
values (
  'bank-extraction-gold',
  'bank-extraction-gold',
  false,
  52428800,
  array[
    'application/pdf',
    'image/png',
    'image/jpeg',
    'image/webp'
  ]
)
on conflict (id) do update
set
  name = excluded.name,
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "bank_extraction_gold_select_platform_admin" on storage.objects;
create policy "bank_extraction_gold_select_platform_admin"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'bank-extraction-gold'
    and public.is_platform_admin()
  );

drop policy if exists "bank_extraction_gold_insert_platform_admin" on storage.objects;
create policy "bank_extraction_gold_insert_platform_admin"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'bank-extraction-gold'
    and public.is_platform_admin()
  );

drop policy if exists "bank_extraction_gold_update_platform_admin" on storage.objects;
create policy "bank_extraction_gold_update_platform_admin"
  on storage.objects for update to authenticated
  using (
    bucket_id = 'bank-extraction-gold'
    and public.is_platform_admin()
  )
  with check (
    bucket_id = 'bank-extraction-gold'
    and public.is_platform_admin()
  );

drop policy if exists "bank_extraction_gold_delete_platform_admin" on storage.objects;
create policy "bank_extraction_gold_delete_platform_admin"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'bank-extraction-gold'
    and public.is_platform_admin()
  );

-- Tighten table RLS for organisation_id IS NULL rows ------------------------

drop policy if exists "bank_extraction_runs_select_member" on public.bank_statement_extraction_runs;
create policy "bank_extraction_runs_select_member" on public.bank_statement_extraction_runs
  for select to authenticated using (
    (organisation_id is null and public.is_platform_admin())
    or (organisation_id is not null and public.is_org_member(organisation_id))
  );

drop policy if exists "bank_extraction_runs_write_accountants" on public.bank_statement_extraction_runs;
create policy "bank_extraction_runs_write_accountants" on public.bank_statement_extraction_runs
  for all to authenticated
  using (
    (organisation_id is null and public.is_platform_admin())
    or (organisation_id is not null and public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  )
  with check (
    (organisation_id is null and public.is_platform_admin())
    or (organisation_id is not null and public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  );

drop policy if exists "bank_gold_files_select_member" on public.bank_statement_gold_files;
create policy "bank_gold_files_select_member" on public.bank_statement_gold_files
  for select to authenticated using (
    (organisation_id is null and public.is_platform_admin())
    or (organisation_id is not null and public.is_org_member(organisation_id))
  );

drop policy if exists "bank_gold_files_write_accountants" on public.bank_statement_gold_files;
create policy "bank_gold_files_write_accountants" on public.bank_statement_gold_files
  for all to authenticated
  using (
    (organisation_id is null and public.is_platform_admin())
    or (organisation_id is not null and public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  )
  with check (
    (organisation_id is null and public.is_platform_admin())
    or (organisation_id is not null and public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  );
