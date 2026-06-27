-- Receipt Inbox: link a captured receipt document to a bank statement line
ALTER TABLE bank_statement_lines
  ADD COLUMN IF NOT EXISTS receipt_document_id uuid REFERENCES invoices_extracted(id);

CREATE INDEX IF NOT EXISTS idx_bank_lines_receipt_document
  ON bank_statement_lines (receipt_document_id)
  WHERE receipt_document_id IS NOT NULL;
