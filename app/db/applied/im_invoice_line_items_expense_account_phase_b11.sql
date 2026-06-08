-- Add expense_account column to invoice_line_items
-- Stores the GL account code applied to each line item (from supplier default or manual override).
ALTER TABLE invoice_line_items
  ADD COLUMN IF NOT EXISTS expense_account TEXT;
