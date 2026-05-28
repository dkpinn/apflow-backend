-- Migration: im_system_accounts_phase_b23.sql
-- Adds system-protected accounts (starting with a Rounding account) that are
-- automatically created for every organisation and cannot be deactivated or deleted.
--
-- Changes:
--   accounts: + is_system (bool), + system_key (text, unique per org)
--   New triggers: protect from deactivation / hard-delete / field mutation
--   New function: create_org_system_accounts(uuid)
--   New trigger: auto-seed on organisations INSERT
--   Backfill: seed Rounding account for every existing org

-- ── 1. New columns ─────────────────────────────────────────────────────────────
ALTER TABLE public.accounts
  ADD COLUMN IF NOT EXISTS is_system  boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS system_key text;

-- Ensure at most one system account per (org, key) pair
CREATE UNIQUE INDEX IF NOT EXISTS accounts_org_system_key_uidx
  ON public.accounts (organisation_id, system_key)
  WHERE system_key IS NOT NULL;

-- ── 2. Protect system accounts — UPDATE ────────────────────────────────────────
-- Blocks:  deactivating, renaming, changing type, touching is_system / system_key.
-- Allows:  code, group_name, vat_treatment, description, updated_at.
CREATE OR REPLACE FUNCTION public.protect_system_accounts()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.is_system THEN
    IF NEW.active = false AND OLD.active = true THEN
      RAISE EXCEPTION 'System account "%" cannot be deactivated.', OLD.name;
    END IF;
    IF NEW.is_system  IS DISTINCT FROM OLD.is_system  OR
       NEW.system_key IS DISTINCT FROM OLD.system_key OR
       NEW.name       IS DISTINCT FROM OLD.name       OR
       NEW.type       IS DISTINCT FROM OLD.type
    THEN
      RAISE EXCEPTION
        'The name, type, and system flags of system account "%" cannot be changed.',
        OLD.name;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_system_accounts ON public.accounts;
CREATE TRIGGER trg_protect_system_accounts
  BEFORE UPDATE ON public.accounts
  FOR EACH ROW EXECUTE FUNCTION public.protect_system_accounts();

-- ── 3. Protect system accounts — DELETE ────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.prevent_system_account_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.is_system THEN
    RAISE EXCEPTION 'System account "%" cannot be deleted.', OLD.name;
  END IF;
  RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_prevent_system_account_delete ON public.accounts;
CREATE TRIGGER trg_prevent_system_account_delete
  BEFORE DELETE ON public.accounts
  FOR EACH ROW EXECUTE FUNCTION public.prevent_system_account_delete();

-- ── 4. Seed function ─────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.create_org_system_accounts(p_org_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  -- Rounding account — code 9999, type other, full VAT treatment.
  -- Code and group_name are user-editable; everything else is locked.
  INSERT INTO public.accounts
    ( organisation_id, code, name, type, group_name,
      vat_treatment, is_system, system_key )
  VALUES
    ( p_org_id, '9999', 'Rounding', 'other', 'Rounding Adjustments',
      'full', true, 'rounding' )
  ON CONFLICT (organisation_id, system_key)
    WHERE system_key IS NOT NULL
    DO NOTHING;
END;
$$;

-- ── 5. Trigger: auto-seed on new org ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.on_organisation_created()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  PERFORM public.create_org_system_accounts(NEW.id);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_on_organisation_created ON public.organisations;
CREATE TRIGGER trg_on_organisation_created
  AFTER INSERT ON public.organisations
  FOR EACH ROW EXECUTE FUNCTION public.on_organisation_created();

-- ── 6. Backfill: seed for all existing orgs ───────────────────────────────────
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT id FROM public.organisations LOOP
    PERFORM public.create_org_system_accounts(r.id);
  END LOOP;
END;
$$;
