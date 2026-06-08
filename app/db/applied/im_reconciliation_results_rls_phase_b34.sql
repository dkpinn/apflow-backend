-- ============================================================
-- Tighten reconciliation_results write policies to role-based checks.
-- Replaces the any-org-member insert policy with owner/admin/accountant
-- guards, and adds missing update and delete policies.
-- ============================================================

-- SELECT: any active org member may read (unchanged intent, switched to helper)
drop policy if exists "reconciliation_results_select_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_select_own_org"
  on public.reconciliation_results for select to authenticated
  using (public.is_org_member(organisation_id));

-- INSERT/UPDATE/DELETE: restricted to privileged roles
drop policy if exists "reconciliation_results_insert_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_insert_own_org"
  on public.reconciliation_results for insert to authenticated
  with check (
    public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[])
  );

drop policy if exists "reconciliation_results_update_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_update_own_org"
  on public.reconciliation_results for update to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "reconciliation_results_delete_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_delete_own_org"
  on public.reconciliation_results for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));
