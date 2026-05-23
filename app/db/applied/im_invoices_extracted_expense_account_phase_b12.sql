-- Add expense_account column to invoices_extracted
-- Stores the GL account code applied to the invoice (from supplier default or manual override).
ALTER TABLE invoices_extracted
  ADD COLUMN IF NOT EXISTS expense_account TEXT;
