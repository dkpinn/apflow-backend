-- Phase C5: Unified contact (customer) + allocation narration on bank statement lines
-- Extends the supplier tagging from phase C4 so a bank transaction can be linked to a
-- customer as well, and carries a free-text narration that becomes the posted GL journal
-- description (defaulting to the bank statement description).

ALTER TABLE bank_statement_lines
  ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id);

COMMENT ON COLUMN bank_statement_lines.customer_id IS
  'Customer linked to this bank transaction during reconciliation review (mutually exclusive with supplier_id).';

ALTER TABLE bank_statement_lines
  ADD COLUMN IF NOT EXISTS allocation_narration TEXT;

COMMENT ON COLUMN bank_statement_lines.allocation_narration IS
  'Free-text narration for the allocation; defaults to the bank description and becomes the posted GL journal description.';
