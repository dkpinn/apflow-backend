-- Phase B8: Add SELECT policy to suppliers table
-- Allows any active org member to read that org's suppliers.
-- Uses the is_org_member() helper from organisation_management.sql.
-- Apply via Supabase SQL Editor.

alter table public.suppliers enable row level security;

drop policy if exists "suppliers_select_member" on public.suppliers;
create policy "suppliers_select_member"
  on public.suppliers for select to authenticated
  using (
    organisation_id is null
    or public.is_org_member(organisation_id)
  );
