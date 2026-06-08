-- Phase C4: Supplier tagging on bank statement lines
-- Allows associating a bank transaction with a supplier for audit trail and invoice matching.

ALTER TABLE bank_statement_lines
  ADD COLUMN IF NOT EXISTS supplier_id UUID REFERENCES suppliers(id);

COMMENT ON COLUMN bank_statement_lines.supplier_id IS
  'Supplier linked to this bank transaction during reconciliation review.';

-- Default tracking value per dimension (user-configured in Settings)
ALTER TABLE tracking_dimensions
  ADD COLUMN IF NOT EXISTS default_value_id UUID REFERENCES tracking_values(id) ON DELETE SET NULL;

COMMENT ON COLUMN tracking_dimensions.default_value_id IS
  'Default tracking value auto-selected when opening a bank transaction for review.';
