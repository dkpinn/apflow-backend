-- Phase C10: Income Statement account and tracking classifications.
-- Apply manually in external Supabase. Idempotent.

ALTER TABLE public.accounts
  ADD COLUMN IF NOT EXISTS income_statement_nature text
    CHECK (
      income_statement_nature IS NULL OR income_statement_nature IN (
        'revenue',
        'changes_in_inventories',
        'raw_materials_consumables',
        'employee_benefits',
        'depreciation_amortisation',
        'other_operating_expenses',
        'other_operating_income'
      )
    ),
  ADD COLUMN IF NOT EXISTS default_income_statement_function text
    CHECK (
      default_income_statement_function IS NULL OR default_income_statement_function IN (
        'cogs',
        'selling',
        'g_and_a',
        'r_and_d',
        'other_operating'
      )
    ),
  ADD COLUMN IF NOT EXISTS special_report_classification text NOT NULL DEFAULT 'none'
    CHECK (
      special_report_classification IN (
        'none',
        'finance_cost',
        'associate_profit',
        'discontinued_operations',
        'extraordinary'
      )
    );

ALTER TABLE public.tracking_dimensions
  ADD COLUMN IF NOT EXISTS is_income_statement_function_driver boolean NOT NULL DEFAULT false;

ALTER TABLE public.tracking_values
  ADD COLUMN IF NOT EXISTS income_statement_function text
    CHECK (
      income_statement_function IS NULL OR income_statement_function IN (
        'cogs',
        'selling',
        'g_and_a',
        'r_and_d',
        'other_operating'
      )
    );

CREATE UNIQUE INDEX IF NOT EXISTS tracking_dimensions_one_is_function_driver_per_org
  ON public.tracking_dimensions (organisation_id)
  WHERE is_income_statement_function_driver;

COMMENT ON COLUMN public.accounts.income_statement_nature IS
  'Nature classification used for Income Statement presentation by nature.';
COMMENT ON COLUMN public.accounts.default_income_statement_function IS
  'Fallback Function classification when no mapped function driver tracking value exists.';
COMMENT ON COLUMN public.accounts.special_report_classification IS
  'Special Income Statement placement outside normal operating grouping.';
COMMENT ON COLUMN public.tracking_dimensions.is_income_statement_function_driver IS
  'Marks the one tracking dimension whose values drive Income Statement Function presentation.';
COMMENT ON COLUMN public.tracking_values.income_statement_function IS
  'Function classification used when this tracking value appears on a posted GL line.';
