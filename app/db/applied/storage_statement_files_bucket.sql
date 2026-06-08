-- ============================================================
-- Statement Files Storage Bucket
-- Creates the private Supabase Storage bucket used by supplier
-- statement uploads and Bank/Cash statement uploads.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

insert into storage.buckets (
  id,
  name,
  public,
  file_size_limit,
  allowed_mime_types
)
values (
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
)
on conflict (id) do update
set
  name = excluded.name,
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

create or replace function public.storage_object_org_id(_name text)
returns uuid
language plpgsql
immutable
as $$
declare
  _first_segment text;
begin
  _first_segment := split_part(coalesce(_name, ''), '/', 1);
  if _first_segment ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    return _first_segment::uuid;
  end if;
  return null;
end;
$$;

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
