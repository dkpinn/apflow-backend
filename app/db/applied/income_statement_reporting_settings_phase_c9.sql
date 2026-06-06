-- Phase C9: Organisation reporting defaults for generated financial reports.
-- Apply manually in external Supabase. Idempotent.

ALTER TABLE public.organisations
  ADD COLUMN IF NOT EXISTS reporting_standard text NOT NULL DEFAULT 'ifrs'
    CHECK (reporting_standard IN ('ifrs', 'us_gaap', 'uk_gaap_frs_102', 'aspe')),
  ADD COLUMN IF NOT EXISTS income_statement_presentation text NOT NULL DEFAULT 'function'
    CHECK (income_statement_presentation IN ('function', 'nature'));

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'organisations_us_gaap_function_presentation_chk'
      AND conrelid = 'public.organisations'::regclass
  ) THEN
    ALTER TABLE public.organisations
      ADD CONSTRAINT organisations_us_gaap_function_presentation_chk
      CHECK (
        reporting_standard <> 'us_gaap'
        OR income_statement_presentation = 'function'
      );
  END IF;
END $$;

COMMENT ON COLUMN public.organisations.reporting_standard IS
  'Default reporting framework used when generating financial statements.';

COMMENT ON COLUMN public.organisations.income_statement_presentation IS
  'Default Income Statement expense presentation: function or nature. US GAAP reports must resolve to function.';
