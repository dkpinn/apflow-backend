-- Production custom Storage baseline captured from project
-- arueantocclxnziipwdf on 2026-06-08.
--
-- Supabase owns the storage schema itself. This migration contains only
-- application buckets and the policies currently present on storage.objects.
-- Mark its version as applied in production; do not replay it there.

insert into storage.buckets (
  id,
  name,
  public,
  file_size_limit,
  allowed_mime_types
)
values
  ('invoices', 'invoices', false, null, null),
  ('kyc-documents', 'kyc-documents', false, null, null),
  (
    'statement-files',
    'statement-files',
    false,
    52428800,
    array[
      'application/pdf',
      'text/csv',
      'application/csv',
      'application/vnd.ms-excel',
      'image/png',
      'image/jpeg',
      'image/webp'
    ]
  ),
  (
    'organisation-branding',
    'organisation-branding',
    false,
    2097152,
    array['image/png', 'image/jpeg', 'image/webp']
  )
on conflict (id) do update
set
  name = excluded.name,
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "Allow authenticated invoice reads" on storage.objects;
create policy "Allow authenticated invoice reads"
  on storage.objects for select to authenticated
  using (bucket_id = 'invoices');

drop policy if exists "Allow authenticated invoice updates" on storage.objects;
create policy "Allow authenticated invoice updates"
  on storage.objects for update to authenticated
  using (bucket_id = 'invoices')
  with check (bucket_id = 'invoices');

drop policy if exists "Allow authenticated invoice uploads" on storage.objects;
create policy "Allow authenticated invoice uploads"
  on storage.objects for insert to authenticated
  with check (bucket_id = 'invoices');

drop policy if exists "organisation_branding_select_member" on storage.objects;
create policy "organisation_branding_select_member"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'organisation-branding'
    and public.is_org_member(public.storage_object_org_id(name))
  );

drop policy if exists "organisation_branding_insert_admin" on storage.objects;
create policy "organisation_branding_insert_admin"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'organisation-branding'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop policy if exists "organisation_branding_update_admin" on storage.objects;
create policy "organisation_branding_update_admin"
  on storage.objects for update to authenticated
  using (
    bucket_id = 'organisation-branding'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  )
  with check (
    bucket_id = 'organisation-branding'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop policy if exists "organisation_branding_delete_admin" on storage.objects;
create policy "organisation_branding_delete_admin"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'organisation-branding'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop policy if exists "statement_files_select_member" on storage.objects;
create policy "statement_files_select_member"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'statement-files'
    and public.is_org_member(public.storage_object_org_id(name))
  );

drop policy if exists "statement_files_insert_accountants" on storage.objects;
create policy "statement_files_insert_accountants"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'statement-files'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "statement_files_update_accountants" on storage.objects;
create policy "statement_files_update_accountants"
  on storage.objects for update to authenticated
  using (
    bucket_id = 'statement-files'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin','accountant']::public.organisation_role[]
    )
  )
  with check (
    bucket_id = 'statement-files'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "statement_files_delete_accountants" on storage.objects;
create policy "statement_files_delete_accountants"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'statement-files'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );
