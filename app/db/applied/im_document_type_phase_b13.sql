-- Phase b13: document_type and document_count on invoices_extracted
-- document_type classifies the kind of financial document (tax_invoice, card_receipt, etc.)
-- document_count flags uploads where multiple separate documents appear on one page

ALTER TABLE invoices_extracted
  ADD COLUMN IF NOT EXISTS document_type TEXT DEFAULT 'tax_invoice',
  ADD COLUMN IF NOT EXISTS document_count INTEGER DEFAULT 1;

-- Index for filtering by document type in the invoice list
CREATE INDEX IF NOT EXISTS idx_invoices_extracted_document_type
  ON invoices_extracted (organisation_id, document_type);
