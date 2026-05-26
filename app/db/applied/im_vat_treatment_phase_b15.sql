-- Phase b15: VAT treatment per GL account (SA VAT Act Section 17)
-- Certain expenses (entertainment, refreshments, club subscriptions) carry VAT charged by
-- the supplier that the purchaser cannot claim as input tax.
-- Setting vat_treatment on the GL account propagates automatically to every line item
-- coded to that account — no per-invoice effort required.

-- Add VAT treatment to Chart of Accounts
ALTER TABLE accounts
  ADD COLUMN IF NOT EXISTS vat_treatment TEXT NOT NULL DEFAULT 'full'
  CHECK (vat_treatment IN ('full', 'blocked', 'exempt', 'zero_rated'));

COMMENT ON COLUMN accounts.vat_treatment IS
  'full=standard claimable input VAT | blocked=S17 no claim (entertainment etc) |
   exempt=supplier not VAT registered | zero_rated=0% VAT supply';

-- Cache the treatment on each line item for fast invoice-level computation.
-- Populated at save time from the matched account; NULL means inherit ''full''.
ALTER TABLE invoice_line_items
  ADD COLUMN IF NOT EXISTS vat_treatment TEXT DEFAULT NULL
  CHECK (vat_treatment IS NULL OR vat_treatment IN ('full', 'blocked', 'exempt', 'zero_rated'));

COMMENT ON COLUMN invoice_line_items.vat_treatment IS
  'Inherited from accounts.vat_treatment at save time. NULL = full (standard claimable).';

-- Index for fast per-invoice VAT split queries
CREATE INDEX IF NOT EXISTS idx_invoice_line_items_vat_treatment
  ON invoice_line_items (invoice_extracted_id, vat_treatment)
  WHERE vat_treatment IS NOT NULL AND vat_treatment <> 'full';
