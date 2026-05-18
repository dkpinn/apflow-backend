-- ============================================================
-- APPayPal Invoice Line Items: item code column
-- Stores the supplier's product/SKU code from the invoice line.
-- Used for future order matching against purchase orders.
-- Apply manually in Supabase SQL editor.
-- Idempotent. Does not change RLS.
-- ============================================================

alter table public.invoice_line_items
  add column if not exists code text;

create index if not exists invoice_line_items_code_idx
  on public.invoice_line_items(code)
  where code is not null;

select 'line_item_code_phase_b5_applied' as migration_note;
