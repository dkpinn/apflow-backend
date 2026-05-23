-- ============================================================
-- APPayPal Supplier Extraction Profile Phase B3.4
-- Adds optional supplier profile fields that are already extracted
-- from invoices/receipts but were not present on suppliers.
-- Apply manually in external Supabase.
-- Idempotent. Does not change RLS.
-- ============================================================

alter table public.suppliers
  add column if not exists delivery_address text,
  add column if not exists postal_address text,
  add column if not exists accounting_email text,
  add column if not exists fax text,
  add column if not exists cell text,
  add column if not exists website text,
  add column if not exists source_invoice_extracted_id uuid;

select 'supplier_extraction_profile_phase_b3_4_applied' as migration_note;
