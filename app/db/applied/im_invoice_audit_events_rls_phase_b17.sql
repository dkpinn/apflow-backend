-- ============================================================
-- APPayPal Invoice Audit Events RLS Phase B17
-- Ensures organisation members can read audit trail rows from
-- Supabase fallback queries. Backend service-role inserts continue
-- to bypass RLS.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

alter table public.invoice_audit_events enable row level security;

drop policy if exists "invoice_audit_events_select_org_member" on public.invoice_audit_events;
create policy "invoice_audit_events_select_org_member"
  on public.invoice_audit_events for select to authenticated
  using (public.is_org_member(organisation_id));

select 'invoice_audit_events_rls_phase_b17_applied' as migration_note;
