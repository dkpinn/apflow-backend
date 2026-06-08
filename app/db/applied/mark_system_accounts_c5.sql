-- Mark system/control accounts so they are excluded from direct posting.
-- Run this in the Supabase SQL editor, then move this file to app/db/applied/.

-- Step 1: accounts created by the system_accounts migration already have system_key set;
-- they just need is_system flipped to true.
UPDATE public.accounts
SET is_system = true
WHERE system_key IS NOT NULL
  AND is_system = false;

-- Step 2: mark pre-existing control accounts by code.
-- Covers both the standard migration codes and any legacy codes that may exist.
UPDATE public.accounts
SET is_system = true
WHERE is_system = false
  AND (
    code IN ('1200', '2100', '8100', '8500', '9999')  -- standard migration-assigned codes
    OR code IN ('6300', '8000', '9999')                -- legacy codes (adjust as needed)
  );

-- Step 3: mark any GL account that is linked to a bank_accounts row (bank clearing accounts).
UPDATE public.accounts a
SET is_system = true
FROM public.bank_accounts b
WHERE b.gl_account_id = a.id
  AND a.is_system = false;
