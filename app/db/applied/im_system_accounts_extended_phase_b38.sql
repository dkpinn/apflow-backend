-- ============================================================
-- Extend system accounts — Phase B38
-- Adds Trade Receivables, Trade Payables, VAT Control, and
-- Salaries & Wages as system accounts for every organisation.
--
-- Valid account types: income, expense, asset, liability, equity, other
-- vat_treatment is NOT NULL; valid values: full, blocked, exempt, zero_rated
-- ============================================================

CREATE OR REPLACE FUNCTION public.create_org_system_accounts(p_org_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  -- Rounding (original — preserved so function is self-contained)
  INSERT INTO public.accounts
    (organisation_id, code, name, type, group_name, vat_treatment, is_system, system_key)
  SELECT p_org_id, '9999', 'Rounding', 'other', 'Rounding Adjustments', 'full', true, 'rounding'
  WHERE NOT EXISTS (
    SELECT 1 FROM public.accounts WHERE organisation_id = p_org_id AND system_key = 'rounding'
  );

  -- Trade Receivables (Debtors)
  INSERT INTO public.accounts
    (organisation_id, code, name, type, group_name, vat_treatment, is_system, system_key)
  SELECT p_org_id, '1200', 'Trade Receivables (Debtors)', 'asset', 'Current Assets', 'full', true, 'trade_receivables'
  WHERE NOT EXISTS (
    SELECT 1 FROM public.accounts WHERE organisation_id = p_org_id AND system_key = 'trade_receivables'
  );

  -- Trade Payables (Creditors)
  INSERT INTO public.accounts
    (organisation_id, code, name, type, group_name, vat_treatment, is_system, system_key)
  SELECT p_org_id, '2100', 'Trade Payables (Creditors)', 'liability', 'Current Liabilities', 'full', true, 'trade_payables'
  WHERE NOT EXISTS (
    SELECT 1 FROM public.accounts WHERE organisation_id = p_org_id AND system_key = 'trade_payables'
  );

  -- VAT Control Account
  INSERT INTO public.accounts
    (organisation_id, code, name, type, group_name, vat_treatment, is_system, system_key)
  SELECT p_org_id, '8100', 'VAT Control Account', 'liability', 'Tax', 'full', true, 'vat_control'
  WHERE NOT EXISTS (
    SELECT 1 FROM public.accounts WHERE organisation_id = p_org_id AND system_key = 'vat_control'
  );

  -- Salaries and Wages
  INSERT INTO public.accounts
    (organisation_id, code, name, type, group_name, vat_treatment, is_system, system_key)
  SELECT p_org_id, '8500', 'Salaries and Wages Control Account', 'expense', 'Employee Costs', 'full', true, 'salaries_wages'
  WHERE NOT EXISTS (
    SELECT 1 FROM public.accounts WHERE organisation_id = p_org_id AND system_key = 'salaries_wages'
  );
END;
$$;

-- Backfill all existing organisations
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT id FROM public.organisations LOOP
    PERFORM public.create_org_system_accounts(r.id);
  END LOOP;
END;
$$;

SELECT 'im_system_accounts_extended_phase_b38_applied' AS migration_note;
