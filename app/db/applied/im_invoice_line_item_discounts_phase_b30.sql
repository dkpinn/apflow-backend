-- ============================================================
-- APPayPal Invoice Line Item Discounts Phase B30
-- Stores discount-aware pricing evidence for invoice line items.
--
-- line_total remains the accounting source of truth. Discount
-- fields explain how that net line total was derived from printed
-- unit price, discount percentage/amount, or discounted unit price.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

alter table public.invoice_line_items
  add column if not exists discount_amount numeric(14, 2),
  add column if not exists discount_percent numeric(9, 4),
  add column if not exists discounted_unit_price numeric(14, 2),
  add column if not exists pricing_basis text,
  add column if not exists pricing_notes jsonb not null default '{}'::jsonb;

select 'invoice_line_item_discounts_phase_b30_applied' as migration_note;
