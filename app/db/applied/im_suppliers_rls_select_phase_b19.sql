-- Phase B19: Tighten suppliers SELECT policy.
--
-- Original policy (phase_b8) allowed any authenticated user to read rows where
-- organisation_id IS NULL, intended for orphaned legacy records created before
-- org management was wired in. No application code creates NULL-org suppliers:
--   - create_supplier() raises HTTP 400 if organisation_id is missing
--   - create_supplier_from_invoice() always sets organisation_id from the invoice
-- The IS NULL arm therefore exposes legacy orphaned rows to every authenticated
-- user with no scoping. The new policy requires a non-NULL organisation_id that
-- the requesting user is an active member of.
--
-- Note: the companion suppliers_delete_admin policy (phase_b7) contains the
-- same IS NULL arm. That policy is NOT changed here — owner/admin users can
-- still delete orphaned rows via the DELETE policy. Once all NULL-org rows have
-- been cleaned up a follow-on patch can tighten the DELETE policy to match.
--
-- Apply via Supabase SQL Editor.

drop policy if exists "suppliers_select_member" on public.suppliers;
create policy "suppliers_select_member"
  on public.suppliers for select to authenticated
  using (
    organisation_id is not null
    and public.is_org_member(organisation_id)
  );
