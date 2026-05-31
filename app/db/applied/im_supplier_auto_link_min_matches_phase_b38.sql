-- ============================================================
-- Supplier Auto-Link Match Threshold — Phase B38
-- Organisation-level control for how many supplier identity
-- signals must match before an extracted document auto-links to
-- an existing supplier.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

alter table if exists public.organisations
  add column if not exists supplier_auto_link_min_matches integer not null default 2;

alter table if exists public.organisations
  drop constraint if exists organisations_supplier_auto_link_min_matches_check;

alter table if exists public.organisations
  add constraint organisations_supplier_auto_link_min_matches_check
  check (supplier_auto_link_min_matches between 1 and 4);

select 'im_supplier_auto_link_min_matches_phase_b38_applied' as migration_note;
