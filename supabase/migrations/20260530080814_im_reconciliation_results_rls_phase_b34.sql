-- ============================================================
-- Tighten reconciliation_results write policies to role checks.
-- Existing remote migration fetched into local history.
-- ============================================================

drop policy if exists "reconciliation_results_select_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_select_own_org"
  on public.reconciliation_results for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "reconciliation_results_insert_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_insert_own_org"
  on public.reconciliation_results for insert to authenticated
  with check (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "reconciliation_results_update_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_update_own_org"
  on public.reconciliation_results for update to authenticated
  using (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  )
  with check (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "reconciliation_results_delete_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_delete_own_org"
  on public.reconciliation_results for delete to authenticated
  using (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );
