-- Bank rule enhancements: supplier tagging, auto-post, split allocations,
-- and organisation VAT-change tracking.
-- Copy to supabase/migrations/ and apply with Supabase CLI.

-- bank_transaction_rules: supplier link, auto-post flag, split allocations
ALTER TABLE public.bank_transaction_rules
  ADD COLUMN IF NOT EXISTS supplier_id uuid
    REFERENCES public.suppliers(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS auto_post boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS split_allocations jsonb;

COMMENT ON COLUMN public.bank_transaction_rules.supplier_id IS
  'Supplier/contact linked to this rule. Applied to bank lines when the rule fires.';
COMMENT ON COLUMN public.bank_transaction_rules.auto_post IS
  'When true, matching bank lines are automatically posted without manual review.';
COMMENT ON COLUMN public.bank_transaction_rules.split_allocations IS
  'JSON array of split rows. Each: {account_id, label?, type: "percent"|"fixed"|"remainder", value?, tracking?, tax_treatment?}';

-- organisations: record when VAT registration status changes so the UI can
-- prompt users to review their bank rules.
ALTER TABLE public.organisations
  ADD COLUMN IF NOT EXISTS vat_registered_changed_at timestamptz;

COMMENT ON COLUMN public.organisations.vat_registered_changed_at IS
  'Set automatically whenever vat_registered flips. Used to prompt rule VAT review.';

CREATE OR REPLACE FUNCTION public.track_vat_registered_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.vat_registered IS DISTINCT FROM OLD.vat_registered THEN
    NEW.vat_registered_changed_at = now();
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_org_vat_change ON public.organisations;
CREATE TRIGGER trg_org_vat_change
  BEFORE UPDATE ON public.organisations
  FOR EACH ROW EXECUTE FUNCTION public.track_vat_registered_change();

SELECT '20260703_bank_rule_enhancements_applied' AS migration_note;
