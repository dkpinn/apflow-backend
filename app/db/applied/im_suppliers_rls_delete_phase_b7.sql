-- Phase B7: Add DELETE policy to suppliers table
-- Allows owners and admins to delete suppliers in their org.
-- Also allows deleting orphaned records (organisation_id IS NULL)
-- so legacy data created before org management was wired in can be cleaned up.
-- Apply via Supabase SQL Editor.

alter table public.suppliers enable row level security;

drop policy if exists "suppliers_delete_admin" on public.suppliers;
create policy "suppliers_delete_admin"
  on public.suppliers for delete to authenticated
  using (
    organisation_id is null
    or public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[])
  );
