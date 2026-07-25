-- Create the private finance-lease document bucket and enforce organisation-first
-- object paths. The API performs the same checks before any service-role access.

insert into storage.buckets(id, name, public, file_size_limit, allowed_mime_types)
values (
  'finance-lease-docs',
  'finance-lease-docs',
  false,
  10485760,
  array['application/pdf', 'image/png', 'image/jpeg', 'image/webp']
)
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "finance_lease_documents_select_member" on storage.objects;
create policy "finance_lease_documents_select_member"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'finance-lease-docs'
    and public.is_org_member(public.storage_object_org_id(name))
  );

drop policy if exists "finance_lease_documents_insert_accountants" on storage.objects;
create policy "finance_lease_documents_insert_accountants"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'finance-lease-docs'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "finance_lease_documents_update_accountants" on storage.objects;
create policy "finance_lease_documents_update_accountants"
  on storage.objects for update to authenticated
  using (
    bucket_id = 'finance-lease-docs'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin','accountant']::public.organisation_role[]
    )
  )
  with check (
    bucket_id = 'finance-lease-docs'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "finance_lease_documents_delete_accountants" on storage.objects;
create policy "finance_lease_documents_delete_accountants"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'finance-lease-docs'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );
