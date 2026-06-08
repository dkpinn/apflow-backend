-- Phase B9: Supplier invoice processing preferences
-- Adds columns that control how invoices from this supplier are extracted and allocated.
-- parse_line_items and line_items_include_vat may already exist — IF NOT EXISTS is safe.
-- Apply via Supabase SQL Editor.

ALTER TABLE public.suppliers
  ADD COLUMN IF NOT EXISTS parse_line_items          boolean       DEFAULT false,
  ADD COLUMN IF NOT EXISTS line_items_include_vat    boolean       DEFAULT true,
  ADD COLUMN IF NOT EXISTS track_inventory           boolean       DEFAULT false,
  ADD COLUMN IF NOT EXISTS use_uom_from_description  boolean       DEFAULT false,
  ADD COLUMN IF NOT EXISTS default_expense_account   text,
  ADD COLUMN IF NOT EXISTS default_vat_rate          numeric(5,2);
