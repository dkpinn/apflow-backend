-- Phase C17: Asset types with managed cost, accumulated, and expense accounts.
-- Apply in Supabase SQL editor, then move this file to app/db/applied/.

CREATE TABLE IF NOT EXISTS public.asset_types (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id UUID NOT NULL REFERENCES public.organisations(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('tangible', 'intangible')),
  depreciation_method TEXT NOT NULL DEFAULT 'straight_line'
    CHECK (depreciation_method = 'straight_line'),
  useful_life_months INTEGER NOT NULL CHECK (useful_life_months BETWEEN 1 AND 1200),
  residual_value_percent NUMERIC(5,2) NOT NULL DEFAULT 0
    CHECK (residual_value_percent BETWEEN 0 AND 100),
  depreciation_convention TEXT NOT NULL DEFAULT 'in_service_month'
    CHECK (depreciation_convention = 'in_service_month'),
  active BOOLEAN NOT NULL DEFAULT true,
  archived_at TIMESTAMPTZ,
  archived_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  cost_account_id UUID REFERENCES public.accounts(id) ON DELETE RESTRICT,
  accumulated_account_id UUID REFERENCES public.accounts(id) ON DELETE RESTRICT,
  expense_account_id UUID REFERENCES public.accounts(id) ON DELETE RESTRICT,
  created_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT asset_types_accounts_complete_check CHECK (
    (cost_account_id IS NULL AND accumulated_account_id IS NULL AND expense_account_id IS NULL)
    OR
    (cost_account_id IS NOT NULL AND accumulated_account_id IS NOT NULL AND expense_account_id IS NOT NULL)
  ),
  CONSTRAINT asset_types_archive_state_check CHECK (
    (active AND archived_at IS NULL)
    OR
    (NOT active AND archived_at IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS asset_types_org_name_ci_uidx
  ON public.asset_types (organisation_id, lower(btrim(name)));

CREATE INDEX IF NOT EXISTS asset_types_org_active_idx
  ON public.asset_types (organisation_id, active, name);

ALTER TABLE public.accounts
  ADD COLUMN IF NOT EXISTS managed_asset_type_id UUID
    REFERENCES public.asset_types(id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS asset_account_role TEXT
    CHECK (asset_account_role IS NULL OR asset_account_role IN ('cost', 'accumulated', 'expense'));

ALTER TABLE public.accounts
  DROP CONSTRAINT IF EXISTS accounts_asset_management_pair_check;

ALTER TABLE public.accounts
  ADD CONSTRAINT accounts_asset_management_pair_check CHECK (
    (managed_asset_type_id IS NULL AND asset_account_role IS NULL)
    OR
    (managed_asset_type_id IS NOT NULL AND asset_account_role IS NOT NULL AND is_system)
  );

CREATE UNIQUE INDEX IF NOT EXISTS accounts_asset_type_role_uidx
  ON public.accounts (managed_asset_type_id, asset_account_role)
  WHERE managed_asset_type_id IS NOT NULL;

ALTER TABLE public.asset_types ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "asset_types_select_member" ON public.asset_types;
CREATE POLICY "asset_types_select_member"
  ON public.asset_types FOR SELECT TO authenticated
  USING (public.is_org_member(organisation_id));

-- Writes go through the transactional SECURITY DEFINER functions below.
DROP POLICY IF EXISTS "asset_types_write_admin" ON public.asset_types;

DROP TRIGGER IF EXISTS asset_types_set_updated_at ON public.asset_types;
CREATE TRIGGER asset_types_set_updated_at
  BEFORE UPDATE ON public.asset_types
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE OR REPLACE FUNCTION public.asset_type_account_names(
  p_name TEXT,
  p_category TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  clean_name TEXT := btrim(p_name);
BEGIN
  IF clean_name = '' THEN
    RAISE EXCEPTION 'Asset type name is required';
  END IF;
  IF p_category = 'tangible' THEN
    RETURN jsonb_build_object(
      'cost', clean_name || ' - At Cost',
      'accumulated', clean_name || ' - Accumulated Depreciation',
      'expense', 'Depreciation on ' || clean_name
    );
  END IF;
  IF p_category = 'intangible' THEN
    RETURN jsonb_build_object(
      'cost', clean_name || ' - At Cost',
      'accumulated', clean_name || ' - Accumulated Amortisation',
      'expense', 'Amortisation of ' || clean_name
    );
  END IF;
  RAISE EXCEPTION 'Asset type category must be tangible or intangible';
END;
$$;

CREATE OR REPLACE FUNCTION public.assert_asset_type_admin(p_org_id UUID)
RETURNS void
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND (
       auth.uid() IS NULL
       OR NOT public.has_org_role(
         p_org_id,
         ARRAY['owner','admin']::public.organisation_role[]
       )
     )
  THEN
    RAISE EXCEPTION 'Only organisation owners and admins can manage asset types';
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.protect_system_accounts()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  asset_maintenance BOOLEAN :=
    coalesce(current_setting('app.asset_type_account_maintenance', true), 'off') = 'on';
BEGIN
  IF OLD.is_system THEN
    IF asset_maintenance
       AND OLD.managed_asset_type_id IS NOT NULL
       AND NEW.managed_asset_type_id IS NOT DISTINCT FROM OLD.managed_asset_type_id
       AND NEW.asset_account_role IS NOT DISTINCT FROM OLD.asset_account_role
       AND NEW.system_key IS NOT DISTINCT FROM OLD.system_key
       AND NEW.is_system
    THEN
      RETURN NEW;
    END IF;

    IF NEW.active = false AND OLD.active = true THEN
      RAISE EXCEPTION 'System account "%" cannot be deactivated.', OLD.name;
    END IF;
    IF NEW.is_system IS DISTINCT FROM OLD.is_system
       OR NEW.system_key IS DISTINCT FROM OLD.system_key
       OR NEW.name IS DISTINCT FROM OLD.name
       OR NEW.type IS DISTINCT FROM OLD.type
       OR NEW.managed_asset_type_id IS DISTINCT FROM OLD.managed_asset_type_id
       OR NEW.asset_account_role IS DISTINCT FROM OLD.asset_account_role
    THEN
      RAISE EXCEPTION
        'The name, type, and system linkage of system account "%" cannot be changed.',
        OLD.name;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.prevent_system_account_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  asset_maintenance BOOLEAN :=
    coalesce(current_setting('app.asset_type_account_maintenance', true), 'off') = 'on';
BEGIN
  IF OLD.is_system
     AND NOT (asset_maintenance AND OLD.managed_asset_type_id IS NOT NULL)
  THEN
    RAISE EXCEPTION 'System account "%" cannot be deleted.', OLD.name;
  END IF;
  RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION public.asset_type_usage(p_asset_type_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  type_row public.asset_types%ROWTYPE;
  account_ids UUID[];
  active_assets INTEGER := 0;
  total_assets INTEGER := 0;
  journal_lines INTEGER := 0;
  mappings INTEGER := 0;
  has_active_column BOOLEAN := false;
  has_status_column BOOLEAN := false;
BEGIN
  SELECT * INTO type_row
  FROM public.asset_types
  WHERE id = p_asset_type_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Asset type not found';
  END IF;

  account_ids := ARRAY[
    type_row.cost_account_id,
    type_row.accumulated_account_id,
    type_row.expense_account_id
  ];

  SELECT count(*) INTO journal_lines
  FROM public.gl_journal_lines
  WHERE account_id = ANY(account_ids);

  SELECT count(*) INTO mappings
  FROM public.account_mappings
  WHERE account_id = ANY(account_ids);

  IF to_regclass('public.assets') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_schema = 'public'
         AND table_name = 'assets'
         AND column_name = 'asset_type_id'
     )
  THEN
    EXECUTE 'SELECT count(*) FROM public.assets WHERE asset_type_id = $1'
      INTO total_assets
      USING p_asset_type_id;

    SELECT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name = 'assets' AND column_name = 'active'
    ) INTO has_active_column;

    SELECT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name = 'assets' AND column_name = 'status'
    ) INTO has_status_column;

    IF has_active_column THEN
      EXECUTE 'SELECT count(*) FROM public.assets WHERE asset_type_id = $1 AND active'
        INTO active_assets
        USING p_asset_type_id;
    ELSIF has_status_column THEN
      EXECUTE $query$
        SELECT count(*)
        FROM public.assets
        WHERE asset_type_id = $1
          AND coalesce(status::text, 'active') NOT IN ('archived', 'deleted', 'disposed')
      $query$
        INTO active_assets
        USING p_asset_type_id;
    ELSE
      active_assets := total_assets;
    END IF;
  END IF;

  RETURN jsonb_build_object(
    'active_assets', active_assets,
    'total_assets', total_assets,
    'journal_lines', journal_lines,
    'account_mappings', mappings,
    'has_history', total_assets > 0 OR journal_lines > 0 OR mappings > 0
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.create_asset_type_with_accounts(
  p_org_id UUID,
  p_name TEXT,
  p_category TEXT,
  p_useful_life_months INTEGER,
  p_residual_value_percent NUMERIC DEFAULT 0
)
RETURNS public.asset_types
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  created_type public.asset_types%ROWTYPE;
  names JSONB;
  cost_id UUID := gen_random_uuid();
  accumulated_id UUID := gen_random_uuid();
  expense_id UUID := gen_random_uuid();
BEGIN
  PERFORM public.assert_asset_type_admin(p_org_id);
  names := public.asset_type_account_names(p_name, p_category);

  IF p_useful_life_months IS NULL OR p_useful_life_months NOT BETWEEN 1 AND 1200 THEN
    RAISE EXCEPTION 'Useful life must be between 1 and 1200 months';
  END IF;
  IF p_residual_value_percent IS NULL OR p_residual_value_percent NOT BETWEEN 0 AND 100 THEN
    RAISE EXCEPTION 'Residual value percentage must be between 0 and 100';
  END IF;

  INSERT INTO public.asset_types (
    organisation_id,
    name,
    category,
    useful_life_months,
    residual_value_percent,
    created_by
  )
  VALUES (
    p_org_id,
    btrim(p_name),
    p_category,
    p_useful_life_months,
    p_residual_value_percent,
    auth.uid()
  )
  RETURNING * INTO created_type;

  INSERT INTO public.accounts (
    id,
    organisation_id,
    code,
    name,
    type,
    group_name,
    vat_treatment,
    is_system,
    system_key,
    managed_asset_type_id,
    asset_account_role,
    income_statement_nature
  )
  VALUES
    (
      cost_id,
      p_org_id,
      NULL,
      names->>'cost',
      'asset',
      CASE WHEN p_category = 'tangible' THEN 'Property, Plant and Equipment' ELSE 'Intangible Assets' END,
      'full',
      true,
      'asset_type:' || created_type.id || ':cost',
      created_type.id,
      'cost',
      NULL
    ),
    (
      accumulated_id,
      p_org_id,
      NULL,
      names->>'accumulated',
      'asset',
      CASE WHEN p_category = 'tangible' THEN 'Property, Plant and Equipment' ELSE 'Intangible Assets' END,
      'full',
      true,
      'asset_type:' || created_type.id || ':accumulated',
      created_type.id,
      'accumulated',
      NULL
    ),
    (
      expense_id,
      p_org_id,
      NULL,
      names->>'expense',
      'expense',
      'Depreciation and Amortisation',
      'full',
      true,
      'asset_type:' || created_type.id || ':expense',
      created_type.id,
      'expense',
      'depreciation_amortisation'
    );

  UPDATE public.asset_types
  SET
    cost_account_id = cost_id,
    accumulated_account_id = accumulated_id,
    expense_account_id = expense_id
  WHERE id = created_type.id
  RETURNING * INTO created_type;

  RETURN created_type;
EXCEPTION
  WHEN unique_violation THEN
    RAISE EXCEPTION 'An asset type with this name already exists';
END;
$$;

CREATE OR REPLACE FUNCTION public.update_asset_type_with_accounts(
  p_org_id UUID,
  p_asset_type_id UUID,
  p_name TEXT,
  p_category TEXT,
  p_useful_life_months INTEGER,
  p_residual_value_percent NUMERIC
)
RETURNS public.asset_types
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  current_type public.asset_types%ROWTYPE;
  updated_type public.asset_types%ROWTYPE;
  names JSONB;
BEGIN
  PERFORM public.assert_asset_type_admin(p_org_id);

  SELECT * INTO current_type
  FROM public.asset_types
  WHERE id = p_asset_type_id
    AND organisation_id = p_org_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Asset type not found';
  END IF;
  IF NOT current_type.active THEN
    RAISE EXCEPTION 'Restore the asset type before editing it';
  END IF;

  names := public.asset_type_account_names(p_name, p_category);
  IF p_useful_life_months IS NULL OR p_useful_life_months NOT BETWEEN 1 AND 1200 THEN
    RAISE EXCEPTION 'Useful life must be between 1 and 1200 months';
  END IF;
  IF p_residual_value_percent IS NULL OR p_residual_value_percent NOT BETWEEN 0 AND 100 THEN
    RAISE EXCEPTION 'Residual value percentage must be between 0 and 100';
  END IF;

  UPDATE public.asset_types
  SET
    name = btrim(p_name),
    category = p_category,
    useful_life_months = p_useful_life_months,
    residual_value_percent = p_residual_value_percent
  WHERE id = p_asset_type_id
  RETURNING * INTO updated_type;

  PERFORM set_config('app.asset_type_account_maintenance', 'on', true);

  UPDATE public.accounts
  SET
    name = CASE asset_account_role
      WHEN 'cost' THEN names->>'cost'
      WHEN 'accumulated' THEN names->>'accumulated'
      WHEN 'expense' THEN names->>'expense'
    END,
    type = CASE WHEN asset_account_role = 'expense' THEN 'expense' ELSE 'asset' END,
    income_statement_nature = CASE
      WHEN asset_account_role = 'expense' THEN 'depreciation_amortisation'
      ELSE NULL
    END,
    updated_at = now()
  WHERE managed_asset_type_id = p_asset_type_id;

  RETURN updated_type;
EXCEPTION
  WHEN unique_violation THEN
    RAISE EXCEPTION 'An asset type with this name already exists';
END;
$$;

CREATE OR REPLACE FUNCTION public.preview_asset_type_removal(
  p_org_id UUID,
  p_asset_type_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  type_row public.asset_types%ROWTYPE;
  usage JSONB;
BEGIN
  PERFORM public.assert_asset_type_admin(p_org_id);

  SELECT * INTO type_row
  FROM public.asset_types
  WHERE id = p_asset_type_id
    AND organisation_id = p_org_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Asset type not found';
  END IF;

  usage := public.asset_type_usage(p_asset_type_id);

  RETURN usage || jsonb_build_object(
    'asset_type_id', type_row.id,
    'asset_type_name', type_row.name,
    'action', CASE
      WHEN (usage->>'active_assets')::INTEGER > 0 THEN 'blocked'
      WHEN (usage->>'has_history')::BOOLEAN THEN 'archive'
      ELSE 'delete'
    END
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.remove_asset_type_with_accounts(
  p_org_id UUID,
  p_asset_type_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  type_row public.asset_types%ROWTYPE;
  preview JSONB;
BEGIN
  PERFORM public.assert_asset_type_admin(p_org_id);

  SELECT * INTO type_row
  FROM public.asset_types
  WHERE id = p_asset_type_id
    AND organisation_id = p_org_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Asset type not found';
  END IF;
  IF NOT type_row.active THEN
    RAISE EXCEPTION 'Asset type is already archived';
  END IF;

  preview := public.preview_asset_type_removal(p_org_id, p_asset_type_id);
  IF preview->>'action' = 'blocked' THEN
    RAISE EXCEPTION 'Reassign or remove active assets before removing this asset type';
  END IF;

  PERFORM set_config('app.asset_type_account_maintenance', 'on', true);

  IF preview->>'action' = 'delete' THEN
    UPDATE public.asset_types
    SET
      cost_account_id = NULL,
      accumulated_account_id = NULL,
      expense_account_id = NULL
    WHERE id = p_asset_type_id;

    DELETE FROM public.accounts
    WHERE managed_asset_type_id = p_asset_type_id;

    DELETE FROM public.asset_types
    WHERE id = p_asset_type_id;
  ELSE
    UPDATE public.asset_types
    SET
      active = false,
      archived_at = now(),
      archived_by = auth.uid()
    WHERE id = p_asset_type_id;

    UPDATE public.accounts
    SET active = false, updated_at = now()
    WHERE managed_asset_type_id = p_asset_type_id;
  END IF;

  RETURN preview;
END;
$$;

CREATE OR REPLACE FUNCTION public.restore_asset_type_with_accounts(
  p_org_id UUID,
  p_asset_type_id UUID
)
RETURNS public.asset_types
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  restored_type public.asset_types%ROWTYPE;
BEGIN
  PERFORM public.assert_asset_type_admin(p_org_id);

  SELECT * INTO restored_type
  FROM public.asset_types
  WHERE id = p_asset_type_id
    AND organisation_id = p_org_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Asset type not found';
  END IF;
  IF restored_type.active THEN
    RAISE EXCEPTION 'Asset type is already active';
  END IF;

  PERFORM set_config('app.asset_type_account_maintenance', 'on', true);

  UPDATE public.accounts
  SET active = true, updated_at = now()
  WHERE managed_asset_type_id = p_asset_type_id;

  UPDATE public.asset_types
  SET
    active = true,
    archived_at = NULL,
    archived_by = NULL
  WHERE id = p_asset_type_id
  RETURNING * INTO restored_type;

  RETURN restored_type;
END;
$$;

REVOKE ALL ON FUNCTION public.create_asset_type_with_accounts(UUID, TEXT, TEXT, INTEGER, NUMERIC) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.update_asset_type_with_accounts(UUID, UUID, TEXT, TEXT, INTEGER, NUMERIC) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.preview_asset_type_removal(UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.remove_asset_type_with_accounts(UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.restore_asset_type_with_accounts(UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.asset_type_usage(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.assert_asset_type_admin(UUID) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.create_asset_type_with_accounts(UUID, TEXT, TEXT, INTEGER, NUMERIC)
  TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.update_asset_type_with_accounts(UUID, UUID, TEXT, TEXT, INTEGER, NUMERIC)
  TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.preview_asset_type_removal(UUID, UUID)
  TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.remove_asset_type_with_accounts(UUID, UUID)
  TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.restore_asset_type_with_accounts(UUID, UUID)
  TO authenticated, service_role;

SELECT 'asset_types_managed_accounts_phase_c17_ready' AS migration_note;
