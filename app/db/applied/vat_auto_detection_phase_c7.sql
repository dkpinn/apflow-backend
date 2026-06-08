-- Phase C7: Store auto-detected VAT treatment per extraction.
-- Run in Supabase SQL editor, then move to app/db/applied/.

ALTER TABLE public.invoices_extracted
  ADD COLUMN IF NOT EXISTS prices_include_vat_detected TEXT
    CHECK (prices_include_vat_detected IN ('exclusive', 'inclusive'));

COMMENT ON COLUMN public.invoices_extracted.prices_include_vat_detected IS
  'Auto-detected VAT treatment: exclusive = line prices are ex-VAT; inclusive = prices include VAT (stripped during extraction).';
