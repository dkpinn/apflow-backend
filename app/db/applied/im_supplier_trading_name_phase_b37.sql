-- ============================================================
-- Add trading_name to suppliers — Phase B37
-- The column was already referenced in matching and display code
-- but never migrated to the schema.
-- ============================================================

alter table if exists public.suppliers
  add column if not exists trading_name text default null;

-- Record migration application
select 'im_supplier_trading_name_phase_b37_applied' as migration_note;
