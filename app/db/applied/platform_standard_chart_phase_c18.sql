-- Phase C18: Platform Standard Chart of Accounts
-- Creates a platform-level template that is seeded to every new organisation.
-- The create_org_system_accounts() function is extended to copy from this template.

-- ──────────────────────────────────────────────
-- 1. Table
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.platform_standard_accounts (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  code          text        NOT NULL,
  name          text        NOT NULL,
  type          text        NOT NULL
                CHECK (type IN ('income','expense','asset','liability','equity','other')),
  group_name    text,
  description   text,
  vat_treatment text        NOT NULL DEFAULT 'full'
                CHECK (vat_treatment IN ('full','blocked','exempt','zero_rated')),
  is_system     boolean     NOT NULL DEFAULT false,
  system_key    text        UNIQUE,
  display_order integer     NOT NULL DEFAULT 0,
  active        boolean     NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.platform_standard_accounts IS
  'Platform-level template chart of accounts. Seeded to every new organisation on creation.';

-- ──────────────────────────────────────────────
-- 2. RLS
-- ──────────────────────────────────────────────
ALTER TABLE public.platform_standard_accounts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "platform_standard_accounts_read" ON public.platform_standard_accounts;
CREATE POLICY "platform_standard_accounts_read"
  ON public.platform_standard_accounts
  FOR SELECT TO authenticated
  USING (true);

DROP POLICY IF EXISTS "platform_standard_accounts_write" ON public.platform_standard_accounts;
CREATE POLICY "platform_standard_accounts_write"
  ON public.platform_standard_accounts
  FOR ALL TO authenticated
  USING   (public.is_platform_admin())
  WITH CHECK (public.is_platform_admin());

-- ──────────────────────────────────────────────
-- 3. Seed: Standard accounts
--    display_order drives the grouping sequence on the admin page.
-- ──────────────────────────────────────────────

-- Revenue / Income
INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order, is_system) VALUES
  ('4000', 'Sales Revenue',     'income', 'Revenue',      100, false),
  ('4010', 'Service Revenue',   'income', 'Revenue',      110, false),
  ('4020', 'Other Income',      'income', 'Other Income', 200, false),
  ('4030', 'Interest Received', 'income', 'Other Income', 210, false),
  ('4040', 'Rental Income',     'income', 'Other Income', 220, false)
ON CONFLICT (system_key) WHERE system_key IS NOT NULL DO NOTHING;

-- Direct Costs / Cost of Sales
INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('5000', 'Cost of Sales',              'expense', 'Direct Costs', 300),
  ('5010', 'Raw Materials & Consumables','expense', 'Direct Costs', 310),
  ('5020', 'Direct Labour',              'expense', 'Direct Costs', 320),
  ('5030', 'Freight & Import Costs',     'expense', 'Direct Costs', 330)
ON CONFLICT DO NOTHING;

-- Operating Expenses (salaries is the only system account here)
INSERT INTO public.platform_standard_accounts
  (code, name, type, group_name, display_order, is_system, system_key) VALUES
  ('6000', 'Salaries and Wages', 'expense', 'Operating Expenses', 400, true, 'salaries_wages')
ON CONFLICT (system_key) WHERE system_key IS NOT NULL DO NOTHING;

INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('6010', 'Rent',                          'expense', 'Operating Expenses', 410),
  ('6020', 'Utilities',                     'expense', 'Operating Expenses', 420),
  ('6030', 'Insurance',                     'expense', 'Operating Expenses', 430),
  ('6040', 'Bank Charges',                  'expense', 'Operating Expenses', 440),
  ('6050', 'Telephone & Internet',          'expense', 'Operating Expenses', 450),
  ('6060', 'Accounting & Professional Fees','expense', 'Operating Expenses', 460),
  ('6070', 'Depreciation',                  'expense', 'Operating Expenses', 470),
  ('6080', 'Travel & Entertainment',        'expense', 'Operating Expenses', 480),
  ('6090', 'Advertising & Marketing',       'expense', 'Operating Expenses', 490),
  ('6095', 'Repairs & Maintenance',         'expense', 'Operating Expenses', 495),
  ('6100', 'Office Supplies',               'expense', 'Operating Expenses', 500),
  ('6110', 'Motor Vehicle Expenses',        'expense', 'Operating Expenses', 510)
ON CONFLICT DO NOTHING;

-- Taxation (income tax provision — separate from VAT liability)
INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('6300', 'Income Tax Expense', 'expense', 'Taxation', 600)
ON CONFLICT DO NOTHING;

-- Current Assets
INSERT INTO public.platform_standard_accounts
  (code, name, type, group_name, display_order, is_system, system_key) VALUES
  ('1200', 'Trade Debtors (Accounts Receivable)', 'asset', 'Current Assets', 700, true, 'trade_receivables')
ON CONFLICT (system_key) WHERE system_key IS NOT NULL DO NOTHING;

INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('1300', 'Prepaid Expenses', 'asset', 'Current Assets', 710),
  ('1400', 'VAT Receivable',   'asset', 'Current Assets', 720)
ON CONFLICT DO NOTHING;

-- Inventory
INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('1500', 'Inventory', 'asset', 'Inventory', 800)
ON CONFLICT DO NOTHING;

-- Investments
INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('1600', 'Long-term Investments', 'asset', 'Investments', 900),
  ('1610', 'Fixed Deposits',        'asset', 'Investments', 910)
ON CONFLICT DO NOTHING;

-- Bank accounts: codes 6200001+ are auto-assigned by the bank account creation endpoint.
-- No pre-seeded bank accounts here.

-- Current Liabilities
INSERT INTO public.platform_standard_accounts
  (code, name, type, group_name, display_order, is_system, system_key) VALUES
  ('2100', 'Trade Creditors (Accounts Payable)', 'liability', 'Current Liabilities', 1000, true, 'trade_payables'),
  ('2200', 'VAT Control Account',               'liability', 'Current Liabilities', 1010, true, 'vat_control')
ON CONFLICT (system_key) WHERE system_key IS NOT NULL DO NOTHING;

INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('2300', 'Accrued Expenses', 'liability', 'Current Liabilities', 1020),
  ('2400', 'PAYE Payable',     'liability', 'Current Liabilities', 1030),
  ('2500', 'UIF Payable',      'liability', 'Current Liabilities', 1040)
ON CONFLICT DO NOTHING;

-- Finance Agreements / Long-term Liabilities
INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('3000', 'Vehicle Finance',  'liability', 'Finance Agreements', 1100),
  ('3100', 'Mortgage / Bond',  'liability', 'Finance Agreements', 1110),
  ('3200', 'Term Loan',        'liability', 'Finance Agreements', 1120),
  ('3300', 'Lease Liability',  'liability', 'Finance Agreements', 1130)
ON CONFLICT DO NOTHING;

-- Equity
INSERT INTO public.platform_standard_accounts (code, name, type, group_name, display_order) VALUES
  ('7000', 'Share Capital / Owner''s Equity', 'equity', 'Equity',               1200),
  ('7010', 'Share Premium',                   'equity', 'Equity',               1210)
ON CONFLICT DO NOTHING;

INSERT INTO public.platform_standard_accounts
  (code, name, type, group_name, display_order, is_system, system_key) VALUES
  ('7100', 'Retained Earnings / (Accumulated Loss)', 'equity', 'Retained Income/Loss', 1220, true, 'retained_earnings')
ON CONFLICT (system_key) WHERE system_key IS NOT NULL DO NOTHING;

-- Rounding (system — must always exist in every org)
INSERT INTO public.platform_standard_accounts
  (code, name, type, group_name, display_order, is_system, system_key) VALUES
  ('9999', 'Rounding Adjustments', 'other', 'Rounding', 9999, true, 'rounding')
ON CONFLICT (system_key) WHERE system_key IS NOT NULL DO NOTHING;


-- ──────────────────────────────────────────────
-- 4. Replace create_org_system_accounts()
--    Now seeds from the platform template instead of a hardcoded list.
-- ──────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.create_org_system_accounts(p_org_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  -- System accounts (keyed by system_key) — use ON CONFLICT to skip existing
  INSERT INTO public.accounts
    (organisation_id, code, name, type, group_name, vat_treatment, is_system, system_key, active)
  SELECT
    p_org_id,
    psa.code,
    psa.name,
    psa.type,
    psa.group_name,
    psa.vat_treatment,
    psa.is_system,
    psa.system_key,
    true
  FROM public.platform_standard_accounts psa
  WHERE psa.active = true
    AND psa.system_key IS NOT NULL
  ON CONFLICT (organisation_id, system_key)
    WHERE system_key IS NOT NULL
    DO NOTHING;

  -- Non-system accounts — insert if no account with the same code exists in this org
  INSERT INTO public.accounts
    (organisation_id, code, name, type, group_name, vat_treatment, is_system, active)
  SELECT
    p_org_id,
    psa.code,
    psa.name,
    psa.type,
    psa.group_name,
    psa.vat_treatment,
    false,
    true
  FROM public.platform_standard_accounts psa
  WHERE psa.active = true
    AND psa.system_key IS NULL
  ON CONFLICT DO NOTHING;
END;
$$;
