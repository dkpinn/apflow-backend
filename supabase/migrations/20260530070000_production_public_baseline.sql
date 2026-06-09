-- Production public-schema baseline captured from project
-- arueantocclxnziipwdf on 2026-06-08.
--
-- This migration describes schema that already exists in production. Mark its
-- version as applied with `supabase migration repair`; never push it to the
-- production database that it describes.

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


CREATE SCHEMA IF NOT EXISTS "public";


ALTER SCHEMA "public" OWNER TO "pg_database_owner";


COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE TYPE "public"."app_role" AS ENUM (
    'admin',
    'accountant',
    'viewer'
);


ALTER TYPE "public"."app_role" OWNER TO "postgres";


CREATE TYPE "public"."connection_status" AS ENUM (
    'connected',
    'disconnected',
    'expired',
    'error'
);


ALTER TYPE "public"."connection_status" OWNER TO "postgres";


CREATE TYPE "public"."exception_type" AS ENUM (
    'amount_mismatch',
    'missing_invoice',
    'duplicate',
    'unknown_supplier',
    'date_mismatch',
    'banking_mismatch',
    'discount_applied',
    'payment_mismatch',
    'review_required',
    'other'
);


ALTER TYPE "public"."exception_type" OWNER TO "postgres";


CREATE TYPE "public"."match_status" AS ENUM (
    'unmatched',
    'partial',
    'matched',
    'exception'
);


ALTER TYPE "public"."match_status" OWNER TO "postgres";


CREATE TYPE "public"."membership_status" AS ENUM (
    'active',
    'invited',
    'suspended',
    'revoked'
);


ALTER TYPE "public"."membership_status" OWNER TO "postgres";


CREATE TYPE "public"."organisation_role" AS ENUM (
    'owner',
    'admin',
    'accountant',
    'reviewer',
    'viewer',
    'client'
);


ALTER TYPE "public"."organisation_role" OWNER TO "postgres";


CREATE TYPE "public"."parse_status" AS ENUM (
    'pending',
    'processing',
    'completed',
    'failed',
    'needs_split',
    'queued'
);


ALTER TYPE "public"."parse_status" OWNER TO "postgres";


CREATE TYPE "public"."reconciliation_status" AS ENUM (
    'draft',
    'in_progress',
    'completed',
    'exception'
);


ALTER TYPE "public"."reconciliation_status" OWNER TO "postgres";


CREATE TYPE "public"."remittance_status" AS ENUM (
    'draft',
    'queued',
    'sent',
    'delivered',
    'bounced',
    'failed'
);


ALTER TYPE "public"."remittance_status" OWNER TO "postgres";


CREATE TYPE "public"."review_status" AS ENUM (
    'pending',
    'in_review',
    'approved',
    'rejected',
    'needs_info'
);


ALTER TYPE "public"."review_status" OWNER TO "postgres";


CREATE TYPE "public"."send_status" AS ENUM (
    'queued',
    'sent',
    'delivered',
    'failed',
    'bounced'
);


ALTER TYPE "public"."send_status" OWNER TO "postgres";


CREATE TYPE "public"."sync_status" AS ENUM (
    'pending',
    'syncing',
    'synced',
    'failed'
);


ALTER TYPE "public"."sync_status" OWNER TO "postgres";


CREATE TYPE "public"."upload_status" AS ENUM (
    'uploaded',
    'processing',
    'failed',
    'archived'
);


ALTER TYPE "public"."upload_status" OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."assert_asset_type_admin"("p_org_id" "uuid") RETURNS "void"
    LANGUAGE "plpgsql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
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


ALTER FUNCTION "public"."assert_asset_type_admin"("p_org_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."assert_bank_write"("p_org_id" "uuid") RETURNS "void"
    LANGUAGE "plpgsql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND (
       auth.uid() IS NULL
       OR NOT public.has_org_role(
         p_org_id,
         ARRAY['owner','admin','accountant']::public.organisation_role[]
       )
     )
  THEN
    RAISE EXCEPTION 'Only organisation owners, admins, and accountants can delete bank data';
  END IF;
END;
$$;


ALTER FUNCTION "public"."assert_bank_write"("p_org_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."asset_type_account_names"("p_name" "text", "p_category" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" IMMUTABLE
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


ALTER FUNCTION "public"."asset_type_account_names"("p_name" "text", "p_category" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."asset_type_usage"("p_asset_type_id" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $_$
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
$_$;


ALTER FUNCTION "public"."asset_type_usage"("p_asset_type_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."assign_org_inbound_email"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
begin
  if new.inbound_email is null then
    new.inbound_email :=
      'inv-' || substring(replace(new.id::text, '-', '') from 1 for 12) || '@mail.apflow.com';
  end if;
  return new;
end;
$$;


ALTER FUNCTION "public"."assign_org_inbound_email"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."can_read_reporting_group"("_group_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select exists (
    select 1
    from public.reporting_groups rg
    where rg.id = _group_id
      and public.is_org_member(rg.owner_organisation_id)
  )
  or exists (
    select 1
    from public.reporting_group_entities rge
    where rge.reporting_group_id = _group_id
      and public.is_org_member(rge.organisation_id)
  )
  or exists (
    select 1
    from public.reporting_group_users rgu
    where rgu.reporting_group_id = _group_id
      and rgu.user_id = auth.uid()
      and rgu.status = 'active'
  );
$$;


ALTER FUNCTION "public"."can_read_reporting_group"("_group_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."can_write_org"("_user_id" "uuid", "_org_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  SELECT public.has_role(_user_id, _org_id, 'admin')
      OR public.has_role(_user_id, _org_id, 'accountant');
$$;


ALTER FUNCTION "public"."can_write_org"("_user_id" "uuid", "_org_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."can_write_reporting_group"("_group_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select exists (
    select 1
    from public.reporting_groups rg
    where rg.id = _group_id
      and public.has_org_role(rg.owner_organisation_id, array['owner','admin','accountant']::public.organisation_role[])
  )
  or exists (
    select 1
    from public.reporting_group_users rgu
    where rgu.reporting_group_id = _group_id
      and rgu.user_id = auth.uid()
      and rgu.status = 'active'
      and rgu.role in ('owner', 'admin', 'accountant')
  );
$$;


ALTER FUNCTION "public"."can_write_reporting_group"("_group_id" "uuid") OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "public"."asset_types" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "name" "text" NOT NULL,
    "category" "text" NOT NULL,
    "depreciation_method" "text" DEFAULT 'straight_line'::"text" NOT NULL,
    "useful_life_months" integer NOT NULL,
    "residual_value_percent" numeric(5,2) DEFAULT 0 NOT NULL,
    "depreciation_convention" "text" DEFAULT 'in_service_month'::"text" NOT NULL,
    "active" boolean DEFAULT true NOT NULL,
    "archived_at" timestamp with time zone,
    "archived_by" "uuid",
    "cost_account_id" "uuid",
    "accumulated_account_id" "uuid",
    "expense_account_id" "uuid",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "asset_types_accounts_complete_check" CHECK (((("cost_account_id" IS NULL) AND ("accumulated_account_id" IS NULL) AND ("expense_account_id" IS NULL)) OR (("cost_account_id" IS NOT NULL) AND ("accumulated_account_id" IS NOT NULL) AND ("expense_account_id" IS NOT NULL)))),
    CONSTRAINT "asset_types_archive_state_check" CHECK ((("active" AND ("archived_at" IS NULL)) OR ((NOT "active") AND ("archived_at" IS NOT NULL)))),
    CONSTRAINT "asset_types_category_check" CHECK (("category" = ANY (ARRAY['tangible'::"text", 'intangible'::"text"]))),
    CONSTRAINT "asset_types_depreciation_convention_check" CHECK (("depreciation_convention" = 'in_service_month'::"text")),
    CONSTRAINT "asset_types_depreciation_method_check" CHECK (("depreciation_method" = 'straight_line'::"text")),
    CONSTRAINT "asset_types_residual_value_percent_check" CHECK ((("residual_value_percent" >= (0)::numeric) AND ("residual_value_percent" <= (100)::numeric))),
    CONSTRAINT "asset_types_useful_life_months_check" CHECK ((("useful_life_months" >= 1) AND ("useful_life_months" <= 1200)))
);


ALTER TABLE "public"."asset_types" OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."create_asset_type_with_accounts"("p_org_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric DEFAULT 0) RETURNS "public"."asset_types"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
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


ALTER FUNCTION "public"."create_asset_type_with_accounts"("p_org_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."create_org_system_accounts"("p_org_id" "uuid") RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
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


ALTER FUNCTION "public"."create_org_system_accounts"("p_org_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."delete_bank_statement_lines_atomic"("p_org_id" "uuid", "p_line_ids" "uuid"[], "p_actor_user_id" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
DECLARE
  requested_ids UUID[];
  found_count INTEGER;
  blocked JSONB;
  affected_upload_ids UUID[];
  affected_account_ids UUID[];
  deleted_count INTEGER;
  account_id UUID;
BEGIN
  PERFORM public.assert_bank_write(p_org_id);
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND p_actor_user_id IS DISTINCT FROM auth.uid()
  THEN
    RAISE EXCEPTION 'Bank deletion actor does not match the authenticated user';
  END IF;

  SELECT coalesce(array_agg(DISTINCT line_id), ARRAY[]::UUID[])
  INTO requested_ids
  FROM unnest(coalesce(p_line_ids, ARRAY[]::UUID[])) AS ids(line_id)
  WHERE line_id IS NOT NULL;

  IF cardinality(requested_ids) = 0 THEN
    RAISE EXCEPTION 'No bank statement line ids were provided';
  END IF;

  PERFORM 1
  FROM public.bank_statement_lines
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids)
  FOR UPDATE;

  SELECT count(*)
  INTO found_count
  FROM public.bank_statement_lines
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids);

  IF found_count <> cardinality(requested_ids) THEN
    RAISE EXCEPTION 'One or more bank statement lines were not found';
  END IF;

  SELECT jsonb_agg(
    jsonb_build_object(
      'line_id', line.id,
      'description', line.description,
      'posting_status', line.posting_status
    )
    ORDER BY line.id
  )
  INTO blocked
  FROM public.bank_statement_lines line
  WHERE line.organisation_id = p_org_id
    AND line.id = ANY(requested_ids)
    AND EXISTS (
      SELECT 1
      FROM public.gl_journals journal
      WHERE journal.organisation_id = p_org_id
        AND (
          (journal.source_id = line.id AND journal.source_type IN (
            'bank_transaction',
            'bank_transaction_reversal'
          ))
          OR journal.id = line.gl_journal_id
        )
        AND (
          journal.status IN ('posted', 'reversed')
          OR journal.reversal_of_journal_id IS NOT NULL
        )
    );

  IF blocked IS NOT NULL THEN
    RAISE EXCEPTION USING
      MESSAGE = 'Bank statement deletion blocked by posted or reversed journal history',
      DETAIL = blocked::TEXT,
      ERRCODE = 'P0001';
  END IF;

  SELECT
    array_agg(DISTINCT bank_statement_upload_id),
    array_agg(DISTINCT bank_account_id)
  INTO affected_upload_ids, affected_account_ids
  FROM public.bank_statement_lines
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids);

  DELETE FROM public.gl_journals journal
  WHERE journal.organisation_id = p_org_id
    AND journal.status = 'draft'
    AND journal.source_type = 'bank_transaction'
    AND (
      journal.source_id = ANY(requested_ids)
      OR journal.id IN (
        SELECT line.gl_journal_id
        FROM public.bank_statement_lines line
        WHERE line.organisation_id = p_org_id
          AND line.id = ANY(requested_ids)
          AND line.gl_journal_id IS NOT NULL
      )
    );

  DELETE FROM public.bank_statement_lines
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids);
  GET DIAGNOSTICS deleted_count = ROW_COUNT;

  UPDATE public.bank_statement_uploads upload
  SET
    extracted_line_count = counts.line_count,
    duplicate_line_count = counts.duplicate_count,
    updated_at = now()
  FROM (
    SELECT
      upload_id,
      count(line.id)::INTEGER AS line_count,
      count(line.id) FILTER (
        WHERE line.duplicate_status IN ('possible_duplicate', 'duplicate')
      )::INTEGER AS duplicate_count
    FROM unnest(affected_upload_ids) AS ids(upload_id)
    LEFT JOIN public.bank_statement_lines line
      ON line.bank_statement_upload_id = upload_id
    GROUP BY upload_id
  ) counts
  WHERE upload.id = counts.upload_id
    AND upload.organisation_id = p_org_id;

  PERFORM public.refresh_bank_account_statement_state(
    p_org_id,
    affected_account_ids
  );

  FOREACH account_id IN ARRAY affected_account_ids
  LOOP
    INSERT INTO public.bank_audit_events (
      organisation_id,
      bank_account_id,
      event_type,
      actor_user_id,
      details
    )
    VALUES (
      p_org_id,
      account_id,
      'bank_lines_deleted',
      coalesce(auth.uid(), p_actor_user_id),
      jsonb_build_object(
        'line_ids', to_jsonb(requested_ids),
        'deleted_count', deleted_count
      )
    );
  END LOOP;

  RETURN jsonb_build_object(
    'deleted_count', deleted_count,
    'line_ids', to_jsonb(requested_ids),
    'affected_upload_ids', to_jsonb(affected_upload_ids),
    'affected_account_ids', to_jsonb(affected_account_ids)
  );
END;
$$;


ALTER FUNCTION "public"."delete_bank_statement_lines_atomic"("p_org_id" "uuid", "p_line_ids" "uuid"[], "p_actor_user_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."delete_bank_statement_uploads_atomic"("p_org_id" "uuid", "p_upload_ids" "uuid"[], "p_actor_user_id" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
DECLARE
  requested_ids UUID[];
  found_count INTEGER;
  blocked JSONB;
  affected_account_ids UUID[];
  line_ids UUID[];
  files JSONB;
  deleted_count INTEGER;
  account_id UUID;
BEGIN
  PERFORM public.assert_bank_write(p_org_id);
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND p_actor_user_id IS DISTINCT FROM auth.uid()
  THEN
    RAISE EXCEPTION 'Bank deletion actor does not match the authenticated user';
  END IF;

  SELECT coalesce(array_agg(DISTINCT upload_id), ARRAY[]::UUID[])
  INTO requested_ids
  FROM unnest(coalesce(p_upload_ids, ARRAY[]::UUID[])) AS ids(upload_id)
  WHERE upload_id IS NOT NULL;

  IF cardinality(requested_ids) = 0 THEN
    RAISE EXCEPTION 'No bank statement upload ids were provided';
  END IF;

  PERFORM 1
  FROM public.bank_statement_uploads
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids)
  FOR UPDATE;

  SELECT count(*)
  INTO found_count
  FROM public.bank_statement_uploads
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids);

  IF found_count <> cardinality(requested_ids) THEN
    RAISE EXCEPTION 'One or more bank statement uploads were not found';
  END IF;

  SELECT jsonb_agg(
    jsonb_build_object(
      'upload_id', upload.id,
      'filename', upload.original_filename,
      'blocked_line_ids', blocked_lines.line_ids
    )
    ORDER BY upload.id
  )
  INTO blocked
  FROM public.bank_statement_uploads upload
  JOIN LATERAL (
    SELECT jsonb_agg(line.id ORDER BY line.id) AS line_ids
    FROM public.bank_statement_lines line
    WHERE line.bank_statement_upload_id = upload.id
      AND EXISTS (
        SELECT 1
        FROM public.gl_journals journal
        WHERE journal.organisation_id = p_org_id
          AND (
            (journal.source_id = line.id AND journal.source_type IN (
              'bank_transaction',
              'bank_transaction_reversal'
            ))
            OR journal.id = line.gl_journal_id
          )
          AND (
            journal.status IN ('posted', 'reversed')
            OR journal.reversal_of_journal_id IS NOT NULL
          )
      )
  ) blocked_lines ON blocked_lines.line_ids IS NOT NULL
  WHERE upload.organisation_id = p_org_id
    AND upload.id = ANY(requested_ids);

  IF blocked IS NOT NULL THEN
    RAISE EXCEPTION USING
      MESSAGE = 'Bank statement deletion blocked by posted or reversed journal history',
      DETAIL = blocked::TEXT,
      ERRCODE = 'P0001';
  END IF;

  SELECT
    array_agg(DISTINCT bank_account_id),
    coalesce(
      jsonb_agg(
        jsonb_build_object(
          'upload_id', id,
          'storage_bucket', storage_bucket,
          'storage_path', storage_path
        )
        ORDER BY id
      ),
      '[]'::JSONB
    )
  INTO affected_account_ids, files
  FROM public.bank_statement_uploads
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids);

  SELECT coalesce(array_agg(id), ARRAY[]::UUID[])
  INTO line_ids
  FROM public.bank_statement_lines
  WHERE organisation_id = p_org_id
    AND bank_statement_upload_id = ANY(requested_ids);

  IF cardinality(line_ids) > 0 THEN
    DELETE FROM public.gl_journals journal
    WHERE journal.organisation_id = p_org_id
      AND journal.status = 'draft'
      AND journal.source_type = 'bank_transaction'
      AND (
        journal.source_id = ANY(line_ids)
        OR journal.id IN (
          SELECT line.gl_journal_id
          FROM public.bank_statement_lines line
          WHERE line.organisation_id = p_org_id
            AND line.id = ANY(line_ids)
            AND line.gl_journal_id IS NOT NULL
        )
      );
  END IF;

  DELETE FROM public.bank_statement_uploads
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids);
  GET DIAGNOSTICS deleted_count = ROW_COUNT;

  PERFORM public.refresh_bank_account_statement_state(
    p_org_id,
    affected_account_ids
  );

  FOREACH account_id IN ARRAY affected_account_ids
  LOOP
    INSERT INTO public.bank_audit_events (
      organisation_id,
      bank_account_id,
      event_type,
      actor_user_id,
      details
    )
    VALUES (
      p_org_id,
      account_id,
      'bank_statement_uploads_deleted',
      coalesce(auth.uid(), p_actor_user_id),
      jsonb_build_object(
        'upload_ids', to_jsonb(requested_ids),
        'deleted_count', deleted_count,
        'files', files
      )
    );
  END LOOP;

  RETURN jsonb_build_object(
    'deleted_count', deleted_count,
    'upload_ids', to_jsonb(requested_ids),
    'affected_account_ids', to_jsonb(affected_account_ids),
    'files', files
  );
END;
$$;


ALTER FUNCTION "public"."delete_bank_statement_uploads_atomic"("p_org_id" "uuid", "p_upload_ids" "uuid"[], "p_actor_user_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_bank_account_balance_summary"("p_org_id" "uuid", "p_bank_account_id" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
DECLARE
  account_row public.bank_accounts%ROWTYPE;
  latest_upload RECORD;
  imported_balance NUMERIC(14,2);
  tb_balance NUMERIC(14,2);
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND (
       auth.uid() IS NULL
       OR NOT public.is_org_member(p_org_id)
     )
  THEN
    RAISE EXCEPTION 'You do not have access to this organisation';
  END IF;

  SELECT *
  INTO account_row
  FROM public.bank_accounts
  WHERE id = p_bank_account_id
    AND organisation_id = p_org_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Bank account not found';
  END IF;

  SELECT
    upload.id,
    upload.statement_period_to,
    upload.opening_balance,
    upload.closing_balance,
    upload.uploaded_at,
    (
      SELECT max(line.line_date)
      FROM public.bank_statement_lines line
      WHERE line.bank_statement_upload_id = upload.id
    ) AS latest_transaction_date
  INTO latest_upload
  FROM public.bank_statement_uploads upload
  WHERE upload.organisation_id = p_org_id
    AND upload.bank_account_id = p_bank_account_id
    AND upload.extraction_status = 'extracted'
  ORDER BY
    coalesce(
      upload.statement_period_to,
      (
        SELECT max(line.line_date)
        FROM public.bank_statement_lines line
        WHERE line.bank_statement_upload_id = upload.id
      )
    ) DESC NULLS LAST,
    (
      SELECT max(line.line_date)
      FROM public.bank_statement_lines line
      WHERE line.bank_statement_upload_id = upload.id
    ) DESC NULLS LAST,
    upload.uploaded_at DESC,
    upload.id DESC
  LIMIT 1;

  IF latest_upload.id IS NOT NULL
     AND latest_upload.opening_balance IS NOT NULL
  THEN
    SELECT round(
      latest_upload.opening_balance + coalesce(sum(line.signed_amount), 0),
      2
    )
    INTO imported_balance
    FROM public.bank_statement_lines line
    WHERE line.organisation_id = p_org_id
      AND line.bank_statement_upload_id = latest_upload.id;
  END IF;

  IF account_row.gl_account_id IS NOT NULL THEN
    SELECT round(
      coalesce(sum(line.debit_amount - line.credit_amount), 0),
      2
    )
    INTO tb_balance
    FROM public.gl_journal_lines line
    JOIN public.gl_journals journal
      ON journal.id = line.gl_journal_id
     AND journal.organisation_id = p_org_id
     AND journal.status = 'posted'
    WHERE line.organisation_id = p_org_id
      AND line.account_id = account_row.gl_account_id;
  END IF;

  RETURN jsonb_build_object(
    'bank_statement_balance', latest_upload.closing_balance,
    'calculated_imported_balance', imported_balance,
    'current_tb_balance', tb_balance,
    'latest_statement_upload_id', latest_upload.id,
    'statement_period_to', latest_upload.statement_period_to,
    'latest_transaction_date', latest_upload.latest_transaction_date,
    'bank_balance_status', CASE
      WHEN latest_upload.closing_balance IS NULL THEN 'unavailable'
      ELSE 'available'
    END,
    'imported_balance_status', CASE
      WHEN imported_balance IS NULL THEN 'unavailable'
      ELSE 'available'
    END,
    'tb_balance_status', CASE
      WHEN account_row.gl_account_id IS NULL THEN 'gl_account_not_linked'
      ELSE 'available'
    END
  );
END;
$$;


ALTER FUNCTION "public"."get_bank_account_balance_summary"("p_org_id" "uuid", "p_bank_account_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."has_org_role"("_org_id" "uuid", "_roles" "public"."organisation_role"[]) RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select exists (
    select 1 from public.organisation_users
    where organisation_id = _org_id
      and user_id = auth.uid()
      and status = 'active'
      and role = any(_roles)
  );
$$;


ALTER FUNCTION "public"."has_org_role"("_org_id" "uuid", "_roles" "public"."organisation_role"[]) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."has_role"("_user_id" "uuid", "_org_id" "uuid", "_role" "public"."app_role") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.user_roles
    WHERE user_id = _user_id AND organisation_id = _org_id AND role = _role
  );
$$;


ALTER FUNCTION "public"."has_role"("_user_id" "uuid", "_org_id" "uuid", "_role" "public"."app_role") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."is_member_of"("_user_id" "uuid", "_org_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.user_organisations
    WHERE user_id = _user_id AND organisation_id = _org_id
  );
$$;


ALTER FUNCTION "public"."is_member_of"("_user_id" "uuid", "_org_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."is_org_member"("_org_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select exists (
    select 1 from public.organisation_users
    where organisation_id = _org_id
      and user_id = auth.uid()
      and status = 'active'
  );
$$;


ALTER FUNCTION "public"."is_org_member"("_org_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."is_valid_auto_link_amount_tiers"("value" "jsonb") RETURNS boolean
    LANGUAGE "plpgsql" IMMUTABLE
    SET "search_path" TO 'public'
    AS $_$
DECLARE
  item JSONB;
  catch_all_count INTEGER := 0;
  seen_amounts NUMERIC[] := ARRAY[]::NUMERIC[];
  max_amount NUMERIC;
  required_matches INTEGER;
BEGIN
  IF jsonb_typeof(value) <> 'array' THEN
    RETURN false;
  END IF;

  FOR item IN SELECT * FROM jsonb_array_elements(value)
  LOOP
    IF jsonb_typeof(item) <> 'object'
       OR NOT (item ? 'max_amount')
       OR NOT (item ? 'required_matches')
       OR item - 'max_amount' - 'required_matches' <> '{}'::JSONB
       OR jsonb_typeof(item -> 'required_matches') <> 'number'
       OR (item ->> 'required_matches') !~ '^[0-9]+$'
    THEN
      RETURN false;
    END IF;

    required_matches := (item ->> 'required_matches')::INTEGER;
    IF required_matches NOT BETWEEN 1 AND 4 THEN
      RETURN false;
    END IF;

    IF jsonb_typeof(item -> 'max_amount') = 'null' THEN
      catch_all_count := catch_all_count + 1;
      IF catch_all_count > 1 THEN
        RETURN false;
      END IF;
    ELSIF jsonb_typeof(item -> 'max_amount') = 'number' THEN
      max_amount := (item ->> 'max_amount')::NUMERIC;
      IF max_amount = 'NaN'::NUMERIC
         OR max_amount < 0
         OR array_position(seen_amounts, max_amount) IS NOT NULL
      THEN
        RETURN false;
      END IF;
      seen_amounts := array_append(seen_amounts, max_amount);
    ELSE
      RETURN false;
    END IF;
  END LOOP;

  RETURN true;
EXCEPTION
  WHEN OTHERS THEN
    RETURN false;
END;
$_$;


ALTER FUNCTION "public"."is_valid_auto_link_amount_tiers"("value" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."on_organisation_created"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
  PERFORM public.create_org_system_accounts(NEW.id);
  RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."on_organisation_created"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."post_invoice_to_gl_atomic"("p_org_id" "uuid", "p_invoice_id" "uuid", "p_user_id" "uuid", "p_journal_date" "date", "p_description" "text", "p_total" numeric, "p_lines" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
DECLARE
  invoice_row public.invoices_extracted%ROWTYPE;
  journal_id UUID := gen_random_uuid();
  line_count INTEGER;
  debit_total NUMERIC(14,2);
  credit_total NUMERIC(14,2);
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    IF auth.uid() IS NULL
       OR p_user_id IS DISTINCT FROM auth.uid()
       OR NOT public.has_org_role(
         p_org_id,
         ARRAY['owner','admin','accountant']::public.organisation_role[]
       )
    THEN
      RAISE EXCEPTION 'Not authorised to post this invoice';
    END IF;
  END IF;

  SELECT *
  INTO invoice_row
  FROM public.invoices_extracted
  WHERE id = p_invoice_id
    AND organisation_id = p_org_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Invoice not found';
  END IF;
  IF invoice_row.posting_status = 'posted' THEN
    RAISE EXCEPTION 'Invoice has already been posted to GL';
  END IF;
  IF invoice_row.supplier_id IS NOT NULL
     AND nullif(btrim(invoice_row.invoice_number), '') IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM public.invoices_extracted duplicate_invoice
       WHERE duplicate_invoice.organisation_id = p_org_id
         AND duplicate_invoice.supplier_id = invoice_row.supplier_id
         AND duplicate_invoice.invoice_number = invoice_row.invoice_number
         AND duplicate_invoice.id <> p_invoice_id
     )
  THEN
    RAISE EXCEPTION 'Duplicate invoices cannot be posted to GL';
  END IF;
  IF p_total IS NULL OR p_total <= 0 THEN
    RAISE EXCEPTION 'Invoice total must be positive';
  END IF;
  IF abs(
    round(coalesce(invoice_row.subtotal, 0) + coalesce(invoice_row.tax_amount, 0), 2)
    - round(p_total, 2)
  ) > 0.02 THEN
    RAISE EXCEPTION 'Invoice totals changed after journal preparation';
  END IF;
  IF jsonb_typeof(p_lines) <> 'array' OR jsonb_array_length(p_lines) < 2 THEN
    RAISE EXCEPTION 'A balanced journal requires at least two lines';
  END IF;

  SELECT
    count(*),
    round(coalesce(sum(debit_amount), 0), 2),
    round(coalesce(sum(credit_amount), 0), 2)
  INTO line_count, debit_total, credit_total
  FROM jsonb_to_recordset(p_lines) AS line(
    account_id UUID,
    description TEXT,
    debit_amount NUMERIC,
    credit_amount NUMERIC,
    tracking JSONB,
    sort_order INTEGER
  );

  IF debit_total <> credit_total OR debit_total <> round(p_total, 2) THEN
    RAISE EXCEPTION 'Journal lines do not balance to the invoice total';
  END IF;

  INSERT INTO public.gl_journals (
    id,
    organisation_id,
    source_type,
    source_id,
    journal_date,
    description,
    status,
    total_debit,
    total_credit,
    created_by,
    posted_by,
    posted_at
  )
  VALUES (
    journal_id,
    p_org_id,
    'invoice',
    p_invoice_id,
    coalesce(p_journal_date, current_date),
    p_description,
    'posted',
    debit_total,
    credit_total,
    p_user_id,
    p_user_id,
    now()
  );

  INSERT INTO public.gl_journal_lines (
    organisation_id,
    gl_journal_id,
    account_id,
    description,
    debit_amount,
    credit_amount,
    tracking,
    sort_order
  )
  SELECT
    p_org_id,
    journal_id,
    line.account_id,
    line.description,
    coalesce(line.debit_amount, 0),
    coalesce(line.credit_amount, 0),
    coalesce(line.tracking, '{}'::JSONB),
    coalesce(line.sort_order, 0)
  FROM jsonb_to_recordset(p_lines) AS line(
    account_id UUID,
    description TEXT,
    debit_amount NUMERIC,
    credit_amount NUMERIC,
    tracking JSONB,
    sort_order INTEGER
  );

  UPDATE public.invoices_extracted
  SET
    gl_journal_id = journal_id,
    posting_status = 'posted',
    posted_at = now(),
    posted_by = p_user_id,
    approval_status = 'approved',
    review_status = 'approved',
    approved_at = now(),
    approved_by = p_user_id,
    updated_at = now()
  WHERE id = p_invoice_id
    AND organisation_id = p_org_id;

  RETURN jsonb_build_object(
    'journal_id', journal_id,
    'total_debit', debit_total,
    'total_credit', credit_total,
    'lines', line_count
  );
END;
$$;


ALTER FUNCTION "public"."post_invoice_to_gl_atomic"("p_org_id" "uuid", "p_invoice_id" "uuid", "p_user_id" "uuid", "p_journal_date" "date", "p_description" "text", "p_total" numeric, "p_lines" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."prevent_duplicate_org_name_for_user"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
declare
  duplicate_org_name text;
begin
  -- If no authenticated user context exists, do not block.
  -- This avoids breaking admin/service maintenance scripts.
  if auth.uid() is null then
    return new;
  end if;

  select o.name
  into duplicate_org_name
  from public.organisations o
  join public.organisation_users ou
    on ou.organisation_id = o.id
  where ou.user_id = auth.uid()
    and ou.status = 'active'
    and o.id <> new.id
    and lower(trim(o.name)) = lower(trim(new.name))
  limit 1;

  if duplicate_org_name is not null then
    raise exception 'You already have access to another organisation named "%". Please use a unique organisation name.', new.name;
  end if;

  return new;
end;
$$;


ALTER FUNCTION "public"."prevent_duplicate_org_name_for_user"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."prevent_system_account_delete"() RETURNS "trigger"
    LANGUAGE "plpgsql"
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


ALTER FUNCTION "public"."prevent_system_account_delete"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."preview_asset_type_removal"("p_org_id" "uuid", "p_asset_type_id" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
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


ALTER FUNCTION "public"."preview_asset_type_removal"("p_org_id" "uuid", "p_asset_type_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."protect_last_owner"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
declare
  remaining_owners integer;
  target_org uuid;
begin
  -- Only act when the change would remove an active owner.
  if (tg_op = 'UPDATE') then
    target_org := old.organisation_id;
    if old.role = 'owner'::public.organisation_role
       and old.status = 'active'::public.membership_status
       and (
         new.role <> 'owner'::public.organisation_role
         or new.status <> 'active'::public.membership_status
       )
    then
      select count(*) into remaining_owners
      from public.organisation_users
      where organisation_id = target_org
        and role = 'owner'::public.organisation_role
        and status = 'active'::public.membership_status
        and id <> old.id;

      if remaining_owners = 0 then
        raise exception 'Cannot demote or suspend the last active owner of this organisation';
      end if;
    end if;
  elsif (tg_op = 'DELETE') then
    target_org := old.organisation_id;
    if old.role = 'owner'::public.organisation_role
       and old.status = 'active'::public.membership_status
    then
      select count(*) into remaining_owners
      from public.organisation_users
      where organisation_id = target_org
        and role = 'owner'::public.organisation_role
        and status = 'active'::public.membership_status
        and id <> old.id;

      if remaining_owners = 0 then
        raise exception 'Cannot remove the last active owner of this organisation';
      end if;
    end if;
  end if;

  if (tg_op = 'DELETE') then
    return old;
  end if;
  return new;
end;
$$;


ALTER FUNCTION "public"."protect_last_owner"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."protect_system_accounts"() RETURNS "trigger"
    LANGUAGE "plpgsql"
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


ALTER FUNCTION "public"."protect_system_accounts"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."refresh_bank_account_statement_state"("p_org_id" "uuid", "p_bank_account_ids" "uuid"[]) RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
DECLARE
  account_id UUID;
  latest_upload RECORD;
BEGIN
  FOREACH account_id IN ARRAY coalesce(p_bank_account_ids, ARRAY[]::UUID[])
  LOOP
    SELECT
      upload.id,
      upload.closing_balance
    INTO latest_upload
    FROM public.bank_statement_uploads upload
    WHERE upload.organisation_id = p_org_id
      AND upload.bank_account_id = account_id
      AND upload.extraction_status = 'extracted'
    ORDER BY
      coalesce(
        upload.statement_period_to,
        (
          SELECT max(line.line_date)
          FROM public.bank_statement_lines line
          WHERE line.bank_statement_upload_id = upload.id
        )
      ) DESC NULLS LAST,
      (
        SELECT max(line.line_date)
        FROM public.bank_statement_lines line
        WHERE line.bank_statement_upload_id = upload.id
      ) DESC NULLS LAST,
      upload.uploaded_at DESC,
      upload.id DESC
    LIMIT 1;

    IF FOUND THEN
      UPDATE public.bank_accounts
      SET
        last_statement_upload_id = latest_upload.id,
        current_reconciled_balance = coalesce(
          latest_upload.closing_balance,
          opening_balance
        ),
        updated_at = now()
      WHERE id = account_id
        AND organisation_id = p_org_id;
    ELSE
      UPDATE public.bank_accounts
      SET
        last_statement_upload_id = NULL,
        current_reconciled_balance = opening_balance,
        updated_at = now()
      WHERE id = account_id
        AND organisation_id = p_org_id;
    END IF;
  END LOOP;
END;
$$;


ALTER FUNCTION "public"."refresh_bank_account_statement_state"("p_org_id" "uuid", "p_bank_account_ids" "uuid"[]) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."remove_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
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


ALTER FUNCTION "public"."remove_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."restore_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") RETURNS "public"."asset_types"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
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


ALTER FUNCTION "public"."restore_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."set_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
begin new.updated_at = now(); return new; end $$;


ALTER FUNCTION "public"."set_updated_at"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."storage_object_org_id"("_name" "text") RETURNS "uuid"
    LANGUAGE "plpgsql" IMMUTABLE
    AS $_$
declare
  _first_segment text;
begin
  _first_segment := split_part(coalesce(_name, ''), '/', 1);
  if _first_segment ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    return _first_segment::uuid;
  end if;
  return null;
end;
$_$;


ALTER FUNCTION "public"."storage_object_org_id"("_name" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."update_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) RETURNS "public"."asset_types"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
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


ALTER FUNCTION "public"."update_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."update_updated_at_column"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'public'
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."update_updated_at_column"() OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."account_budgets" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "account_id" "uuid" NOT NULL,
    "tracking_value_id" "uuid",
    "period_start" "date" NOT NULL,
    "period_end" "date" NOT NULL,
    "amount" numeric(14,2) DEFAULT 0 NOT NULL,
    "notes" "text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "account_budgets_amount_check" CHECK (("amount" >= (0)::numeric)),
    CONSTRAINT "account_budgets_period_check" CHECK (("period_start" <= "period_end"))
);


ALTER TABLE "public"."account_budgets" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."account_mappings" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "account_id" "uuid" NOT NULL,
    "integration_id" "uuid" NOT NULL,
    "external_code" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."account_mappings" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."accounts" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "code" "text",
    "name" "text" NOT NULL,
    "type" "text" DEFAULT 'expense'::"text" NOT NULL,
    "group_name" "text",
    "description" "text",
    "active" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "vat_treatment" "text" DEFAULT 'full'::"text" NOT NULL,
    "is_system" boolean DEFAULT false NOT NULL,
    "system_key" "text",
    "income_statement_nature" "text",
    "default_income_statement_function" "text",
    "special_report_classification" "text" DEFAULT 'none'::"text" NOT NULL,
    "managed_asset_type_id" "uuid",
    "asset_account_role" "text",
    CONSTRAINT "accounts_asset_account_role_check" CHECK ((("asset_account_role" IS NULL) OR ("asset_account_role" = ANY (ARRAY['cost'::"text", 'accumulated'::"text", 'expense'::"text"])))),
    CONSTRAINT "accounts_asset_management_pair_check" CHECK (((("managed_asset_type_id" IS NULL) AND ("asset_account_role" IS NULL)) OR (("managed_asset_type_id" IS NOT NULL) AND ("asset_account_role" IS NOT NULL) AND "is_system"))),
    CONSTRAINT "accounts_default_income_statement_function_check" CHECK ((("default_income_statement_function" IS NULL) OR ("default_income_statement_function" = ANY (ARRAY['cogs'::"text", 'selling'::"text", 'g_and_a'::"text", 'r_and_d'::"text", 'other_operating'::"text"])))),
    CONSTRAINT "accounts_income_statement_nature_check" CHECK ((("income_statement_nature" IS NULL) OR ("income_statement_nature" = ANY (ARRAY['revenue'::"text", 'changes_in_inventories'::"text", 'raw_materials_consumables'::"text", 'employee_benefits'::"text", 'depreciation_amortisation'::"text", 'other_operating_expenses'::"text", 'other_operating_income'::"text"])))),
    CONSTRAINT "accounts_special_report_classification_check" CHECK (("special_report_classification" = ANY (ARRAY['none'::"text", 'finance_cost'::"text", 'associate_profit'::"text", 'discontinued_operations'::"text", 'extraordinary'::"text"]))),
    CONSTRAINT "accounts_type_check" CHECK (("type" = ANY (ARRAY['income'::"text", 'expense'::"text", 'asset'::"text", 'liability'::"text", 'equity'::"text", 'other'::"text"]))),
    CONSTRAINT "accounts_vat_treatment_check" CHECK (("vat_treatment" = ANY (ARRAY['full'::"text", 'blocked'::"text", 'exempt'::"text", 'zero_rated'::"text"])))
);


ALTER TABLE "public"."accounts" OWNER TO "postgres";


COMMENT ON COLUMN "public"."accounts"."vat_treatment" IS 'full=standard claimable input VAT | blocked=S17 no claim (entertainment etc) |
   exempt=supplier not VAT registered | zero_rated=0% VAT supply';



COMMENT ON COLUMN "public"."accounts"."income_statement_nature" IS 'Nature classification used for Income Statement presentation by nature.';



COMMENT ON COLUMN "public"."accounts"."default_income_statement_function" IS 'Fallback Function classification when no mapped function driver tracking value exists.';



COMMENT ON COLUMN "public"."accounts"."special_report_classification" IS 'Special Income Statement placement outside normal operating grouping.';



CREATE TABLE IF NOT EXISTS "public"."audit_log" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "user_id" "uuid",
    "entity_type" "text" NOT NULL,
    "entity_id" "uuid",
    "action_type" "text" NOT NULL,
    "action_summary" "text",
    "metadata_json" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."audit_log" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."bank_accounts" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "name" "text" NOT NULL,
    "institution_name" "text",
    "account_type" "text" DEFAULT 'bank'::"text" NOT NULL,
    "currency" "text" DEFAULT 'ZAR'::"text" NOT NULL,
    "account_number_mask" "text",
    "account_number_hash" "text",
    "gl_account_id" "uuid",
    "opening_balance" numeric(14,2) DEFAULT 0 NOT NULL,
    "current_reconciled_balance" numeric(14,2) DEFAULT 0 NOT NULL,
    "last_statement_upload_id" "uuid",
    "active" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "bank_accounts_account_type_check" CHECK (("account_type" = ANY (ARRAY['bank'::"text", 'cash'::"text", 'credit_card'::"text", 'loan'::"text", 'mortgage'::"text", 'vehicle_finance'::"text", 'investment'::"text", 'call_account'::"text", 'money_market'::"text", 'paypal'::"text", 'paygate'::"text", 'crypto'::"text", 'foreign_bank'::"text", 'other'::"text"])))
);


ALTER TABLE "public"."bank_accounts" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."bank_audit_events" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "bank_account_id" "uuid",
    "bank_statement_upload_id" "uuid",
    "bank_statement_line_id" "uuid",
    "gl_journal_id" "uuid",
    "event_type" "text" NOT NULL,
    "actor_user_id" "uuid",
    "actor_type" "text" DEFAULT 'user'::"text" NOT NULL,
    "details" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."bank_audit_events" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."bank_parsing_rules" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "institution_name" "text",
    "account_type" "text",
    "parsing_hint" "text" NOT NULL,
    "active" boolean DEFAULT true NOT NULL,
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "bank_parsing_rules_account_type_check" CHECK ((("account_type" IS NULL) OR ("account_type" = ANY (ARRAY['bank'::"text", 'cash'::"text", 'credit_card'::"text", 'loan'::"text", 'mortgage'::"text", 'vehicle_finance'::"text", 'investment'::"text", 'call_account'::"text", 'money_market'::"text", 'paypal'::"text", 'paygate'::"text", 'crypto'::"text", 'foreign_bank'::"text", 'other'::"text"]))))
);


ALTER TABLE "public"."bank_parsing_rules" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."bank_statement_lines" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "bank_account_id" "uuid" NOT NULL,
    "bank_statement_upload_id" "uuid" NOT NULL,
    "line_date" "date",
    "value_date" "date",
    "description" "text",
    "reference" "text",
    "counterparty" "text",
    "debit_amount" numeric(14,2) DEFAULT 0 NOT NULL,
    "credit_amount" numeric(14,2) DEFAULT 0 NOT NULL,
    "signed_amount" numeric(14,2) DEFAULT 0 NOT NULL,
    "balance_amount" numeric(14,2),
    "currency" "text",
    "transaction_hash" "text" NOT NULL,
    "duplicate_status" "text" DEFAULT 'clear'::"text" NOT NULL,
    "match_status" "text" DEFAULT 'unmatched'::"text" NOT NULL,
    "allocation_status" "text" DEFAULT 'unallocated'::"text" NOT NULL,
    "posting_status" "text" DEFAULT 'unposted'::"text" NOT NULL,
    "accepted_suggestion_id" "uuid",
    "accepted_rule_id" "uuid",
    "gl_journal_id" "uuid",
    "review_status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "reviewed_by" "uuid",
    "reviewed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "transaction_type" "text",
    "bank_reference" "text",
    "raw_text" "text",
    "raw_lines" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "source_page" integer,
    "source_row_index" integer,
    "extraction_confidence" numeric(5,4),
    "extraction_warnings" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "supplier_id" "uuid",
    CONSTRAINT "bank_statement_lines_allocation_status_check" CHECK (("allocation_status" = ANY (ARRAY['unallocated'::"text", 'suggested'::"text", 'allocated'::"text", 'split'::"text"]))),
    CONSTRAINT "bank_statement_lines_duplicate_status_check" CHECK (("duplicate_status" = ANY (ARRAY['clear'::"text", 'possible_duplicate'::"text", 'duplicate'::"text"]))),
    CONSTRAINT "bank_statement_lines_match_status_check" CHECK (("match_status" = ANY (ARRAY['unmatched'::"text", 'suggested'::"text", 'matched'::"text", 'exception'::"text", 'ignored'::"text"]))),
    CONSTRAINT "bank_statement_lines_posting_status_check" CHECK (("posting_status" = ANY (ARRAY['unposted'::"text", 'draft'::"text", 'posted'::"text", 'reversed'::"text"]))),
    CONSTRAINT "bank_statement_lines_review_status_check" CHECK (("review_status" = ANY (ARRAY['pending'::"text", 'reviewed'::"text", 'approved'::"text", 'ignored'::"text"])))
);


ALTER TABLE "public"."bank_statement_lines" OWNER TO "postgres";


COMMENT ON COLUMN "public"."bank_statement_lines"."supplier_id" IS 'Supplier linked to this bank transaction during reconciliation review.';



CREATE TABLE IF NOT EXISTS "public"."bank_statement_uploads" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "bank_account_id" "uuid" NOT NULL,
    "original_filename" "text" NOT NULL,
    "mime_type" "text",
    "storage_bucket" "text" DEFAULT 'statement-files'::"text" NOT NULL,
    "storage_path" "text" NOT NULL,
    "file_sha256" "text",
    "source_type" "text" DEFAULT 'upload'::"text" NOT NULL,
    "statement_period_from" "date",
    "statement_period_to" "date",
    "opening_balance" numeric(14,2),
    "closing_balance" numeric(14,2),
    "extracted_line_count" integer DEFAULT 0 NOT NULL,
    "duplicate_line_count" integer DEFAULT 0 NOT NULL,
    "balance_status" "text" DEFAULT 'unchecked'::"text" NOT NULL,
    "duplicate_status" "text" DEFAULT 'unchecked'::"text" NOT NULL,
    "extraction_status" "text" DEFAULT 'uploaded'::"text" NOT NULL,
    "confidence_score" numeric(5,4),
    "duplicate_summary" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "extraction_evidence" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "uploaded_by" "uuid",
    "uploaded_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "extracted_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "extractor_type" "text" DEFAULT 'bank_statement'::"text" NOT NULL,
    "extractor_version" "text" DEFAULT 'v1'::"text" NOT NULL,
    "source_format" "text",
    "raw_extraction" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "extraction_warnings" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "extraction_input_tokens" integer,
    "extraction_output_tokens" integer,
    "extraction_model" "text",
    "extraction_cost_usd" numeric(10,6),
    CONSTRAINT "bank_statement_uploads_balance_status_check" CHECK (("balance_status" = ANY (ARRAY['unchecked'::"text", 'balanced'::"text", 'opening_mismatch'::"text", 'closing_mismatch'::"text", 'missing_balance'::"text"]))),
    CONSTRAINT "bank_statement_uploads_duplicate_status_check" CHECK (("duplicate_status" = ANY (ARRAY['unchecked'::"text", 'clear'::"text", 'possible_duplicates'::"text", 'duplicate_file'::"text"]))),
    CONSTRAINT "bank_statement_uploads_extraction_status_check" CHECK (("extraction_status" = ANY (ARRAY['uploaded'::"text", 'processing'::"text", 'extracted'::"text", 'failed'::"text"]))),
    CONSTRAINT "bank_statement_uploads_source_type_check" CHECK (("source_type" = ANY (ARRAY['upload'::"text", 'email'::"text", 'mobile'::"text", 'bank_feed'::"text", 'api'::"text", 'other'::"text"])))
);


ALTER TABLE "public"."bank_statement_uploads" OWNER TO "postgres";


COMMENT ON COLUMN "public"."bank_statement_uploads"."extraction_input_tokens" IS 'Prompt token count from VLM bank statement extraction.';



COMMENT ON COLUMN "public"."bank_statement_uploads"."extraction_output_tokens" IS 'Completion token count from VLM bank statement extraction.';



COMMENT ON COLUMN "public"."bank_statement_uploads"."extraction_model" IS 'Model name used for VLM extraction.';



COMMENT ON COLUMN "public"."bank_statement_uploads"."extraction_cost_usd" IS 'Estimated USD cost of VLM extraction.';



CREATE TABLE IF NOT EXISTS "public"."bank_transaction_rules" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "bank_account_id" "uuid",
    "name" "text" NOT NULL,
    "active" boolean DEFAULT true NOT NULL,
    "priority" integer DEFAULT 100 NOT NULL,
    "amount_direction" "text" DEFAULT 'any'::"text" NOT NULL,
    "match_type" "text" DEFAULT 'contains'::"text" NOT NULL,
    "description_pattern" "text",
    "reference_pattern" "text",
    "counterparty_pattern" "text",
    "min_amount" numeric(14,2),
    "max_amount" numeric(14,2),
    "gl_account_id" "uuid",
    "tracking" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "tax_treatment" "text",
    "notes" "text",
    "source_bank_statement_line_id" "uuid",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "criteria" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "criteria_mode" "text" DEFAULT 'and'::"text" NOT NULL,
    CONSTRAINT "bank_transaction_rules_amount_direction_check" CHECK (("amount_direction" = ANY (ARRAY['any'::"text", 'money_in'::"text", 'money_out'::"text"]))),
    CONSTRAINT "bank_transaction_rules_criteria_mode_check" CHECK (("criteria_mode" = ANY (ARRAY['and'::"text", 'or'::"text", 'only'::"text"]))),
    CONSTRAINT "bank_transaction_rules_match_type_check" CHECK (("match_type" = ANY (ARRAY['contains'::"text", 'exact'::"text", 'regex'::"text"])))
);


ALTER TABLE "public"."bank_transaction_rules" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."bank_transaction_suggestions" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "bank_statement_line_id" "uuid" NOT NULL,
    "suggestion_type" "text" NOT NULL,
    "confidence_score" numeric(5,4) DEFAULT 0 NOT NULL,
    "rationale" "text",
    "evidence" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "matched_invoice_id" "uuid",
    "matched_invoice_number" "text",
    "suggested_account_id" "uuid",
    "suggested_tracking" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "suggested_tax_treatment" "text",
    "status" "text" DEFAULT 'open'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "bank_transaction_suggestions_status_check" CHECK (("status" = ANY (ARRAY['open'::"text", 'accepted'::"text", 'rejected'::"text", 'superseded'::"text"]))),
    CONSTRAINT "bank_transaction_suggestions_suggestion_type_check" CHECK (("suggestion_type" = ANY (ARRAY['supplier_invoice'::"text", 'receivable_invoice'::"text", 'prior_transaction'::"text", 'rule'::"text", 'manual'::"text", 'vlm'::"text"])))
);


ALTER TABLE "public"."bank_transaction_suggestions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."bills_synced" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "invoice_extracted_id" "uuid" NOT NULL,
    "supplier_id" "uuid" NOT NULL,
    "xero_bill_id" "text",
    "sync_status" "public"."sync_status" DEFAULT 'pending'::"public"."sync_status" NOT NULL,
    "sync_error" "text",
    "synced_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."bills_synced" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."consolidation_account_mappings" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "reporting_group_id" "uuid" NOT NULL,
    "entity_organisation_id" "uuid" NOT NULL,
    "local_account_id" "uuid" NOT NULL,
    "group_account_id" "uuid" NOT NULL,
    "effective_from" "date" DEFAULT CURRENT_DATE NOT NULL,
    "effective_to" "date",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "consolidation_account_mappings_check" CHECK ((("effective_to" IS NULL) OR ("effective_to" >= "effective_from")))
);


ALTER TABLE "public"."consolidation_account_mappings" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."consolidation_adjustment_lines" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "adjustment_id" "uuid" NOT NULL,
    "line_number" integer NOT NULL,
    "account_id" "uuid" NOT NULL,
    "entity_organisation_id" "uuid",
    "description" "text",
    "debit_amount" numeric(20,4) DEFAULT 0 NOT NULL,
    "credit_amount" numeric(20,4) DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "consolidation_adjustment_lines_check" CHECK ((("debit_amount" >= (0)::numeric) AND ("credit_amount" >= (0)::numeric)))
);


ALTER TABLE "public"."consolidation_adjustment_lines" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."consolidation_adjustments" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "reporting_group_id" "uuid" NOT NULL,
    "period_id" "uuid" NOT NULL,
    "adjustment_type" "text" DEFAULT 'manual'::"text" NOT NULL,
    "description" "text" NOT NULL,
    "status" "text" DEFAULT 'draft'::"text" NOT NULL,
    "created_by" "uuid",
    "posted_by" "uuid",
    "posted_at" timestamp with time zone,
    "reversed_by" "uuid",
    "reversed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "consolidation_adjustments_adjustment_type_check" CHECK (("adjustment_type" = ANY (ARRAY['elimination'::"text", 'reclassification'::"text", 'minority_interest'::"text", 'manual'::"text", 'fx'::"text"]))),
    CONSTRAINT "consolidation_adjustments_status_check" CHECK (("status" = ANY (ARRAY['draft'::"text", 'posted'::"text", 'reversed'::"text"])))
);


ALTER TABLE "public"."consolidation_adjustments" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."consolidation_entity_balances" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "reporting_group_id" "uuid" NOT NULL,
    "period_id" "uuid" NOT NULL,
    "entity_organisation_id" "uuid" NOT NULL,
    "account_id" "uuid" NOT NULL,
    "currency" "text" NOT NULL,
    "debit_amount" numeric(20,4) DEFAULT 0 NOT NULL,
    "credit_amount" numeric(20,4) DEFAULT 0 NOT NULL,
    "source_type" "text" DEFAULT 'trial_balance_import'::"text" NOT NULL,
    "source_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "consolidation_entity_balances_check" CHECK ((("debit_amount" >= (0)::numeric) AND ("credit_amount" >= (0)::numeric)))
);


ALTER TABLE "public"."consolidation_entity_balances" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."consolidation_periods" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "reporting_group_id" "uuid" NOT NULL,
    "name" "text" NOT NULL,
    "start_date" "date" NOT NULL,
    "end_date" "date" NOT NULL,
    "reporting_currency" "text" DEFAULT 'ZAR'::"text" NOT NULL,
    "status" "text" DEFAULT 'draft'::"text" NOT NULL,
    "locked_at" timestamp with time zone,
    "locked_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "consolidation_periods_check" CHECK (("end_date" >= "start_date")),
    CONSTRAINT "consolidation_periods_status_check" CHECK (("status" = ANY (ARRAY['draft'::"text", 'open'::"text", 'locked'::"text", 'closed'::"text"])))
);


ALTER TABLE "public"."consolidation_periods" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."document_pages" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "invoice_raw_id" "uuid",
    "job_id" "uuid",
    "page_number" integer NOT NULL,
    "page_count" integer,
    "extraction_method" "text",
    "text_content" "text",
    "text_preview" "text",
    "image_quality_score" numeric,
    "ocr_confidence" numeric,
    "layout_type" "text",
    "document_type" "text",
    "supplier_guess" "text",
    "invoice_number_guess" "text",
    "invoice_date_guess" "date",
    "total_guess" numeric(14,2),
    "is_continuation_page" boolean,
    "document_group_key" "text",
    "confidence_score" numeric,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "issuer_guess" "text",
    "recipient_guess" "text",
    "document_direction" "text",
    "organisation_match_status" "text",
    "validation_status" "text",
    "original_preview_path" "text",
    "processed_preview_path" "text",
    "preprocessing_notes" "text",
    "crop_applied" boolean DEFAULT false,
    "deskew_applied" boolean DEFAULT false,
    "crop_box" "jsonb",
    "crop_area_ratio" numeric
);


ALTER TABLE "public"."document_pages" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."document_processing_jobs" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "batch_id" "uuid",
    "invoice_raw_id" "uuid",
    "job_type" "text" DEFAULT 'invoice_extract'::"text" NOT NULL,
    "status" "text" DEFAULT 'queued'::"text" NOT NULL,
    "current_stage" "text",
    "priority" integer DEFAULT 100 NOT NULL,
    "retry_count" integer DEFAULT 0 NOT NULL,
    "max_retries" integer DEFAULT 3 NOT NULL,
    "last_error" "text",
    "started_at" timestamp with time zone,
    "completed_at" timestamp with time zone,
    "failed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "extraction_strategy" "text",
    "extracted_invoice_id" "uuid",
    "diagnostic" "jsonb" DEFAULT '{}'::"jsonb"
);


ALTER TABLE "public"."document_processing_jobs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."document_upload_batches" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "uploaded_by" "uuid",
    "source" "text" DEFAULT 'manual_upload'::"text" NOT NULL,
    "status" "text" DEFAULT 'uploaded'::"text" NOT NULL,
    "total_files" integer DEFAULT 0 NOT NULL,
    "processed_files" integer DEFAULT 0 NOT NULL,
    "failed_files" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."document_upload_batches" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."emails_sent" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid",
    "email_type" "text" NOT NULL,
    "recipient_email" "text" NOT NULL,
    "subject" "text",
    "body_preview" "text",
    "send_status" "public"."send_status" DEFAULT 'queued'::"public"."send_status" NOT NULL,
    "sent_by" "uuid",
    "sent_at" timestamp with time zone,
    "related_invoice_id" "uuid",
    "related_statement_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."emails_sent" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."exchange_rates" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "reporting_group_id" "uuid",
    "period_id" "uuid",
    "from_currency" "text" NOT NULL,
    "to_currency" "text" NOT NULL,
    "rate_type" "text" DEFAULT 'closing'::"text" NOT NULL,
    "rate_date" "date" NOT NULL,
    "rate" numeric(20,10) NOT NULL,
    "source" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "exchange_rates_rate_check" CHECK (("rate" > (0)::numeric)),
    CONSTRAINT "exchange_rates_rate_type_check" CHECK (("rate_type" = ANY (ARRAY['closing'::"text", 'average'::"text", 'historical'::"text", 'spot'::"text"])))
);


ALTER TABLE "public"."exchange_rates" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."gl_journal_lines" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "gl_journal_id" "uuid" NOT NULL,
    "account_id" "uuid",
    "description" "text",
    "debit_amount" numeric(14,2) DEFAULT 0 NOT NULL,
    "credit_amount" numeric(14,2) DEFAULT 0 NOT NULL,
    "tracking" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "sort_order" smallint DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."gl_journal_lines" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."gl_journals" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "source_type" "text" DEFAULT 'bank_transaction'::"text" NOT NULL,
    "source_id" "uuid",
    "journal_date" "date",
    "description" "text",
    "status" "text" DEFAULT 'draft'::"text" NOT NULL,
    "total_debit" numeric(14,2) DEFAULT 0 NOT NULL,
    "total_credit" numeric(14,2) DEFAULT 0 NOT NULL,
    "created_by" "uuid",
    "posted_by" "uuid",
    "posted_at" timestamp with time zone,
    "reversed_by" "uuid",
    "reversed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "reversal_of_journal_id" "uuid",
    CONSTRAINT "gl_journals_balanced_check" CHECK (("round"("total_debit", 2) = "round"("total_credit", 2))),
    CONSTRAINT "gl_journals_status_check" CHECK (("status" = ANY (ARRAY['draft'::"text", 'posted'::"text", 'reversed'::"text"])))
);


ALTER TABLE "public"."gl_journals" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_agent_suggestions" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "invoice_raw_id" "uuid",
    "invoice_extracted_id" "uuid",
    "category" "text" NOT NULL,
    "severity" "text" DEFAULT 'info'::"text" NOT NULL,
    "message" "text" NOT NULL,
    "reason" "text",
    "confidence" numeric(5,4) DEFAULT 0 NOT NULL,
    "apply_payload" "jsonb",
    "status" "text" DEFAULT 'open'::"text" NOT NULL,
    "fingerprint" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "target" "jsonb",
    CONSTRAINT "invoice_agent_suggestions_confidence_check" CHECK ((("confidence" >= (0)::numeric) AND ("confidence" <= (1)::numeric))),
    CONSTRAINT "invoice_agent_suggestions_severity_check" CHECK (("severity" = ANY (ARRAY['info'::"text", 'warning'::"text", 'critical'::"text"]))),
    CONSTRAINT "invoice_agent_suggestions_status_check" CHECK (("status" = ANY (ARRAY['open'::"text", 'applied'::"text", 'dismissed'::"text", 'checked'::"text"])))
);


ALTER TABLE "public"."invoice_agent_suggestions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_audit_events" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "invoice_raw_id" "uuid",
    "invoice_extracted_id" "uuid",
    "job_id" "uuid",
    "event_type" "text" NOT NULL,
    "stage" "text",
    "field_name" "text",
    "old_value" "jsonb",
    "new_value" "jsonb",
    "actor_type" "text" DEFAULT 'system'::"text" NOT NULL,
    "actor_user_id" "uuid",
    "source" "text",
    "confidence_before" numeric,
    "confidence_after" numeric,
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."invoice_audit_events" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_audit_log" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "invoice_id" "uuid" NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "user_id" "uuid",
    "field_name" "text",
    "old_value" "text",
    "new_value" "text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."invoice_audit_log" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_extraction_feedback" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "invoice_raw_id" "uuid",
    "invoice_extracted_id" "uuid",
    "supplier_id" "uuid",
    "field_name" "text" NOT NULL,
    "extracted_value" "text",
    "corrected_value" "text" NOT NULL,
    "source_text" "text",
    "layout_type" "text",
    "correction_type" "text" DEFAULT 'manual'::"text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."invoice_extraction_feedback" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_line_item_allocations" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "invoice_line_item_id" "uuid" NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "expense_account" "text",
    "tracking" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "amount" numeric(14,2) NOT NULL,
    "percent" numeric(9,4),
    "note" "text",
    "sort_order" smallint DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "invoice_line_item_allocations_amount_check" CHECK (("amount" >= (0)::numeric)),
    CONSTRAINT "invoice_line_item_allocations_percent_check" CHECK ((("percent" IS NULL) OR (("percent" >= (0)::numeric) AND ("percent" <= (100)::numeric))))
);


ALTER TABLE "public"."invoice_line_item_allocations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_line_items" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "invoice_extracted_id" "uuid" NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "description" "text",
    "quantity" numeric,
    "unit_price" numeric,
    "tax_amount" numeric,
    "line_total" numeric,
    "raw_line" "text",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "code" "text",
    "expense_account" "text",
    "tracking" "jsonb" DEFAULT '{}'::"jsonb",
    "vat_treatment" "text",
    "discount_amount" numeric(14,2),
    "discount_percent" numeric(9,4),
    "discounted_unit_price" numeric(14,2),
    "pricing_basis" "text",
    "pricing_notes" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    CONSTRAINT "invoice_line_items_vat_treatment_check" CHECK ((("vat_treatment" IS NULL) OR ("vat_treatment" = ANY (ARRAY['full'::"text", 'blocked'::"text", 'exempt'::"text", 'zero_rated'::"text"]))))
);


ALTER TABLE "public"."invoice_line_items" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_page_groups" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "invoice_raw_id" "uuid" NOT NULL,
    "page_numbers" "jsonb" NOT NULL,
    "supplier_detected" "text",
    "confidence" numeric,
    "strategy" "text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."invoice_page_groups" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_parse_attempts" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "invoice_raw_id" "uuid" NOT NULL,
    "invoice_extracted_id" "uuid",
    "attempt_number" integer NOT NULL,
    "strategy" "text" NOT NULL,
    "dpi" integer,
    "ocr_variant" "text",
    "ocr_psm" "text",
    "ocr_used" boolean DEFAULT false,
    "ocr_confidence" numeric,
    "image_quality_score" numeric,
    "candidate_score" numeric,
    "confidence_score" numeric,
    "parsed_data" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "line_items" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "text_preview" "text",
    "selected" boolean DEFAULT false NOT NULL,
    "accepted_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."invoice_parse_attempts" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoice_supplier_comparison_ignores" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "invoice_extracted_id" "uuid" NOT NULL,
    "supplier_id" "uuid",
    "field_key" "text" NOT NULL,
    "reason" "text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "invoice_supplier_comparison_ignores_field_check" CHECK (("field_key" = ANY (ARRAY['supplier_name_extracted'::"text", 'supplier_telephone_extracted'::"text", 'supplier_email_extracted'::"text", 'supplier_website_extracted'::"text", 'supplier_del_address_extracted'::"text", 'company_registration_number_extracted'::"text"])))
);


ALTER TABLE "public"."invoice_supplier_comparison_ignores" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."invoices_extracted" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "invoice_raw_id" "uuid" NOT NULL,
    "supplier_id" "uuid",
    "supplier_name_extracted" "text",
    "invoice_number" "text",
    "invoice_date" "date",
    "due_date" "date",
    "subtotal" numeric,
    "tax_amount" numeric,
    "total_amount" numeric,
    "currency" "text" DEFAULT 'GBP'::"text" NOT NULL,
    "confidence_score" numeric,
    "review_status" "public"."review_status" DEFAULT 'pending'::"public"."review_status" NOT NULL,
    "reviewed_by" "uuid",
    "reviewed_at" timestamp with time zone,
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "bank_account_name_extracted" "text",
    "bank_name_extracted" "text",
    "bank_account_number_extracted" "text",
    "bank_branch_code_extracted" "text",
    "bank_swift_code_extracted" "text",
    "override_bank_account_number" "text",
    "override_bank_name" "text",
    "override_sort_code" "text",
    "approved_at" timestamp with time zone,
    "approved_by" "uuid",
    "approval_status" "text" DEFAULT 'pending'::"text",
    "supplier_del_address_extracted" "text",
    "supplier_pos_address_extracted" "text",
    "supplier_email_extracted" "text",
    "supplier_acc_email_extracted" "text",
    "supplier_telephone_extracted" "text",
    "supplier_fax_extracted" "text",
    "supplier_cell_extracted" "text",
    "supplier_website_extracted" "text",
    "vat_number_extracted" "text",
    "cus_code_extracted" "text",
    "company_registration_number_extracted" "text",
    "issuer_name_extracted" "text",
    "recipient_name_extracted" "text",
    "document_direction" "text",
    "organisation_match_status" "text",
    "validation_status" "text",
    "validation_notes" "text",
    "tags" "text"[] DEFAULT '{}'::"text"[],
    "expense_account" "text",
    "document_type" "text" DEFAULT 'tax_invoice'::"text",
    "document_count" integer DEFAULT 1,
    "supplier_branch_id" "uuid",
    "extraction_input_tokens" integer,
    "extraction_output_tokens" integer,
    "extraction_model" "text",
    "extraction_cost_usd" numeric(10,6),
    "prices_include_vat_detected" "text",
    "gl_journal_id" "uuid",
    "posting_status" "text" DEFAULT 'unposted'::"text",
    "posted_at" timestamp with time zone,
    "posted_by" "uuid",
    CONSTRAINT "invoices_extracted_approval_status_check" CHECK (("approval_status" = ANY (ARRAY['pending'::"text", 'approved'::"text", 'rejected'::"text", 'needs_info'::"text"]))),
    CONSTRAINT "invoices_extracted_posting_status_check" CHECK (("posting_status" = ANY (ARRAY['unposted'::"text", 'posted'::"text", 'reversed'::"text"]))),
    CONSTRAINT "invoices_extracted_prices_include_vat_detected_check" CHECK (("prices_include_vat_detected" = ANY (ARRAY['exclusive'::"text", 'inclusive'::"text"])))
);


ALTER TABLE "public"."invoices_extracted" OWNER TO "postgres";


COMMENT ON COLUMN "public"."invoices_extracted"."extraction_input_tokens" IS 'Prompt token count from VLM extraction call.';



COMMENT ON COLUMN "public"."invoices_extracted"."extraction_output_tokens" IS 'Completion token count from VLM extraction call.';



COMMENT ON COLUMN "public"."invoices_extracted"."extraction_model" IS 'Model name used for VLM extraction (e.g. gemini-2.5-flash).';



COMMENT ON COLUMN "public"."invoices_extracted"."extraction_cost_usd" IS 'Estimated USD cost of VLM extraction based on published token rates.';



COMMENT ON COLUMN "public"."invoices_extracted"."prices_include_vat_detected" IS 'Auto-detected VAT treatment: exclusive = line prices are ex-VAT; inclusive = prices include VAT (stripped during extraction).';



COMMENT ON COLUMN "public"."invoices_extracted"."gl_journal_id" IS 'GL journal created when invoice was posted.';



COMMENT ON COLUMN "public"."invoices_extracted"."posting_status" IS 'unposted | posted | reversed';



CREATE TABLE IF NOT EXISTS "public"."invoices_raw" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid",
    "file_name" "text" NOT NULL,
    "file_path" "text" NOT NULL,
    "file_type" "text",
    "source_type" "text" DEFAULT 'upload'::"text" NOT NULL,
    "upload_status" "public"."upload_status" DEFAULT 'uploaded'::"public"."upload_status" NOT NULL,
    "uploaded_by" "uuid",
    "uploaded_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "parse_requested_at" timestamp with time zone,
    "parse_completed_at" timestamp with time zone,
    "parse_status" "public"."parse_status" DEFAULT 'pending'::"public"."parse_status" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "preview_path" "text",
    "processed_preview_path" "text",
    "grouped_from_pages" "jsonb" DEFAULT '[]'::"jsonb",
    "page_grouping_strategy" "text",
    "total_pages_in_upload" integer,
    "parse_started_at" timestamp with time zone
);


ALTER TABLE "public"."invoices_raw" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."org_integrations" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "name" "text" NOT NULL,
    "display_name" "text" NOT NULL,
    "active" boolean DEFAULT true NOT NULL,
    "position" smallint DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."org_integrations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."organisation_invoice_branding" (
    "organisation_id" "uuid" NOT NULL,
    "logo_storage_path" "text",
    "primary_color" "text" DEFAULT '#174EA6'::"text" NOT NULL,
    "accent_color" "text" DEFAULT '#E8EEF9'::"text" NOT NULL,
    "text_color" "text" DEFAULT '#111827'::"text" NOT NULL,
    "font_family" "text" DEFAULT 'inter'::"text" NOT NULL,
    "terms_and_conditions" "text" DEFAULT ''::"text" NOT NULL,
    "bank_name" "text" DEFAULT ''::"text" NOT NULL,
    "account_holder" "text" DEFAULT ''::"text" NOT NULL,
    "account_number" "text" DEFAULT ''::"text" NOT NULL,
    "account_type" "text" DEFAULT ''::"text" NOT NULL,
    "branch_code" "text" DEFAULT ''::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "organisation_invoice_branding_accent_color_check" CHECK (("accent_color" ~ '^#[0-9A-Fa-f]{6}$'::"text")),
    CONSTRAINT "organisation_invoice_branding_font_family_check" CHECK (("font_family" = ANY (ARRAY['inter'::"text", 'arial'::"text", 'georgia'::"text", 'times_new_roman'::"text", 'roboto_mono'::"text"]))),
    CONSTRAINT "organisation_invoice_branding_primary_color_check" CHECK (("primary_color" ~ '^#[0-9A-Fa-f]{6}$'::"text")),
    CONSTRAINT "organisation_invoice_branding_text_color_check" CHECK (("text_color" ~ '^#[0-9A-Fa-f]{6}$'::"text"))
);


ALTER TABLE "public"."organisation_invoice_branding" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."organisation_module_settings" (
    "organisation_id" "uuid" NOT NULL,
    "module_key" "text" NOT NULL,
    "tracking_enabled" boolean DEFAULT false NOT NULL,
    "required_tracking_dimension_ids" "uuid"[] DEFAULT '{}'::"uuid"[] NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "organisation_module_settings_check" CHECK (("tracking_enabled" OR ("cardinality"("required_tracking_dimension_ids") = 0))),
    CONSTRAINT "organisation_module_settings_check1" CHECK (((NOT "tracking_enabled") OR ("cardinality"("required_tracking_dimension_ids") > 0))),
    CONSTRAINT "organisation_module_settings_module_key_check" CHECK (("module_key" = ANY (ARRAY['supplier'::"text", 'customer'::"text", 'inventory'::"text", 'bank_cash'::"text", 'asset'::"text", 'liability'::"text", 'project'::"text"])))
);


ALTER TABLE "public"."organisation_module_settings" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."organisation_users" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "user_id" "uuid",
    "role" "public"."organisation_role" DEFAULT 'viewer'::"public"."organisation_role" NOT NULL,
    "status" "public"."membership_status" DEFAULT 'active'::"public"."membership_status" NOT NULL,
    "invited_email" "text",
    "invited_at" timestamp with time zone,
    "accepted_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "phone" "text",
    "external_sender_emails" "text"[] DEFAULT '{}'::"text"[] NOT NULL,
    "invoice_approver" boolean DEFAULT false NOT NULL,
    "permissions" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL
);


ALTER TABLE "public"."organisation_users" OWNER TO "postgres";


COMMENT ON COLUMN "public"."organisation_users"."invoice_approver" IS 'When true, this member can approve invoices regardless of their role.';



COMMENT ON COLUMN "public"."organisation_users"."permissions" IS 'Optional granular permission overrides for this member. Keys are MemberPermissionKey values.';



CREATE TABLE IF NOT EXISTS "public"."organisations" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "name" "text" NOT NULL,
    "legal_name" "text",
    "country" "text" DEFAULT 'GB'::"text" NOT NULL,
    "currency" "text" DEFAULT 'GBP'::"text" NOT NULL,
    "xero_connected" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "registration_number" "text",
    "vat_number" "text",
    "tax_number" "text",
    "base_currency" "text",
    "financial_year_end" "text",
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "trading_name" "text",
    "organisation_type" "text",
    "vat_registered" boolean DEFAULT false NOT NULL,
    "paye_registered" boolean DEFAULT false NOT NULL,
    "paye_reference_number" "text",
    "uif_reference_number" "text",
    "sdl_registered" boolean DEFAULT false NOT NULL,
    "primary_email" "text",
    "accounts_email" "text",
    "phone" "text",
    "website" "text",
    "physical_address_line_1" "text",
    "physical_address_line_2" "text",
    "physical_city" "text",
    "physical_province" "text",
    "physical_postal_code" "text",
    "physical_country" "text",
    "postal_same_as_physical" boolean DEFAULT true NOT NULL,
    "postal_address_line_1" "text",
    "postal_address_line_2" "text",
    "postal_city" "text",
    "postal_province" "text",
    "postal_postal_code" "text",
    "postal_country" "text",
    "default_payment_terms" integer,
    "vat_basis" "text",
    "invoice_approval_required" boolean DEFAULT true NOT NULL,
    "extraction_strategy" "text" DEFAULT 'auto_group'::"text",
    "ask_per_upload" boolean DEFAULT false,
    "vlm_enabled" boolean DEFAULT false,
    "inbound_email" "text",
    "supplier_auto_link_min_matches" integer DEFAULT 2 NOT NULL,
    "reporting_standard" "text" DEFAULT 'ifrs'::"text" NOT NULL,
    "income_statement_presentation" "text" DEFAULT 'function'::"text" NOT NULL,
    "auto_link_amount_tiers" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    CONSTRAINT "organisations_auto_link_amount_tiers_check" CHECK ("public"."is_valid_auto_link_amount_tiers"("auto_link_amount_tiers")),
    CONSTRAINT "organisations_income_statement_presentation_check" CHECK (("income_statement_presentation" = ANY (ARRAY['function'::"text", 'nature'::"text"]))),
    CONSTRAINT "organisations_reporting_standard_check" CHECK (("reporting_standard" = ANY (ARRAY['ifrs'::"text", 'us_gaap'::"text", 'uk_gaap_frs_102'::"text", 'aspe'::"text"]))),
    CONSTRAINT "organisations_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'suspended'::"text", 'archived'::"text"]))),
    CONSTRAINT "organisations_supplier_auto_link_min_matches_check" CHECK ((("supplier_auto_link_min_matches" >= 1) AND ("supplier_auto_link_min_matches" <= 4))),
    CONSTRAINT "organisations_us_gaap_function_presentation_chk" CHECK ((("reporting_standard" <> 'us_gaap'::"text") OR ("income_statement_presentation" = 'function'::"text")))
);


ALTER TABLE "public"."organisations" OWNER TO "postgres";


COMMENT ON COLUMN "public"."organisations"."reporting_standard" IS 'Default reporting framework used when generating financial statements.';



COMMENT ON COLUMN "public"."organisations"."income_statement_presentation" IS 'Default Income Statement expense presentation: function or nature. US GAAP reports must resolve to function.';



COMMENT ON COLUMN "public"."organisations"."auto_link_amount_tiers" IS 'Per-amount-tier supplier auto-link thresholds. Each entry: {max_amount: numeric|null, required_matches: 1-4}. Tiers evaluated ascending by max_amount; null max_amount = catch-all. Falls back to supplier_auto_link_min_matches when empty.';



CREATE TABLE IF NOT EXISTS "public"."payments" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid" NOT NULL,
    "payment_date" "date" NOT NULL,
    "payment_reference" "text",
    "amount" numeric NOT NULL,
    "currency" "text" DEFAULT 'GBP'::"text" NOT NULL,
    "payment_source" "text",
    "xero_payment_id" "text",
    "match_status" "public"."match_status" DEFAULT 'unmatched'::"public"."match_status" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."payments" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."profiles" (
    "id" "uuid" NOT NULL,
    "email" "text",
    "full_name" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."profiles" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."reconciliation_lines" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "reconciliation_id" "uuid" NOT NULL,
    "statement_line_id" "uuid",
    "invoice_extracted_id" "uuid",
    "bill_synced_id" "uuid",
    "payment_id" "uuid",
    "match_status" "public"."match_status" DEFAULT 'unmatched'::"public"."match_status" NOT NULL,
    "exception_type" "public"."exception_type",
    "expected_amount" numeric,
    "matched_amount" numeric,
    "variance_amount" numeric,
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."reconciliation_lines" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."reconciliation_results" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid",
    "reconciliation_id" "text" NOT NULL,
    "statement_raw_id" "uuid",
    "line_id" "uuid",
    "match_status" "text" NOT NULL,
    "expected_amount" numeric(14,2),
    "matched_amount" numeric(14,2),
    "variance_amount" numeric(14,2),
    "matched_invoice_id" "uuid",
    "matched_invoice_number" "text",
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "reconciliation_results_match_status_check" CHECK (("match_status" = ANY (ARRAY['matched'::"text", 'unmatched'::"text", 'exception'::"text"])))
);


ALTER TABLE "public"."reconciliation_results" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."reconciliations" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid" NOT NULL,
    "statement_raw_id" "uuid",
    "reconciliation_date" "date" DEFAULT CURRENT_DATE NOT NULL,
    "reconciliation_status" "public"."reconciliation_status" DEFAULT 'draft'::"public"."reconciliation_status" NOT NULL,
    "total_statement_amount" numeric,
    "total_matched_amount" numeric,
    "total_unmatched_amount" numeric,
    "notes" "text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."reconciliations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."remittances" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid" NOT NULL,
    "payment_id" "uuid",
    "remittance_date" "date" DEFAULT CURRENT_DATE NOT NULL,
    "remittance_status" "public"."remittance_status" DEFAULT 'draft'::"public"."remittance_status" NOT NULL,
    "total_amount" numeric NOT NULL,
    "currency" "text" DEFAULT 'GBP'::"text" NOT NULL,
    "file_path" "text",
    "sent_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."remittances" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."reporting_group_entities" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "reporting_group_id" "uuid" NOT NULL,
    "parent_entity_id" "uuid",
    "organisation_id" "uuid" NOT NULL,
    "entity_type" "text" NOT NULL,
    "ownership_percent" numeric(7,4) DEFAULT 100 NOT NULL,
    "consolidation_method" "text" DEFAULT 'full'::"text" NOT NULL,
    "effective_from" "date" DEFAULT CURRENT_DATE NOT NULL,
    "effective_to" "date",
    "sort_order" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "reporting_group_entities_check" CHECK ((("effective_to" IS NULL) OR ("effective_to" >= "effective_from"))),
    CONSTRAINT "reporting_group_entities_consolidation_method_check" CHECK (("consolidation_method" = ANY (ARRAY['full'::"text", 'proportionate'::"text", 'equity'::"text", 'none'::"text"]))),
    CONSTRAINT "reporting_group_entities_entity_type_check" CHECK (("entity_type" = ANY (ARRAY['parent'::"text", 'subsidiary'::"text", 'associate'::"text", 'joint_venture'::"text"]))),
    CONSTRAINT "reporting_group_entities_ownership_percent_check" CHECK ((("ownership_percent" >= (0)::numeric) AND ("ownership_percent" <= (100)::numeric)))
);


ALTER TABLE "public"."reporting_group_entities" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."reporting_group_users" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "reporting_group_id" "uuid" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" DEFAULT 'viewer'::"text" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "reporting_group_users_role_check" CHECK (("role" = ANY (ARRAY['owner'::"text", 'admin'::"text", 'accountant'::"text", 'reviewer'::"text", 'viewer'::"text"]))),
    CONSTRAINT "reporting_group_users_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'invited'::"text", 'suspended'::"text", 'revoked'::"text"])))
);


ALTER TABLE "public"."reporting_group_users" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."reporting_groups" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "owner_organisation_id" "uuid" NOT NULL,
    "name" "text" NOT NULL,
    "reporting_currency" "text" DEFAULT 'ZAR'::"text" NOT NULL,
    "country" "text",
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "reporting_groups_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'archived'::"text"])))
);


ALTER TABLE "public"."reporting_groups" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."statement_lines" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "statement_raw_id" "uuid" NOT NULL,
    "supplier_id" "uuid",
    "line_date" "date",
    "reference" "text",
    "description" "text",
    "debit_amount" numeric,
    "credit_amount" numeric,
    "balance_amount" numeric,
    "invoice_number" "text",
    "line_type" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "match_status" "text",
    "review_status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "review_action" "text",
    "review_invoice_id" "uuid",
    "reviewed_at" timestamp with time zone,
    "reviewed_by" "uuid",
    CONSTRAINT "statement_lines_review_status_check" CHECK (("review_status" = ANY (ARRAY['pending'::"text", 'resolved'::"text", 'ignored'::"text"])))
);


ALTER TABLE "public"."statement_lines" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."statements_raw" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid",
    "file_name" "text" NOT NULL,
    "file_path" "text" NOT NULL,
    "file_type" "text",
    "statement_period_from" "date",
    "statement_period_to" "date",
    "statement_date" "date",
    "upload_status" "public"."upload_status" DEFAULT 'uploaded'::"public"."upload_status" NOT NULL,
    "uploaded_by" "uuid",
    "uploaded_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "parse_status" "public"."parse_status" DEFAULT 'pending'::"public"."parse_status" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."statements_raw" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."supplier_branches" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid" NOT NULL,
    "branch_name" "text" NOT NULL,
    "branch_code" "text",
    "vat_number" "text",
    "tax_number" "text",
    "company_registration_number" "text",
    "phone" "text",
    "default_email" "text",
    "website" "text",
    "delivery_address" "text",
    "postal_address" "text",
    "bank_account_name" "text",
    "bank_name" "text",
    "bank_account_number" "text",
    "bank_branch_code" "text",
    "bank_swift_code" "text",
    "active" boolean DEFAULT true NOT NULL,
    "source_invoice_extracted_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."supplier_branches" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."supplier_contacts" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid" NOT NULL,
    "contact_name" "text" NOT NULL,
    "email" "text",
    "phone" "text",
    "is_primary" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."supplier_contacts" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."supplier_extraction_profiles" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid",
    "profile_name" "text" NOT NULL,
    "supplier_name_pattern" "text",
    "layout_type" "text" NOT NULL,
    "invoice_number_rule" "jsonb" DEFAULT '{}'::"jsonb",
    "invoice_date_rule" "jsonb" DEFAULT '{}'::"jsonb",
    "totals_rule" "jsonb" DEFAULT '{}'::"jsonb",
    "line_items_rule" "jsonb" DEFAULT '{}'::"jsonb",
    "banking_rule" "jsonb" DEFAULT '{}'::"jsonb",
    "supplier_details_rule" "jsonb" DEFAULT '{}'::"jsonb",
    "confidence_threshold" numeric DEFAULT 0.70,
    "is_active" boolean DEFAULT true,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "updated_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."supplier_extraction_profiles" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."supplier_kyc_documents" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "kyc_request_id" "uuid" NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "document_type" "text" NOT NULL,
    "document_label" "text",
    "storage_path" "text" NOT NULL,
    "file_name" "text" NOT NULL,
    "file_size" bigint,
    "mime_type" "text",
    "uploaded_by" "uuid",
    "uploaded_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "supplier_kyc_documents_document_type_check" CHECK (("document_type" = ANY (ARRAY['id_document'::"text", 'company_registration'::"text", 'bank_confirmation'::"text", 'vat_certificate'::"text", 'tax_clearance'::"text", 'proof_of_address'::"text", 'other'::"text"])))
);


ALTER TABLE "public"."supplier_kyc_documents" OWNER TO "postgres";


COMMENT ON TABLE "public"."supplier_kyc_documents" IS 'Documents uploaded as evidence for a KYC request (stored in Supabase Storage).';



CREATE TABLE IF NOT EXISTS "public"."supplier_kyc_requests" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid" NOT NULL,
    "trigger_type" "text" NOT NULL,
    "status" "text" DEFAULT 'draft'::"text" NOT NULL,
    "notes" "text",
    "requested_by" "uuid",
    "submitted_at" timestamp with time zone,
    "reviewed_by" "uuid",
    "reviewed_at" timestamp with time zone,
    "reviewer_notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "supplier_kyc_requests_status_check" CHECK (("status" = ANY (ARRAY['draft'::"text", 'submitted'::"text", 'approved'::"text", 'rejected'::"text", 'cancelled'::"text"]))),
    CONSTRAINT "supplier_kyc_requests_trigger_type_check" CHECK (("trigger_type" = ANY (ARRAY['new_supplier'::"text", 'bank_change'::"text", 'info_change'::"text", 'periodic_review'::"text", 'other'::"text"])))
);


ALTER TABLE "public"."supplier_kyc_requests" OWNER TO "postgres";


COMMENT ON TABLE "public"."supplier_kyc_requests" IS 'Know-Your-Customer approval requests for supplier onboarding and data changes.';



COMMENT ON COLUMN "public"."supplier_kyc_requests"."trigger_type" IS 'What prompted this KYC request: new_supplier, bank_change, info_change, periodic_review, other.';



COMMENT ON COLUMN "public"."supplier_kyc_requests"."status" IS 'Lifecycle: draft → submitted → approved|rejected. Can also be cancelled.';



CREATE TABLE IF NOT EXISTS "public"."supplier_line_item_allocation_rule_splits" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "rule_id" "uuid" NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "expense_account" "text",
    "tracking" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "percent" numeric(9,4) NOT NULL,
    "note" "text",
    "sort_order" smallint DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "supplier_allocation_rule_splits_percent_check" CHECK ((("percent" > (0)::numeric) AND ("percent" <= (100)::numeric)))
);


ALTER TABLE "public"."supplier_line_item_allocation_rule_splits" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."supplier_line_item_allocation_rules" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_id" "uuid" NOT NULL,
    "name" "text" NOT NULL,
    "active" boolean DEFAULT true NOT NULL,
    "priority" integer DEFAULT 100 NOT NULL,
    "document_scope" "text" DEFAULT 'all'::"text" NOT NULL,
    "match_type" "text" DEFAULT 'all_lines'::"text" NOT NULL,
    "pattern" "text",
    "match_field" "text" DEFAULT 'description'::"text" NOT NULL,
    "notes" "text",
    "source_invoice_extracted_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "supplier_line_item_allocation_rules_document_scope_check" CHECK (("document_scope" = ANY (ARRAY['all'::"text", 'invoice'::"text", 'credit_note'::"text"]))),
    CONSTRAINT "supplier_line_item_allocation_rules_match_field_check" CHECK (("match_field" = ANY (ARRAY['description'::"text", 'code'::"text", 'description_or_code'::"text"]))),
    CONSTRAINT "supplier_line_item_allocation_rules_match_type_check" CHECK (("match_type" = ANY (ARRAY['all_lines'::"text", 'contains'::"text", 'exact'::"text", 'regex'::"text"])))
);


ALTER TABLE "public"."supplier_line_item_allocation_rules" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."suppliers" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "supplier_name" "text" NOT NULL,
    "supplier_code" "text",
    "account_number" "text",
    "tax_number" "text",
    "registration_number" "text",
    "currency" "text" DEFAULT 'GBP'::"text" NOT NULL,
    "default_email" "text",
    "phone" "text",
    "payment_terms" integer DEFAULT 30 NOT NULL,
    "active" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "vat_number" "text",
    "company_registration_number" "text",
    "payment_terms_text" "text",
    "payment_terms_days" integer,
    "early_settlement_discount_percent" numeric,
    "early_settlement_days" integer,
    "bank_account_name" "text",
    "bank_name" "text",
    "bank_account_number" "text",
    "bank_branch_code" "text",
    "bank_swift_code" "text",
    "bank_country" "text",
    "bank_verified" boolean DEFAULT false NOT NULL,
    "bank_details_last_updated_at" timestamp with time zone,
    "bank_details_source" "text",
    "parse_line_items" boolean DEFAULT false,
    "line_items_include_vat" boolean DEFAULT true,
    "track_inventory" boolean DEFAULT false,
    "use_uom_from_description" boolean DEFAULT false,
    "default_expense_account" "text",
    "default_vat_rate" numeric(5,2),
    "delivery_address" "text",
    "postal_address" "text",
    "accounting_email" "text",
    "fax" "text",
    "cell" "text",
    "website" "text",
    "source_invoice_extracted_id" "uuid",
    "kyc_status" "text" DEFAULT 'not_started'::"text",
    "kyc_verified_at" timestamp with time zone,
    "kyc_verified_by" "uuid",
    "trading_name" "text",
    "auto_save_rules" boolean DEFAULT false NOT NULL,
    "stp_enabled" boolean DEFAULT false NOT NULL,
    "stp_max_amount" numeric(14,2),
    "default_tracking" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    CONSTRAINT "suppliers_default_tracking_object_check" CHECK (("jsonb_typeof"("default_tracking") = 'object'::"text")),
    CONSTRAINT "suppliers_kyc_status_check" CHECK (("kyc_status" = ANY (ARRAY['not_started'::"text", 'pending'::"text", 'approved'::"text", 'rejected'::"text"]))),
    CONSTRAINT "suppliers_stp_max_amount_check" CHECK ((("stp_max_amount" IS NULL) OR (("stp_max_amount" <> 'NaN'::numeric) AND ("stp_max_amount" >= (0)::numeric))))
);


ALTER TABLE "public"."suppliers" OWNER TO "postgres";


COMMENT ON COLUMN "public"."suppliers"."kyc_status" IS 'Current KYC state: not_started, pending, approved, rejected.';



COMMENT ON COLUMN "public"."suppliers"."kyc_verified_at" IS 'When the most recent KYC approval was granted.';



COMMENT ON COLUMN "public"."suppliers"."kyc_verified_by" IS 'Who approved the most recent KYC.';



COMMENT ON COLUMN "public"."suppliers"."stp_enabled" IS 'When true, qualifying extractions for this supplier are auto-posted to GL without manual review.';



COMMENT ON COLUMN "public"."suppliers"."stp_max_amount" IS 'Maximum invoice total eligible for STP. NULL = no limit. Amounts above this land in Needs Review.';



COMMENT ON COLUMN "public"."suppliers"."default_tracking" IS 'Default invoice allocation tracking as {tracking_dimension_id: tracking_value_id}. Line-specific and supplier allocation-rule tracking values take precedence.';



CREATE TABLE IF NOT EXISTS "public"."themes" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "slug" "text" NOT NULL,
    "name" "text" NOT NULL,
    "description" "text",
    "preview_image_url" "text",
    "tokens" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "is_active" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."themes" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."tracking_dimensions" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "name" "text" NOT NULL,
    "position" smallint NOT NULL,
    "active" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "default_value_id" "uuid",
    "is_income_statement_function_driver" boolean DEFAULT false NOT NULL,
    CONSTRAINT "tracking_dimensions_position_check" CHECK ((("position" >= 1) AND ("position" <= 5)))
);


ALTER TABLE "public"."tracking_dimensions" OWNER TO "postgres";


COMMENT ON COLUMN "public"."tracking_dimensions"."default_value_id" IS 'Default tracking value auto-selected when opening a bank transaction for review.';



COMMENT ON COLUMN "public"."tracking_dimensions"."is_income_statement_function_driver" IS 'Marks the one tracking dimension whose values drive Income Statement Function presentation.';



CREATE TABLE IF NOT EXISTS "public"."tracking_values" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "dimension_id" "uuid" NOT NULL,
    "code" "text",
    "name" "text" NOT NULL,
    "active" boolean DEFAULT true NOT NULL,
    "sort_order" smallint DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "income_statement_function" "text",
    CONSTRAINT "tracking_values_income_statement_function_check" CHECK ((("income_statement_function" IS NULL) OR ("income_statement_function" = ANY (ARRAY['cogs'::"text", 'selling'::"text", 'g_and_a'::"text", 'r_and_d'::"text", 'other_operating'::"text"]))))
);


ALTER TABLE "public"."tracking_values" OWNER TO "postgres";


COMMENT ON COLUMN "public"."tracking_values"."income_statement_function" IS 'Function classification used when this tracking value appears on a posted GL line.';



CREATE OR REPLACE VIEW "public"."user_organisations" WITH ("security_invoker"='true') AS
 SELECT "id",
    "organisation_id",
    "user_id",
    ("role")::"text" AS "role",
    "created_at"
   FROM "public"."organisation_users"
  WHERE ("status" = 'active'::"public"."membership_status");


ALTER VIEW "public"."user_organisations" OWNER TO "postgres";


COMMENT ON VIEW "public"."user_organisations" IS 'Active organisation memberships exposed with caller privileges and organisation_users RLS.';



CREATE TABLE IF NOT EXISTS "public"."user_roles" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "role" "public"."app_role" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."user_roles" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."user_theme_entitlements" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "theme_id" "uuid" NOT NULL,
    "source" "text" DEFAULT 'store_purchase'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."user_theme_entitlements" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."user_theme_preferences" (
    "user_id" "uuid" NOT NULL,
    "active_theme_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."user_theme_preferences" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."whatsapp_pending_selections" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "phone" "text" NOT NULL,
    "options" "jsonb" NOT NULL,
    "media_id" "text" NOT NULL,
    "mime_type" "text" NOT NULL,
    "filename" "text",
    "uploaded_by" "uuid",
    "expires_at" timestamp with time zone DEFAULT ("now"() + '00:10:00'::interval) NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);

ALTER TABLE ONLY "public"."whatsapp_pending_selections" FORCE ROW LEVEL SECURITY;


ALTER TABLE "public"."whatsapp_pending_selections" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."xero_connections" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "connection_status" "public"."connection_status" DEFAULT 'disconnected'::"public"."connection_status" NOT NULL,
    "external_user_id" "text",
    "token_expires_at" timestamp with time zone,
    "last_sync_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."xero_connections" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."xero_tenants" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "organisation_id" "uuid" NOT NULL,
    "xero_connection_id" "uuid" NOT NULL,
    "xero_tenant_id" "text" NOT NULL,
    "tenant_name" "text" NOT NULL,
    "tenant_type" "text",
    "is_default" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."xero_tenants" OWNER TO "postgres";


ALTER TABLE ONLY "public"."account_budgets"
    ADD CONSTRAINT "account_budgets_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."account_mappings"
    ADD CONSTRAINT "account_mappings_account_id_integration_id_key" UNIQUE ("account_id", "integration_id");



ALTER TABLE ONLY "public"."account_mappings"
    ADD CONSTRAINT "account_mappings_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."accounts"
    ADD CONSTRAINT "accounts_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."asset_types"
    ADD CONSTRAINT "asset_types_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."audit_log"
    ADD CONSTRAINT "audit_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bank_accounts"
    ADD CONSTRAINT "bank_accounts_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bank_audit_events"
    ADD CONSTRAINT "bank_audit_events_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bank_parsing_rules"
    ADD CONSTRAINT "bank_parsing_rules_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bank_statement_lines"
    ADD CONSTRAINT "bank_statement_lines_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bank_statement_uploads"
    ADD CONSTRAINT "bank_statement_uploads_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bank_transaction_rules"
    ADD CONSTRAINT "bank_transaction_rules_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bank_transaction_suggestions"
    ADD CONSTRAINT "bank_transaction_suggestions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bills_synced"
    ADD CONSTRAINT "bills_synced_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."consolidation_account_mappings"
    ADD CONSTRAINT "consolidation_account_mapping_reporting_group_id_entity_org_key" UNIQUE ("reporting_group_id", "entity_organisation_id", "local_account_id", "effective_from");



ALTER TABLE ONLY "public"."consolidation_account_mappings"
    ADD CONSTRAINT "consolidation_account_mappings_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."consolidation_adjustment_lines"
    ADD CONSTRAINT "consolidation_adjustment_lines_adjustment_id_line_number_key" UNIQUE ("adjustment_id", "line_number");



ALTER TABLE ONLY "public"."consolidation_adjustment_lines"
    ADD CONSTRAINT "consolidation_adjustment_lines_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."consolidation_adjustments"
    ADD CONSTRAINT "consolidation_adjustments_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."consolidation_entity_balances"
    ADD CONSTRAINT "consolidation_entity_balances_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."consolidation_entity_balances"
    ADD CONSTRAINT "consolidation_entity_balances_reporting_group_id_period_id__key" UNIQUE ("reporting_group_id", "period_id", "entity_organisation_id", "account_id", "source_type", "source_id");



ALTER TABLE ONLY "public"."consolidation_periods"
    ADD CONSTRAINT "consolidation_periods_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."consolidation_periods"
    ADD CONSTRAINT "consolidation_periods_reporting_group_id_start_date_end_dat_key" UNIQUE ("reporting_group_id", "start_date", "end_date");



ALTER TABLE ONLY "public"."document_pages"
    ADD CONSTRAINT "document_pages_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."document_processing_jobs"
    ADD CONSTRAINT "document_processing_jobs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."document_upload_batches"
    ADD CONSTRAINT "document_upload_batches_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."emails_sent"
    ADD CONSTRAINT "emails_sent_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."exchange_rates"
    ADD CONSTRAINT "exchange_rates_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."exchange_rates"
    ADD CONSTRAINT "exchange_rates_reporting_group_id_period_id_from_currency_t_key" UNIQUE ("reporting_group_id", "period_id", "from_currency", "to_currency", "rate_type", "rate_date");



ALTER TABLE ONLY "public"."gl_journal_lines"
    ADD CONSTRAINT "gl_journal_lines_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."gl_journals"
    ADD CONSTRAINT "gl_journals_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_agent_suggestions"
    ADD CONSTRAINT "invoice_agent_suggestions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_audit_events"
    ADD CONSTRAINT "invoice_audit_events_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_audit_log"
    ADD CONSTRAINT "invoice_audit_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_extraction_feedback"
    ADD CONSTRAINT "invoice_extraction_feedback_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_line_item_allocations"
    ADD CONSTRAINT "invoice_line_item_allocations_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_line_items"
    ADD CONSTRAINT "invoice_line_items_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_page_groups"
    ADD CONSTRAINT "invoice_page_groups_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_parse_attempts"
    ADD CONSTRAINT "invoice_parse_attempts_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoice_supplier_comparison_ignores"
    ADD CONSTRAINT "invoice_supplier_comparison_i_organisation_id_invoice_extra_key" UNIQUE ("organisation_id", "invoice_extracted_id", "field_key");



ALTER TABLE ONLY "public"."invoice_supplier_comparison_ignores"
    ADD CONSTRAINT "invoice_supplier_comparison_ignores_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoices_extracted"
    ADD CONSTRAINT "invoices_extracted_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."invoices_raw"
    ADD CONSTRAINT "invoices_raw_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."org_integrations"
    ADD CONSTRAINT "org_integrations_organisation_id_name_key" UNIQUE ("organisation_id", "name");



ALTER TABLE ONLY "public"."org_integrations"
    ADD CONSTRAINT "org_integrations_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."organisation_invoice_branding"
    ADD CONSTRAINT "organisation_invoice_branding_pkey" PRIMARY KEY ("organisation_id");



ALTER TABLE ONLY "public"."organisation_module_settings"
    ADD CONSTRAINT "organisation_module_settings_pkey" PRIMARY KEY ("organisation_id", "module_key");



ALTER TABLE ONLY "public"."organisation_users"
    ADD CONSTRAINT "organisation_users_organisation_id_user_id_key" UNIQUE ("organisation_id", "user_id");



ALTER TABLE ONLY "public"."organisation_users"
    ADD CONSTRAINT "organisation_users_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."organisations"
    ADD CONSTRAINT "organisations_inbound_email_key" UNIQUE ("inbound_email");



ALTER TABLE ONLY "public"."organisations"
    ADD CONSTRAINT "organisations_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."payments"
    ADD CONSTRAINT "payments_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."reconciliation_lines"
    ADD CONSTRAINT "reconciliation_lines_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."reconciliation_results"
    ADD CONSTRAINT "reconciliation_results_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."reconciliations"
    ADD CONSTRAINT "reconciliations_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."remittances"
    ADD CONSTRAINT "remittances_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."reporting_group_entities"
    ADD CONSTRAINT "reporting_group_entities_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."reporting_group_entities"
    ADD CONSTRAINT "reporting_group_entities_reporting_group_id_organisation_id_key" UNIQUE ("reporting_group_id", "organisation_id", "effective_from");



ALTER TABLE ONLY "public"."reporting_group_users"
    ADD CONSTRAINT "reporting_group_users_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."reporting_group_users"
    ADD CONSTRAINT "reporting_group_users_reporting_group_id_user_id_key" UNIQUE ("reporting_group_id", "user_id");



ALTER TABLE ONLY "public"."reporting_groups"
    ADD CONSTRAINT "reporting_groups_owner_organisation_id_name_key" UNIQUE ("owner_organisation_id", "name");



ALTER TABLE ONLY "public"."reporting_groups"
    ADD CONSTRAINT "reporting_groups_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."statement_lines"
    ADD CONSTRAINT "statement_lines_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."statements_raw"
    ADD CONSTRAINT "statements_raw_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."supplier_branches"
    ADD CONSTRAINT "supplier_branches_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."supplier_contacts"
    ADD CONSTRAINT "supplier_contacts_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."supplier_extraction_profiles"
    ADD CONSTRAINT "supplier_extraction_profiles_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."supplier_kyc_documents"
    ADD CONSTRAINT "supplier_kyc_documents_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."supplier_kyc_requests"
    ADD CONSTRAINT "supplier_kyc_requests_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."supplier_line_item_allocation_rule_splits"
    ADD CONSTRAINT "supplier_line_item_allocation_rule_splits_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."supplier_line_item_allocation_rules"
    ADD CONSTRAINT "supplier_line_item_allocation_rules_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."suppliers"
    ADD CONSTRAINT "suppliers_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."themes"
    ADD CONSTRAINT "themes_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."themes"
    ADD CONSTRAINT "themes_slug_key" UNIQUE ("slug");



ALTER TABLE ONLY "public"."tracking_dimensions"
    ADD CONSTRAINT "tracking_dimensions_organisation_id_position_key" UNIQUE ("organisation_id", "position");



ALTER TABLE ONLY "public"."tracking_dimensions"
    ADD CONSTRAINT "tracking_dimensions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."tracking_values"
    ADD CONSTRAINT "tracking_values_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_roles"
    ADD CONSTRAINT "user_roles_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_roles"
    ADD CONSTRAINT "user_roles_user_id_organisation_id_role_key" UNIQUE ("user_id", "organisation_id", "role");



ALTER TABLE ONLY "public"."user_theme_entitlements"
    ADD CONSTRAINT "user_theme_entitlements_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_theme_entitlements"
    ADD CONSTRAINT "user_theme_entitlements_user_id_theme_id_key" UNIQUE ("user_id", "theme_id");



ALTER TABLE ONLY "public"."user_theme_preferences"
    ADD CONSTRAINT "user_theme_preferences_pkey" PRIMARY KEY ("user_id");



ALTER TABLE ONLY "public"."whatsapp_pending_selections"
    ADD CONSTRAINT "whatsapp_pending_selections_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."xero_connections"
    ADD CONSTRAINT "xero_connections_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."xero_tenants"
    ADD CONSTRAINT "xero_tenants_pkey" PRIMARY KEY ("id");



CREATE INDEX "account_budgets_org_account_idx" ON "public"."account_budgets" USING "btree" ("organisation_id", "account_id");



CREATE UNIQUE INDEX "accounts_asset_type_role_uidx" ON "public"."accounts" USING "btree" ("managed_asset_type_id", "asset_account_role") WHERE ("managed_asset_type_id" IS NOT NULL);



CREATE UNIQUE INDEX "accounts_org_system_key_uidx" ON "public"."accounts" USING "btree" ("organisation_id", "system_key") WHERE ("system_key" IS NOT NULL);



CREATE INDEX "asset_types_org_active_idx" ON "public"."asset_types" USING "btree" ("organisation_id", "active", "name");



CREATE UNIQUE INDEX "asset_types_org_name_ci_uidx" ON "public"."asset_types" USING "btree" ("organisation_id", "lower"("btrim"("name")));



CREATE INDEX "bank_accounts_org_idx" ON "public"."bank_accounts" USING "btree" ("organisation_id", "active", "account_type");



CREATE INDEX "bank_audit_events_org_idx" ON "public"."bank_audit_events" USING "btree" ("organisation_id", "created_at" DESC);



CREATE INDEX "bank_parsing_rules_org_idx" ON "public"."bank_parsing_rules" USING "btree" ("organisation_id", "active", "institution_name", "account_type");



CREATE INDEX "bank_statement_lines_bank_reference_idx" ON "public"."bank_statement_lines" USING "btree" ("organisation_id", "bank_account_id", "bank_reference") WHERE ("bank_reference" IS NOT NULL);



CREATE INDEX "bank_statement_lines_hash_idx" ON "public"."bank_statement_lines" USING "btree" ("organisation_id", "bank_account_id", "transaction_hash");



CREATE INDEX "bank_statement_lines_upload_idx" ON "public"."bank_statement_lines" USING "btree" ("bank_statement_upload_id", "line_date", "id");



CREATE INDEX "bank_statement_uploads_extractor_idx" ON "public"."bank_statement_uploads" USING "btree" ("organisation_id", "extractor_type", "extractor_version", "source_format");



CREATE INDEX "bank_statement_uploads_file_sha_idx" ON "public"."bank_statement_uploads" USING "btree" ("organisation_id", "bank_account_id", "file_sha256") WHERE ("file_sha256" IS NOT NULL);



CREATE INDEX "bank_statement_uploads_org_idx" ON "public"."bank_statement_uploads" USING "btree" ("organisation_id", "bank_account_id", "uploaded_at" DESC);



CREATE INDEX "bank_transaction_rules_criteria_idx" ON "public"."bank_transaction_rules" USING "gin" ("criteria");



CREATE INDEX "bank_transaction_rules_org_idx" ON "public"."bank_transaction_rules" USING "btree" ("organisation_id", "active", "priority");



CREATE INDEX "bank_transaction_suggestions_line_idx" ON "public"."bank_transaction_suggestions" USING "btree" ("bank_statement_line_id", "confidence_score" DESC);



CREATE INDEX "consolidation_account_mappings_lookup_idx" ON "public"."consolidation_account_mappings" USING "btree" ("reporting_group_id", "entity_organisation_id", "local_account_id");



CREATE INDEX "consolidation_adjustment_lines_adjustment_idx" ON "public"."consolidation_adjustment_lines" USING "btree" ("adjustment_id");



CREATE INDEX "consolidation_adjustments_period_idx" ON "public"."consolidation_adjustments" USING "btree" ("reporting_group_id", "period_id", "status");



CREATE INDEX "consolidation_entity_balances_report_idx" ON "public"."consolidation_entity_balances" USING "btree" ("reporting_group_id", "period_id", "entity_organisation_id", "account_id");



CREATE INDEX "consolidation_periods_group_idx" ON "public"."consolidation_periods" USING "btree" ("reporting_group_id");



CREATE INDEX "document_pages_document_direction_idx" ON "public"."document_pages" USING "btree" ("document_direction");



CREATE INDEX "document_pages_group_idx" ON "public"."document_pages" USING "btree" ("document_group_key");



CREATE INDEX "document_pages_invoice_raw_idx" ON "public"."document_pages" USING "btree" ("invoice_raw_id");



CREATE INDEX "document_pages_job_idx" ON "public"."document_pages" USING "btree" ("job_id");



CREATE INDEX "document_pages_org_idx" ON "public"."document_pages" USING "btree" ("organisation_id");



CREATE INDEX "document_processing_jobs_batch_idx" ON "public"."document_processing_jobs" USING "btree" ("batch_id");



CREATE INDEX "document_processing_jobs_invoice_raw_idx" ON "public"."document_processing_jobs" USING "btree" ("invoice_raw_id");



CREATE INDEX "document_processing_jobs_org_idx" ON "public"."document_processing_jobs" USING "btree" ("organisation_id");



CREATE INDEX "document_processing_jobs_status_priority_idx" ON "public"."document_processing_jobs" USING "btree" ("status", "priority", "created_at");



CREATE INDEX "document_upload_batches_org_idx" ON "public"."document_upload_batches" USING "btree" ("organisation_id");



CREATE INDEX "document_upload_batches_status_idx" ON "public"."document_upload_batches" USING "btree" ("status");



CREATE INDEX "exchange_rates_lookup_idx" ON "public"."exchange_rates" USING "btree" ("reporting_group_id", "period_id", "from_currency", "to_currency", "rate_type", "rate_date");



CREATE INDEX "gl_journal_lines_journal_idx" ON "public"."gl_journal_lines" USING "btree" ("gl_journal_id", "sort_order");



CREATE UNIQUE INDEX "gl_journals_one_active_invoice_source" ON "public"."gl_journals" USING "btree" ("organisation_id", "source_id") WHERE (("source_type" = 'invoice'::"text") AND ("status" <> 'reversed'::"text"));



CREATE INDEX "gl_journals_reversal_idx" ON "public"."gl_journals" USING "btree" ("organisation_id", "reversal_of_journal_id") WHERE ("reversal_of_journal_id" IS NOT NULL);



CREATE INDEX "idx_audit_log_created" ON "public"."audit_log" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_audit_log_entity" ON "public"."audit_log" USING "btree" ("entity_type", "entity_id");



CREATE INDEX "idx_audit_log_org" ON "public"."audit_log" USING "btree" ("organisation_id");



CREATE INDEX "idx_bills_synced_org" ON "public"."bills_synced" USING "btree" ("organisation_id");



CREATE INDEX "idx_bills_synced_status" ON "public"."bills_synced" USING "btree" ("sync_status");



CREATE INDEX "idx_bills_synced_supplier" ON "public"."bills_synced" USING "btree" ("supplier_id");



CREATE INDEX "idx_emails_sent_org" ON "public"."emails_sent" USING "btree" ("organisation_id");



CREATE INDEX "idx_emails_sent_status" ON "public"."emails_sent" USING "btree" ("send_status");



CREATE INDEX "idx_emails_sent_supplier" ON "public"."emails_sent" USING "btree" ("supplier_id");



CREATE INDEX "idx_invoices_extracted_document_type" ON "public"."invoices_extracted" USING "btree" ("organisation_id", "document_type");



CREATE INDEX "idx_invoices_extracted_invnum" ON "public"."invoices_extracted" USING "btree" ("invoice_number");



CREATE INDEX "idx_invoices_extracted_number" ON "public"."invoices_extracted" USING "btree" ("organisation_id", "invoice_number");



CREATE INDEX "idx_invoices_extracted_org" ON "public"."invoices_extracted" USING "btree" ("organisation_id");



CREATE INDEX "idx_invoices_extracted_review" ON "public"."invoices_extracted" USING "btree" ("review_status");



CREATE INDEX "idx_invoices_extracted_supplier" ON "public"."invoices_extracted" USING "btree" ("supplier_id");



CREATE INDEX "idx_invoices_raw_org" ON "public"."invoices_raw" USING "btree" ("organisation_id");



CREATE INDEX "idx_invoices_raw_parse_status" ON "public"."invoices_raw" USING "btree" ("parse_status");



CREATE INDEX "idx_invoices_raw_supplier" ON "public"."invoices_raw" USING "btree" ("supplier_id");



CREATE INDEX "idx_payments_match_status" ON "public"."payments" USING "btree" ("match_status");



CREATE INDEX "idx_payments_org" ON "public"."payments" USING "btree" ("organisation_id");



CREATE INDEX "idx_payments_supplier" ON "public"."payments" USING "btree" ("supplier_id");



CREATE INDEX "idx_recon_lines_match" ON "public"."reconciliation_lines" USING "btree" ("match_status");



CREATE INDEX "idx_recon_lines_org" ON "public"."reconciliation_lines" USING "btree" ("organisation_id");



CREATE INDEX "idx_recon_lines_recon" ON "public"."reconciliation_lines" USING "btree" ("reconciliation_id");



CREATE INDEX "idx_reconciliations_org" ON "public"."reconciliations" USING "btree" ("organisation_id");



CREATE INDEX "idx_reconciliations_status" ON "public"."reconciliations" USING "btree" ("reconciliation_status");



CREATE INDEX "idx_reconciliations_supplier" ON "public"."reconciliations" USING "btree" ("supplier_id");



CREATE INDEX "idx_remittances_org" ON "public"."remittances" USING "btree" ("organisation_id");



CREATE INDEX "idx_remittances_status" ON "public"."remittances" USING "btree" ("remittance_status");



CREATE INDEX "idx_remittances_supplier" ON "public"."remittances" USING "btree" ("supplier_id");



CREATE INDEX "idx_statement_lines_invnum" ON "public"."statement_lines" USING "btree" ("invoice_number");



CREATE INDEX "idx_statement_lines_org" ON "public"."statement_lines" USING "btree" ("organisation_id");



CREATE INDEX "idx_statement_lines_stmt" ON "public"."statement_lines" USING "btree" ("statement_raw_id");



CREATE INDEX "idx_statements_raw_date" ON "public"."statements_raw" USING "btree" ("statement_date");



CREATE INDEX "idx_statements_raw_org" ON "public"."statements_raw" USING "btree" ("organisation_id");



CREATE INDEX "idx_statements_raw_supplier" ON "public"."statements_raw" USING "btree" ("supplier_id");



CREATE INDEX "idx_supplier_contacts_org" ON "public"."supplier_contacts" USING "btree" ("organisation_id");



CREATE INDEX "idx_supplier_contacts_supplier" ON "public"."supplier_contacts" USING "btree" ("supplier_id");



CREATE INDEX "idx_suppliers_bank_account" ON "public"."suppliers" USING "btree" ("bank_account_number");



CREATE INDEX "idx_suppliers_company_reg" ON "public"."suppliers" USING "btree" ("company_registration_number");



CREATE INDEX "idx_suppliers_name" ON "public"."suppliers" USING "btree" ("organisation_id", "supplier_name");



CREATE INDEX "idx_suppliers_org" ON "public"."suppliers" USING "btree" ("organisation_id");



CREATE INDEX "idx_suppliers_vat" ON "public"."suppliers" USING "btree" ("vat_number");



CREATE INDEX "idx_user_roles_user_org" ON "public"."user_roles" USING "btree" ("user_id", "organisation_id");



CREATE INDEX "idx_xero_connections_org" ON "public"."xero_connections" USING "btree" ("organisation_id");



CREATE INDEX "idx_xero_tenants_conn" ON "public"."xero_tenants" USING "btree" ("xero_connection_id");



CREATE INDEX "idx_xero_tenants_org" ON "public"."xero_tenants" USING "btree" ("organisation_id");



CREATE UNIQUE INDEX "invoice_agent_suggestions_extracted_fp_idx" ON "public"."invoice_agent_suggestions" USING "btree" ("organisation_id", "invoice_extracted_id", "fingerprint") WHERE ("invoice_extracted_id" IS NOT NULL);



CREATE INDEX "invoice_agent_suggestions_extracted_idx" ON "public"."invoice_agent_suggestions" USING "btree" ("invoice_extracted_id", "status");



CREATE INDEX "invoice_agent_suggestions_org_idx" ON "public"."invoice_agent_suggestions" USING "btree" ("organisation_id", "status", "created_at" DESC);



CREATE UNIQUE INDEX "invoice_agent_suggestions_raw_fp_idx" ON "public"."invoice_agent_suggestions" USING "btree" ("organisation_id", "invoice_raw_id", "fingerprint") WHERE (("invoice_extracted_id" IS NULL) AND ("invoice_raw_id" IS NOT NULL));



CREATE INDEX "invoice_agent_suggestions_raw_idx" ON "public"."invoice_agent_suggestions" USING "btree" ("invoice_raw_id", "status");



CREATE INDEX "invoice_audit_events_event_type_idx" ON "public"."invoice_audit_events" USING "btree" ("event_type");



CREATE INDEX "invoice_audit_events_invoice_extracted_idx" ON "public"."invoice_audit_events" USING "btree" ("invoice_extracted_id");



CREATE INDEX "invoice_audit_events_invoice_raw_idx" ON "public"."invoice_audit_events" USING "btree" ("invoice_raw_id");



CREATE INDEX "invoice_audit_events_job_idx" ON "public"."invoice_audit_events" USING "btree" ("job_id");



CREATE INDEX "invoice_audit_events_org_idx" ON "public"."invoice_audit_events" USING "btree" ("organisation_id");



CREATE INDEX "invoice_extraction_feedback_field_name_idx" ON "public"."invoice_extraction_feedback" USING "btree" ("field_name");



CREATE INDEX "invoice_extraction_feedback_invoice_extracted_id_idx" ON "public"."invoice_extraction_feedback" USING "btree" ("invoice_extracted_id");



CREATE INDEX "invoice_extraction_feedback_invoice_raw_id_idx" ON "public"."invoice_extraction_feedback" USING "btree" ("invoice_raw_id");



CREATE INDEX "invoice_extraction_feedback_organisation_id_idx" ON "public"."invoice_extraction_feedback" USING "btree" ("organisation_id");



CREATE INDEX "invoice_extraction_feedback_supplier_id_idx" ON "public"."invoice_extraction_feedback" USING "btree" ("supplier_id");



CREATE INDEX "invoice_line_item_allocations_line_idx" ON "public"."invoice_line_item_allocations" USING "btree" ("invoice_line_item_id", "sort_order");



CREATE INDEX "invoice_line_item_allocations_org_idx" ON "public"."invoice_line_item_allocations" USING "btree" ("organisation_id");



CREATE INDEX "invoice_line_items_code_idx" ON "public"."invoice_line_items" USING "btree" ("code") WHERE ("code" IS NOT NULL);



CREATE INDEX "invoice_line_items_invoice_extracted_id_idx" ON "public"."invoice_line_items" USING "btree" ("invoice_extracted_id");



CREATE INDEX "invoice_line_items_organisation_id_idx" ON "public"."invoice_line_items" USING "btree" ("organisation_id");



CREATE INDEX "invoice_page_groups_invoice_raw_idx" ON "public"."invoice_page_groups" USING "btree" ("invoice_raw_id");



CREATE INDEX "invoice_parse_attempts_extracted_idx" ON "public"."invoice_parse_attempts" USING "btree" ("invoice_extracted_id");



CREATE INDEX "invoice_parse_attempts_raw_idx" ON "public"."invoice_parse_attempts" USING "btree" ("invoice_raw_id", "attempt_number");



CREATE INDEX "invoice_parse_attempts_selected_idx" ON "public"."invoice_parse_attempts" USING "btree" ("invoice_raw_id", "selected");



CREATE INDEX "invoice_supplier_comparison_ignores_invoice_idx" ON "public"."invoice_supplier_comparison_ignores" USING "btree" ("invoice_extracted_id");



CREATE INDEX "invoice_supplier_comparison_ignores_org_idx" ON "public"."invoice_supplier_comparison_ignores" USING "btree" ("organisation_id", "created_at" DESC);



CREATE INDEX "invoices_extracted_document_direction_idx" ON "public"."invoices_extracted" USING "btree" ("document_direction");



CREATE UNIQUE INDEX "invoices_extracted_raw_unique" ON "public"."invoices_extracted" USING "btree" ("invoice_raw_id");



CREATE INDEX "invoices_extracted_supplier_branch_idx" ON "public"."invoices_extracted" USING "btree" ("supplier_branch_id");



CREATE INDEX "invoices_extracted_validation_status_idx" ON "public"."invoices_extracted" USING "btree" ("validation_status");



CREATE INDEX "organisation_users_org_idx" ON "public"."organisation_users" USING "btree" ("organisation_id");



CREATE INDEX "organisation_users_user_idx" ON "public"."organisation_users" USING "btree" ("user_id");



CREATE INDEX "reconciliation_results_line_idx" ON "public"."reconciliation_results" USING "btree" ("line_id");



CREATE INDEX "reconciliation_results_recon_idx" ON "public"."reconciliation_results" USING "btree" ("reconciliation_id");



CREATE INDEX "reconciliation_results_statement_idx" ON "public"."reconciliation_results" USING "btree" ("statement_raw_id");



CREATE INDEX "reporting_group_entities_group_idx" ON "public"."reporting_group_entities" USING "btree" ("reporting_group_id");



CREATE INDEX "reporting_group_entities_org_idx" ON "public"."reporting_group_entities" USING "btree" ("organisation_id");



CREATE INDEX "reporting_group_users_user_idx" ON "public"."reporting_group_users" USING "btree" ("user_id");



CREATE INDEX "reporting_groups_owner_org_idx" ON "public"."reporting_groups" USING "btree" ("owner_organisation_id");



CREATE INDEX "statement_lines_match_status_idx" ON "public"."statement_lines" USING "btree" ("match_status");



CREATE INDEX "statement_lines_review_status_idx" ON "public"."statement_lines" USING "btree" ("review_status");



CREATE INDEX "supplier_allocation_rule_splits_rule_idx" ON "public"."supplier_line_item_allocation_rule_splits" USING "btree" ("rule_id", "sort_order");



CREATE INDEX "supplier_allocation_rules_org_idx" ON "public"."supplier_line_item_allocation_rules" USING "btree" ("organisation_id");



CREATE INDEX "supplier_allocation_rules_supplier_idx" ON "public"."supplier_line_item_allocation_rules" USING "btree" ("supplier_id", "active", "priority");



CREATE INDEX "supplier_branches_org_supplier_idx" ON "public"."supplier_branches" USING "btree" ("organisation_id", "supplier_id", "active");



CREATE INDEX "supplier_branches_vat_idx" ON "public"."supplier_branches" USING "btree" ("organisation_id", "vat_number") WHERE ("vat_number" IS NOT NULL);



CREATE UNIQUE INDEX "tracking_dimensions_one_is_function_driver_per_org" ON "public"."tracking_dimensions" USING "btree" ("organisation_id") WHERE "is_income_statement_function_driver";



CREATE INDEX "user_theme_entitlements_theme_idx" ON "public"."user_theme_entitlements" USING "btree" ("theme_id");



CREATE INDEX "user_theme_entitlements_user_idx" ON "public"."user_theme_entitlements" USING "btree" ("user_id");



CREATE INDEX "user_theme_preferences_active_theme_idx" ON "public"."user_theme_preferences" USING "btree" ("active_theme_id");



CREATE INDEX "whatsapp_pending_expires_idx" ON "public"."whatsapp_pending_selections" USING "btree" ("expires_at");



CREATE UNIQUE INDEX "whatsapp_pending_phone_idx" ON "public"."whatsapp_pending_selections" USING "btree" ("phone");



CREATE OR REPLACE TRIGGER "account_budgets_set_updated_at" BEFORE UPDATE ON "public"."account_budgets" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "asset_types_set_updated_at" BEFORE UPDATE ON "public"."asset_types" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "bank_accounts_set_updated_at" BEFORE UPDATE ON "public"."bank_accounts" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "bank_statement_lines_set_updated_at" BEFORE UPDATE ON "public"."bank_statement_lines" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "bank_statement_uploads_set_updated_at" BEFORE UPDATE ON "public"."bank_statement_uploads" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "consolidation_account_mappings_set_updated_at" BEFORE UPDATE ON "public"."consolidation_account_mappings" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "consolidation_adjustments_set_updated_at" BEFORE UPDATE ON "public"."consolidation_adjustments" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "consolidation_entity_balances_set_updated_at" BEFORE UPDATE ON "public"."consolidation_entity_balances" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "consolidation_periods_set_updated_at" BEFORE UPDATE ON "public"."consolidation_periods" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "document_processing_jobs_set_updated_at" BEFORE UPDATE ON "public"."document_processing_jobs" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "document_upload_batches_set_updated_at" BEFORE UPDATE ON "public"."document_upload_batches" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "exchange_rates_set_updated_at" BEFORE UPDATE ON "public"."exchange_rates" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "invoice_agent_suggestions_set_updated_at" BEFORE UPDATE ON "public"."invoice_agent_suggestions" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "invoice_line_item_allocations_set_updated_at" BEFORE UPDATE ON "public"."invoice_line_item_allocations" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "organisation_invoice_branding_set_updated_at" BEFORE UPDATE ON "public"."organisation_invoice_branding" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "organisation_module_settings_set_updated_at" BEFORE UPDATE ON "public"."organisation_module_settings" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "organisation_users_protect_last_owner" BEFORE DELETE OR UPDATE ON "public"."organisation_users" FOR EACH ROW EXECUTE FUNCTION "public"."protect_last_owner"();



CREATE OR REPLACE TRIGGER "organisation_users_set_updated_at" BEFORE UPDATE ON "public"."organisation_users" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "organisations_prevent_duplicate_name_for_user" BEFORE INSERT OR UPDATE OF "name" ON "public"."organisations" FOR EACH ROW EXECUTE FUNCTION "public"."prevent_duplicate_org_name_for_user"();



CREATE OR REPLACE TRIGGER "organisations_set_updated_at" BEFORE UPDATE ON "public"."organisations" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "reporting_group_entities_set_updated_at" BEFORE UPDATE ON "public"."reporting_group_entities" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "reporting_group_users_set_updated_at" BEFORE UPDATE ON "public"."reporting_group_users" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "reporting_groups_set_updated_at" BEFORE UPDATE ON "public"."reporting_groups" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "supplier_allocation_rule_splits_set_updated_at" BEFORE UPDATE ON "public"."supplier_line_item_allocation_rule_splits" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "supplier_allocation_rules_set_updated_at" BEFORE UPDATE ON "public"."supplier_line_item_allocation_rules" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "supplier_branches_set_updated_at" BEFORE UPDATE ON "public"."supplier_branches" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "supplier_kyc_requests_set_updated_at" BEFORE UPDATE ON "public"."supplier_kyc_requests" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "themes_set_updated_at" BEFORE UPDATE ON "public"."themes" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "trg_assign_org_inbound_email" BEFORE INSERT ON "public"."organisations" FOR EACH ROW EXECUTE FUNCTION "public"."assign_org_inbound_email"();



CREATE OR REPLACE TRIGGER "trg_bills_synced_updated_at" BEFORE UPDATE ON "public"."bills_synced" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_emails_sent_updated_at" BEFORE UPDATE ON "public"."emails_sent" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_invoices_extracted_updated_at" BEFORE UPDATE ON "public"."invoices_extracted" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_invoices_raw_updated_at" BEFORE UPDATE ON "public"."invoices_raw" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_on_organisation_created" AFTER INSERT ON "public"."organisations" FOR EACH ROW EXECUTE FUNCTION "public"."on_organisation_created"();



CREATE OR REPLACE TRIGGER "trg_organisations_updated_at" BEFORE UPDATE ON "public"."organisations" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_payments_updated_at" BEFORE UPDATE ON "public"."payments" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_prevent_system_account_delete" BEFORE DELETE ON "public"."accounts" FOR EACH ROW EXECUTE FUNCTION "public"."prevent_system_account_delete"();



CREATE OR REPLACE TRIGGER "trg_profiles_updated_at" BEFORE UPDATE ON "public"."profiles" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_protect_system_accounts" BEFORE UPDATE ON "public"."accounts" FOR EACH ROW EXECUTE FUNCTION "public"."protect_system_accounts"();



CREATE OR REPLACE TRIGGER "trg_reconciliation_lines_updated_at" BEFORE UPDATE ON "public"."reconciliation_lines" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_reconciliations_updated_at" BEFORE UPDATE ON "public"."reconciliations" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_remittances_updated_at" BEFORE UPDATE ON "public"."remittances" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_statement_lines_updated_at" BEFORE UPDATE ON "public"."statement_lines" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_statements_raw_updated_at" BEFORE UPDATE ON "public"."statements_raw" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_supplier_contacts_updated_at" BEFORE UPDATE ON "public"."supplier_contacts" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_suppliers_updated_at" BEFORE UPDATE ON "public"."suppliers" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_xero_connections_updated_at" BEFORE UPDATE ON "public"."xero_connections" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "trg_xero_tenants_updated_at" BEFORE UPDATE ON "public"."xero_tenants" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();



CREATE OR REPLACE TRIGGER "user_theme_preferences_set_updated_at" BEFORE UPDATE ON "public"."user_theme_preferences" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



ALTER TABLE ONLY "public"."account_budgets"
    ADD CONSTRAINT "account_budgets_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "public"."accounts"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."account_budgets"
    ADD CONSTRAINT "account_budgets_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."account_budgets"
    ADD CONSTRAINT "account_budgets_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."account_budgets"
    ADD CONSTRAINT "account_budgets_tracking_value_id_fkey" FOREIGN KEY ("tracking_value_id") REFERENCES "public"."tracking_values"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."account_mappings"
    ADD CONSTRAINT "account_mappings_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "public"."accounts"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."account_mappings"
    ADD CONSTRAINT "account_mappings_integration_id_fkey" FOREIGN KEY ("integration_id") REFERENCES "public"."org_integrations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."accounts"
    ADD CONSTRAINT "accounts_managed_asset_type_id_fkey" FOREIGN KEY ("managed_asset_type_id") REFERENCES "public"."asset_types"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."accounts"
    ADD CONSTRAINT "accounts_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."asset_types"
    ADD CONSTRAINT "asset_types_accumulated_account_id_fkey" FOREIGN KEY ("accumulated_account_id") REFERENCES "public"."accounts"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."asset_types"
    ADD CONSTRAINT "asset_types_archived_by_fkey" FOREIGN KEY ("archived_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."asset_types"
    ADD CONSTRAINT "asset_types_cost_account_id_fkey" FOREIGN KEY ("cost_account_id") REFERENCES "public"."accounts"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."asset_types"
    ADD CONSTRAINT "asset_types_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."asset_types"
    ADD CONSTRAINT "asset_types_expense_account_id_fkey" FOREIGN KEY ("expense_account_id") REFERENCES "public"."accounts"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."asset_types"
    ADD CONSTRAINT "asset_types_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."audit_log"
    ADD CONSTRAINT "audit_log_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_accounts"
    ADD CONSTRAINT "bank_accounts_gl_account_id_fkey" FOREIGN KEY ("gl_account_id") REFERENCES "public"."accounts"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_accounts"
    ADD CONSTRAINT "bank_accounts_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_audit_events"
    ADD CONSTRAINT "bank_audit_events_actor_user_id_fkey" FOREIGN KEY ("actor_user_id") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_audit_events"
    ADD CONSTRAINT "bank_audit_events_bank_account_id_fkey" FOREIGN KEY ("bank_account_id") REFERENCES "public"."bank_accounts"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_audit_events"
    ADD CONSTRAINT "bank_audit_events_bank_statement_line_id_fkey" FOREIGN KEY ("bank_statement_line_id") REFERENCES "public"."bank_statement_lines"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_audit_events"
    ADD CONSTRAINT "bank_audit_events_bank_statement_upload_id_fkey" FOREIGN KEY ("bank_statement_upload_id") REFERENCES "public"."bank_statement_uploads"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_audit_events"
    ADD CONSTRAINT "bank_audit_events_gl_journal_id_fkey" FOREIGN KEY ("gl_journal_id") REFERENCES "public"."gl_journals"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_audit_events"
    ADD CONSTRAINT "bank_audit_events_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_parsing_rules"
    ADD CONSTRAINT "bank_parsing_rules_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_parsing_rules"
    ADD CONSTRAINT "bank_parsing_rules_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_statement_lines"
    ADD CONSTRAINT "bank_statement_lines_bank_account_id_fkey" FOREIGN KEY ("bank_account_id") REFERENCES "public"."bank_accounts"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_statement_lines"
    ADD CONSTRAINT "bank_statement_lines_bank_statement_upload_id_fkey" FOREIGN KEY ("bank_statement_upload_id") REFERENCES "public"."bank_statement_uploads"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_statement_lines"
    ADD CONSTRAINT "bank_statement_lines_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_statement_lines"
    ADD CONSTRAINT "bank_statement_lines_reviewed_by_fkey" FOREIGN KEY ("reviewed_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_statement_lines"
    ADD CONSTRAINT "bank_statement_lines_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id");



ALTER TABLE ONLY "public"."bank_statement_uploads"
    ADD CONSTRAINT "bank_statement_uploads_bank_account_id_fkey" FOREIGN KEY ("bank_account_id") REFERENCES "public"."bank_accounts"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_statement_uploads"
    ADD CONSTRAINT "bank_statement_uploads_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_statement_uploads"
    ADD CONSTRAINT "bank_statement_uploads_uploaded_by_fkey" FOREIGN KEY ("uploaded_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_transaction_rules"
    ADD CONSTRAINT "bank_transaction_rules_bank_account_id_fkey" FOREIGN KEY ("bank_account_id") REFERENCES "public"."bank_accounts"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_transaction_rules"
    ADD CONSTRAINT "bank_transaction_rules_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_transaction_rules"
    ADD CONSTRAINT "bank_transaction_rules_gl_account_id_fkey" FOREIGN KEY ("gl_account_id") REFERENCES "public"."accounts"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_transaction_rules"
    ADD CONSTRAINT "bank_transaction_rules_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_transaction_rules"
    ADD CONSTRAINT "bank_transaction_rules_source_bank_statement_line_id_fkey" FOREIGN KEY ("source_bank_statement_line_id") REFERENCES "public"."bank_statement_lines"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_transaction_suggestions"
    ADD CONSTRAINT "bank_transaction_suggestions_bank_statement_line_id_fkey" FOREIGN KEY ("bank_statement_line_id") REFERENCES "public"."bank_statement_lines"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_transaction_suggestions"
    ADD CONSTRAINT "bank_transaction_suggestions_matched_invoice_id_fkey" FOREIGN KEY ("matched_invoice_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bank_transaction_suggestions"
    ADD CONSTRAINT "bank_transaction_suggestions_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bank_transaction_suggestions"
    ADD CONSTRAINT "bank_transaction_suggestions_suggested_account_id_fkey" FOREIGN KEY ("suggested_account_id") REFERENCES "public"."accounts"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."bills_synced"
    ADD CONSTRAINT "bills_synced_invoice_extracted_id_fkey" FOREIGN KEY ("invoice_extracted_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bills_synced"
    ADD CONSTRAINT "bills_synced_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."bills_synced"
    ADD CONSTRAINT "bills_synced_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_account_mappings"
    ADD CONSTRAINT "consolidation_account_mappings_entity_organisation_id_fkey" FOREIGN KEY ("entity_organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_account_mappings"
    ADD CONSTRAINT "consolidation_account_mappings_group_account_id_fkey" FOREIGN KEY ("group_account_id") REFERENCES "public"."accounts"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_account_mappings"
    ADD CONSTRAINT "consolidation_account_mappings_local_account_id_fkey" FOREIGN KEY ("local_account_id") REFERENCES "public"."accounts"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_account_mappings"
    ADD CONSTRAINT "consolidation_account_mappings_reporting_group_id_fkey" FOREIGN KEY ("reporting_group_id") REFERENCES "public"."reporting_groups"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_adjustment_lines"
    ADD CONSTRAINT "consolidation_adjustment_lines_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "public"."accounts"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."consolidation_adjustment_lines"
    ADD CONSTRAINT "consolidation_adjustment_lines_adjustment_id_fkey" FOREIGN KEY ("adjustment_id") REFERENCES "public"."consolidation_adjustments"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_adjustment_lines"
    ADD CONSTRAINT "consolidation_adjustment_lines_entity_organisation_id_fkey" FOREIGN KEY ("entity_organisation_id") REFERENCES "public"."organisations"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."consolidation_adjustments"
    ADD CONSTRAINT "consolidation_adjustments_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."consolidation_adjustments"
    ADD CONSTRAINT "consolidation_adjustments_period_id_fkey" FOREIGN KEY ("period_id") REFERENCES "public"."consolidation_periods"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_adjustments"
    ADD CONSTRAINT "consolidation_adjustments_posted_by_fkey" FOREIGN KEY ("posted_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."consolidation_adjustments"
    ADD CONSTRAINT "consolidation_adjustments_reporting_group_id_fkey" FOREIGN KEY ("reporting_group_id") REFERENCES "public"."reporting_groups"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_adjustments"
    ADD CONSTRAINT "consolidation_adjustments_reversed_by_fkey" FOREIGN KEY ("reversed_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."consolidation_entity_balances"
    ADD CONSTRAINT "consolidation_entity_balances_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "public"."accounts"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_entity_balances"
    ADD CONSTRAINT "consolidation_entity_balances_entity_organisation_id_fkey" FOREIGN KEY ("entity_organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_entity_balances"
    ADD CONSTRAINT "consolidation_entity_balances_period_id_fkey" FOREIGN KEY ("period_id") REFERENCES "public"."consolidation_periods"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_entity_balances"
    ADD CONSTRAINT "consolidation_entity_balances_reporting_group_id_fkey" FOREIGN KEY ("reporting_group_id") REFERENCES "public"."reporting_groups"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."consolidation_periods"
    ADD CONSTRAINT "consolidation_periods_locked_by_fkey" FOREIGN KEY ("locked_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."consolidation_periods"
    ADD CONSTRAINT "consolidation_periods_reporting_group_id_fkey" FOREIGN KEY ("reporting_group_id") REFERENCES "public"."reporting_groups"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."document_pages"
    ADD CONSTRAINT "document_pages_invoice_raw_id_fkey" FOREIGN KEY ("invoice_raw_id") REFERENCES "public"."invoices_raw"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."document_pages"
    ADD CONSTRAINT "document_pages_job_id_fkey" FOREIGN KEY ("job_id") REFERENCES "public"."document_processing_jobs"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."document_pages"
    ADD CONSTRAINT "document_pages_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."document_processing_jobs"
    ADD CONSTRAINT "document_processing_jobs_batch_id_fkey" FOREIGN KEY ("batch_id") REFERENCES "public"."document_upload_batches"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."document_processing_jobs"
    ADD CONSTRAINT "document_processing_jobs_extracted_invoice_id_fkey" FOREIGN KEY ("extracted_invoice_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."document_processing_jobs"
    ADD CONSTRAINT "document_processing_jobs_invoice_raw_id_fkey" FOREIGN KEY ("invoice_raw_id") REFERENCES "public"."invoices_raw"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."document_processing_jobs"
    ADD CONSTRAINT "document_processing_jobs_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."document_upload_batches"
    ADD CONSTRAINT "document_upload_batches_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."emails_sent"
    ADD CONSTRAINT "emails_sent_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."emails_sent"
    ADD CONSTRAINT "emails_sent_related_invoice_id_fkey" FOREIGN KEY ("related_invoice_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."emails_sent"
    ADD CONSTRAINT "emails_sent_related_statement_id_fkey" FOREIGN KEY ("related_statement_id") REFERENCES "public"."statements_raw"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."emails_sent"
    ADD CONSTRAINT "emails_sent_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."exchange_rates"
    ADD CONSTRAINT "exchange_rates_period_id_fkey" FOREIGN KEY ("period_id") REFERENCES "public"."consolidation_periods"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."exchange_rates"
    ADD CONSTRAINT "exchange_rates_reporting_group_id_fkey" FOREIGN KEY ("reporting_group_id") REFERENCES "public"."reporting_groups"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."gl_journal_lines"
    ADD CONSTRAINT "gl_journal_lines_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "public"."accounts"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."gl_journal_lines"
    ADD CONSTRAINT "gl_journal_lines_gl_journal_id_fkey" FOREIGN KEY ("gl_journal_id") REFERENCES "public"."gl_journals"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."gl_journal_lines"
    ADD CONSTRAINT "gl_journal_lines_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."gl_journals"
    ADD CONSTRAINT "gl_journals_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."gl_journals"
    ADD CONSTRAINT "gl_journals_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."gl_journals"
    ADD CONSTRAINT "gl_journals_posted_by_fkey" FOREIGN KEY ("posted_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."gl_journals"
    ADD CONSTRAINT "gl_journals_reversal_of_journal_id_fkey" FOREIGN KEY ("reversal_of_journal_id") REFERENCES "public"."gl_journals"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."gl_journals"
    ADD CONSTRAINT "gl_journals_reversed_by_fkey" FOREIGN KEY ("reversed_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."invoice_agent_suggestions"
    ADD CONSTRAINT "invoice_agent_suggestions_invoice_extracted_id_fkey" FOREIGN KEY ("invoice_extracted_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_agent_suggestions"
    ADD CONSTRAINT "invoice_agent_suggestions_invoice_raw_id_fkey" FOREIGN KEY ("invoice_raw_id") REFERENCES "public"."invoices_raw"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_agent_suggestions"
    ADD CONSTRAINT "invoice_agent_suggestions_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_audit_events"
    ADD CONSTRAINT "invoice_audit_events_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_audit_log"
    ADD CONSTRAINT "invoice_audit_log_invoice_id_fkey" FOREIGN KEY ("invoice_id") REFERENCES "public"."invoices_extracted"("id");



ALTER TABLE ONLY "public"."invoice_extraction_feedback"
    ADD CONSTRAINT "invoice_extraction_feedback_invoice_extracted_id_fkey" FOREIGN KEY ("invoice_extracted_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_extraction_feedback"
    ADD CONSTRAINT "invoice_extraction_feedback_invoice_raw_id_fkey" FOREIGN KEY ("invoice_raw_id") REFERENCES "public"."invoices_raw"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_extraction_feedback"
    ADD CONSTRAINT "invoice_extraction_feedback_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."invoice_line_item_allocations"
    ADD CONSTRAINT "invoice_line_item_allocations_invoice_line_item_id_fkey" FOREIGN KEY ("invoice_line_item_id") REFERENCES "public"."invoice_line_items"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_line_item_allocations"
    ADD CONSTRAINT "invoice_line_item_allocations_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_line_items"
    ADD CONSTRAINT "invoice_line_items_invoice_extracted_id_fkey" FOREIGN KEY ("invoice_extracted_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_page_groups"
    ADD CONSTRAINT "invoice_page_groups_invoice_raw_id_fkey" FOREIGN KEY ("invoice_raw_id") REFERENCES "public"."invoices_raw"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_supplier_comparison_ignores"
    ADD CONSTRAINT "invoice_supplier_comparison_ignores_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."invoice_supplier_comparison_ignores"
    ADD CONSTRAINT "invoice_supplier_comparison_ignores_invoice_extracted_id_fkey" FOREIGN KEY ("invoice_extracted_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_supplier_comparison_ignores"
    ADD CONSTRAINT "invoice_supplier_comparison_ignores_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoice_supplier_comparison_ignores"
    ADD CONSTRAINT "invoice_supplier_comparison_ignores_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."invoices_extracted"
    ADD CONSTRAINT "invoices_extracted_gl_journal_id_fkey" FOREIGN KEY ("gl_journal_id") REFERENCES "public"."gl_journals"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."invoices_extracted"
    ADD CONSTRAINT "invoices_extracted_invoice_raw_id_fkey" FOREIGN KEY ("invoice_raw_id") REFERENCES "public"."invoices_raw"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoices_extracted"
    ADD CONSTRAINT "invoices_extracted_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoices_extracted"
    ADD CONSTRAINT "invoices_extracted_posted_by_fkey" FOREIGN KEY ("posted_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."invoices_extracted"
    ADD CONSTRAINT "invoices_extracted_supplier_branch_id_fkey" FOREIGN KEY ("supplier_branch_id") REFERENCES "public"."supplier_branches"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."invoices_extracted"
    ADD CONSTRAINT "invoices_extracted_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."invoices_raw"
    ADD CONSTRAINT "invoices_raw_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."invoices_raw"
    ADD CONSTRAINT "invoices_raw_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."org_integrations"
    ADD CONSTRAINT "org_integrations_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."organisation_invoice_branding"
    ADD CONSTRAINT "organisation_invoice_branding_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."organisation_module_settings"
    ADD CONSTRAINT "organisation_module_settings_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."organisation_users"
    ADD CONSTRAINT "organisation_users_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."organisation_users"
    ADD CONSTRAINT "organisation_users_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."payments"
    ADD CONSTRAINT "payments_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."payments"
    ADD CONSTRAINT "payments_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_id_fkey" FOREIGN KEY ("id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reconciliation_lines"
    ADD CONSTRAINT "reconciliation_lines_bill_synced_id_fkey" FOREIGN KEY ("bill_synced_id") REFERENCES "public"."bills_synced"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."reconciliation_lines"
    ADD CONSTRAINT "reconciliation_lines_invoice_extracted_id_fkey" FOREIGN KEY ("invoice_extracted_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."reconciliation_lines"
    ADD CONSTRAINT "reconciliation_lines_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reconciliation_lines"
    ADD CONSTRAINT "reconciliation_lines_payment_id_fkey" FOREIGN KEY ("payment_id") REFERENCES "public"."payments"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."reconciliation_lines"
    ADD CONSTRAINT "reconciliation_lines_reconciliation_id_fkey" FOREIGN KEY ("reconciliation_id") REFERENCES "public"."reconciliations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reconciliation_lines"
    ADD CONSTRAINT "reconciliation_lines_statement_line_id_fkey" FOREIGN KEY ("statement_line_id") REFERENCES "public"."statement_lines"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."reconciliation_results"
    ADD CONSTRAINT "reconciliation_results_line_id_fkey" FOREIGN KEY ("line_id") REFERENCES "public"."statement_lines"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."reconciliation_results"
    ADD CONSTRAINT "reconciliation_results_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reconciliation_results"
    ADD CONSTRAINT "reconciliation_results_statement_raw_id_fkey" FOREIGN KEY ("statement_raw_id") REFERENCES "public"."statements_raw"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reconciliations"
    ADD CONSTRAINT "reconciliations_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reconciliations"
    ADD CONSTRAINT "reconciliations_statement_raw_id_fkey" FOREIGN KEY ("statement_raw_id") REFERENCES "public"."statements_raw"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."reconciliations"
    ADD CONSTRAINT "reconciliations_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."remittances"
    ADD CONSTRAINT "remittances_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."remittances"
    ADD CONSTRAINT "remittances_payment_id_fkey" FOREIGN KEY ("payment_id") REFERENCES "public"."payments"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."remittances"
    ADD CONSTRAINT "remittances_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reporting_group_entities"
    ADD CONSTRAINT "reporting_group_entities_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reporting_group_entities"
    ADD CONSTRAINT "reporting_group_entities_parent_entity_id_fkey" FOREIGN KEY ("parent_entity_id") REFERENCES "public"."reporting_group_entities"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."reporting_group_entities"
    ADD CONSTRAINT "reporting_group_entities_reporting_group_id_fkey" FOREIGN KEY ("reporting_group_id") REFERENCES "public"."reporting_groups"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reporting_group_users"
    ADD CONSTRAINT "reporting_group_users_reporting_group_id_fkey" FOREIGN KEY ("reporting_group_id") REFERENCES "public"."reporting_groups"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reporting_group_users"
    ADD CONSTRAINT "reporting_group_users_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reporting_groups"
    ADD CONSTRAINT "reporting_groups_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."reporting_groups"
    ADD CONSTRAINT "reporting_groups_owner_organisation_id_fkey" FOREIGN KEY ("owner_organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."statement_lines"
    ADD CONSTRAINT "statement_lines_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."statement_lines"
    ADD CONSTRAINT "statement_lines_statement_raw_id_fkey" FOREIGN KEY ("statement_raw_id") REFERENCES "public"."statements_raw"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."statement_lines"
    ADD CONSTRAINT "statement_lines_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."statements_raw"
    ADD CONSTRAINT "statements_raw_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."statements_raw"
    ADD CONSTRAINT "statements_raw_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."supplier_branches"
    ADD CONSTRAINT "supplier_branches_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_branches"
    ADD CONSTRAINT "supplier_branches_source_invoice_extracted_id_fkey" FOREIGN KEY ("source_invoice_extracted_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."supplier_branches"
    ADD CONSTRAINT "supplier_branches_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_contacts"
    ADD CONSTRAINT "supplier_contacts_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_contacts"
    ADD CONSTRAINT "supplier_contacts_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_extraction_profiles"
    ADD CONSTRAINT "supplier_extraction_profiles_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_kyc_documents"
    ADD CONSTRAINT "supplier_kyc_documents_kyc_request_id_fkey" FOREIGN KEY ("kyc_request_id") REFERENCES "public"."supplier_kyc_requests"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_kyc_documents"
    ADD CONSTRAINT "supplier_kyc_documents_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_kyc_documents"
    ADD CONSTRAINT "supplier_kyc_documents_uploaded_by_fkey" FOREIGN KEY ("uploaded_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."supplier_kyc_requests"
    ADD CONSTRAINT "supplier_kyc_requests_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_kyc_requests"
    ADD CONSTRAINT "supplier_kyc_requests_requested_by_fkey" FOREIGN KEY ("requested_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."supplier_kyc_requests"
    ADD CONSTRAINT "supplier_kyc_requests_reviewed_by_fkey" FOREIGN KEY ("reviewed_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."supplier_kyc_requests"
    ADD CONSTRAINT "supplier_kyc_requests_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_line_item_allocation_rules"
    ADD CONSTRAINT "supplier_line_item_allocation__source_invoice_extracted_id_fkey" FOREIGN KEY ("source_invoice_extracted_id") REFERENCES "public"."invoices_extracted"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."supplier_line_item_allocation_rule_splits"
    ADD CONSTRAINT "supplier_line_item_allocation_rule_splits_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_line_item_allocation_rule_splits"
    ADD CONSTRAINT "supplier_line_item_allocation_rule_splits_rule_id_fkey" FOREIGN KEY ("rule_id") REFERENCES "public"."supplier_line_item_allocation_rules"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_line_item_allocation_rules"
    ADD CONSTRAINT "supplier_line_item_allocation_rules_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."supplier_line_item_allocation_rules"
    ADD CONSTRAINT "supplier_line_item_allocation_rules_supplier_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "public"."suppliers"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."suppliers"
    ADD CONSTRAINT "suppliers_kyc_verified_by_fkey" FOREIGN KEY ("kyc_verified_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."suppliers"
    ADD CONSTRAINT "suppliers_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."tracking_dimensions"
    ADD CONSTRAINT "tracking_dimensions_default_value_id_fkey" FOREIGN KEY ("default_value_id") REFERENCES "public"."tracking_values"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."tracking_dimensions"
    ADD CONSTRAINT "tracking_dimensions_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."tracking_values"
    ADD CONSTRAINT "tracking_values_dimension_id_fkey" FOREIGN KEY ("dimension_id") REFERENCES "public"."tracking_dimensions"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."user_roles"
    ADD CONSTRAINT "user_roles_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."user_roles"
    ADD CONSTRAINT "user_roles_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."user_theme_entitlements"
    ADD CONSTRAINT "user_theme_entitlements_theme_id_fkey" FOREIGN KEY ("theme_id") REFERENCES "public"."themes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."user_theme_entitlements"
    ADD CONSTRAINT "user_theme_entitlements_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."user_theme_preferences"
    ADD CONSTRAINT "user_theme_preferences_active_theme_id_fkey" FOREIGN KEY ("active_theme_id") REFERENCES "public"."themes"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."user_theme_preferences"
    ADD CONSTRAINT "user_theme_preferences_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."whatsapp_pending_selections"
    ADD CONSTRAINT "whatsapp_pending_selections_uploaded_by_fkey" FOREIGN KEY ("uploaded_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."xero_connections"
    ADD CONSTRAINT "xero_connections_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."xero_tenants"
    ADD CONSTRAINT "xero_tenants_organisation_id_fkey" FOREIGN KEY ("organisation_id") REFERENCES "public"."organisations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."xero_tenants"
    ADD CONSTRAINT "xero_tenants_xero_connection_id_fkey" FOREIGN KEY ("xero_connection_id") REFERENCES "public"."xero_connections"("id") ON DELETE CASCADE;



CREATE POLICY "Admins can delete bills_synced" ON "public"."bills_synced" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete emails_sent" ON "public"."emails_sent" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete invoices_extracted" ON "public"."invoices_extracted" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete invoices_raw" ON "public"."invoices_raw" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete payments" ON "public"."payments" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete reconciliation_lines" ON "public"."reconciliation_lines" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete reconciliations" ON "public"."reconciliations" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete remittances" ON "public"."remittances" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete statement_lines" ON "public"."statement_lines" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete statements_raw" ON "public"."statements_raw" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete supplier_contacts" ON "public"."supplier_contacts" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete suppliers" ON "public"."suppliers" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete xero_connections" ON "public"."xero_connections" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can delete xero_tenants" ON "public"."xero_tenants" FOR DELETE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can manage roles" ON "public"."user_roles" TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role")) WITH CHECK ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can update their organisations" ON "public"."organisations" FOR UPDATE TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "id", 'admin'::"public"."app_role")) WITH CHECK ("public"."has_role"("auth"."uid"(), "id", 'admin'::"public"."app_role"));



CREATE POLICY "Admins can view roles in their organisations" ON "public"."user_roles" FOR SELECT TO "authenticated" USING ("public"."has_role"("auth"."uid"(), "organisation_id", 'admin'::"public"."app_role"));



CREATE POLICY "Allow insert per organisation" ON "public"."invoices_raw" FOR INSERT TO "authenticated" WITH CHECK (("organisation_id" = "auth"."uid"()));



CREATE POLICY "Members can view audit_log" ON "public"."audit_log" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view bills_synced" ON "public"."bills_synced" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view emails_sent" ON "public"."emails_sent" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view invoices_extracted" ON "public"."invoices_extracted" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view invoices_raw" ON "public"."invoices_raw" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view payments" ON "public"."payments" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view reconciliation_lines" ON "public"."reconciliation_lines" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view reconciliations" ON "public"."reconciliations" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view remittances" ON "public"."remittances" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view statement_lines" ON "public"."statement_lines" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view statements_raw" ON "public"."statements_raw" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view supplier_contacts" ON "public"."supplier_contacts" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view suppliers" ON "public"."suppliers" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view their organisations" ON "public"."organisations" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "id"));



CREATE POLICY "Members can view xero_connections" ON "public"."xero_connections" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Members can view xero_tenants" ON "public"."xero_tenants" FOR SELECT TO "authenticated" USING ("public"."is_member_of"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Users can update their own profile" ON "public"."profiles" FOR UPDATE TO "authenticated" USING (("id" = "auth"."uid"())) WITH CHECK (("id" = "auth"."uid"()));



CREATE POLICY "Users can view their own profile" ON "public"."profiles" FOR SELECT TO "authenticated" USING (("id" = "auth"."uid"()));



CREATE POLICY "Users can view their own roles" ON "public"."user_roles" FOR SELECT TO "authenticated" USING (("user_id" = "auth"."uid"()));



CREATE POLICY "Writers can insert audit_log" ON "public"."audit_log" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert bills_synced" ON "public"."bills_synced" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert emails_sent" ON "public"."emails_sent" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert invoices_extracted" ON "public"."invoices_extracted" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert invoices_raw" ON "public"."invoices_raw" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert payments" ON "public"."payments" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert reconciliation_lines" ON "public"."reconciliation_lines" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert reconciliations" ON "public"."reconciliations" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert remittances" ON "public"."remittances" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert statement_lines" ON "public"."statement_lines" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert statements_raw" ON "public"."statements_raw" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert supplier_contacts" ON "public"."supplier_contacts" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert suppliers" ON "public"."suppliers" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert xero_connections" ON "public"."xero_connections" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can insert xero_tenants" ON "public"."xero_tenants" FOR INSERT TO "authenticated" WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update bills_synced" ON "public"."bills_synced" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update emails_sent" ON "public"."emails_sent" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update invoices_extracted" ON "public"."invoices_extracted" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update invoices_raw" ON "public"."invoices_raw" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update payments" ON "public"."payments" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update reconciliation_lines" ON "public"."reconciliation_lines" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update reconciliations" ON "public"."reconciliations" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update remittances" ON "public"."remittances" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update statement_lines" ON "public"."statement_lines" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update statements_raw" ON "public"."statements_raw" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update supplier_contacts" ON "public"."supplier_contacts" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update suppliers" ON "public"."suppliers" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update xero_connections" ON "public"."xero_connections" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



CREATE POLICY "Writers can update xero_tenants" ON "public"."xero_tenants" FOR UPDATE TO "authenticated" USING ("public"."can_write_org"("auth"."uid"(), "organisation_id")) WITH CHECK ("public"."can_write_org"("auth"."uid"(), "organisation_id"));



ALTER TABLE "public"."account_budgets" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "account_budgets_select_member" ON "public"."account_budgets" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "account_budgets_write_accountants" ON "public"."account_budgets" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."account_mappings" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "account_mappings_select" ON "public"."account_mappings" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."accounts" "a"
  WHERE (("a"."id" = "account_mappings"."account_id") AND "public"."is_org_member"("a"."organisation_id")))));



CREATE POLICY "account_mappings_write" ON "public"."account_mappings" TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."accounts" "a"
  WHERE (("a"."id" = "account_mappings"."account_id") AND "public"."has_org_role"("a"."organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM "public"."accounts" "a"
  WHERE (("a"."id" = "account_mappings"."account_id") AND "public"."has_org_role"("a"."organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])))));



ALTER TABLE "public"."accounts" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "accounts_select_member" ON "public"."accounts" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "accounts_write" ON "public"."accounts" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."asset_types" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "asset_types_select_member" ON "public"."asset_types" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



ALTER TABLE "public"."audit_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."bank_accounts" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "bank_accounts_select_member" ON "public"."bank_accounts" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "bank_accounts_write_accountants" ON "public"."bank_accounts" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."bank_audit_events" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "bank_audit_events_insert_accountants" ON "public"."bank_audit_events" FOR INSERT TO "authenticated" WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "bank_audit_events_select_member" ON "public"."bank_audit_events" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "bank_lines_select_member" ON "public"."bank_statement_lines" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "bank_lines_write_accountants" ON "public"."bank_statement_lines" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."bank_parsing_rules" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "bank_parsing_rules_select_member" ON "public"."bank_parsing_rules" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "bank_parsing_rules_write_accountants" ON "public"."bank_parsing_rules" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "bank_rules_select_member" ON "public"."bank_transaction_rules" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "bank_rules_write_accountants" ON "public"."bank_transaction_rules" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."bank_statement_lines" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."bank_statement_uploads" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "bank_suggestions_select_member" ON "public"."bank_transaction_suggestions" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "bank_suggestions_write_accountants" ON "public"."bank_transaction_suggestions" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."bank_transaction_rules" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."bank_transaction_suggestions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "bank_uploads_select_member" ON "public"."bank_statement_uploads" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "bank_uploads_write_accountants" ON "public"."bank_statement_uploads" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."bills_synced" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."consolidation_account_mappings" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "consolidation_account_mappings_select" ON "public"."consolidation_account_mappings" FOR SELECT TO "authenticated" USING ("public"."can_read_reporting_group"("reporting_group_id"));



CREATE POLICY "consolidation_account_mappings_write" ON "public"."consolidation_account_mappings" TO "authenticated" USING ("public"."can_write_reporting_group"("reporting_group_id")) WITH CHECK ("public"."can_write_reporting_group"("reporting_group_id"));



ALTER TABLE "public"."consolidation_adjustment_lines" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "consolidation_adjustment_lines_select" ON "public"."consolidation_adjustment_lines" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."consolidation_adjustments" "a"
  WHERE (("a"."id" = "consolidation_adjustment_lines"."adjustment_id") AND "public"."can_read_reporting_group"("a"."reporting_group_id")))));



CREATE POLICY "consolidation_adjustment_lines_write" ON "public"."consolidation_adjustment_lines" TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."consolidation_adjustments" "a"
  WHERE (("a"."id" = "consolidation_adjustment_lines"."adjustment_id") AND "public"."can_write_reporting_group"("a"."reporting_group_id"))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM "public"."consolidation_adjustments" "a"
  WHERE (("a"."id" = "consolidation_adjustment_lines"."adjustment_id") AND "public"."can_write_reporting_group"("a"."reporting_group_id")))));



ALTER TABLE "public"."consolidation_adjustments" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "consolidation_adjustments_select" ON "public"."consolidation_adjustments" FOR SELECT TO "authenticated" USING ("public"."can_read_reporting_group"("reporting_group_id"));



CREATE POLICY "consolidation_adjustments_write" ON "public"."consolidation_adjustments" TO "authenticated" USING ("public"."can_write_reporting_group"("reporting_group_id")) WITH CHECK ("public"."can_write_reporting_group"("reporting_group_id"));



ALTER TABLE "public"."consolidation_entity_balances" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "consolidation_entity_balances_select" ON "public"."consolidation_entity_balances" FOR SELECT TO "authenticated" USING ("public"."can_read_reporting_group"("reporting_group_id"));



CREATE POLICY "consolidation_entity_balances_write" ON "public"."consolidation_entity_balances" TO "authenticated" USING ("public"."can_write_reporting_group"("reporting_group_id")) WITH CHECK ("public"."can_write_reporting_group"("reporting_group_id"));



ALTER TABLE "public"."consolidation_periods" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "consolidation_periods_select" ON "public"."consolidation_periods" FOR SELECT TO "authenticated" USING ("public"."can_read_reporting_group"("reporting_group_id"));



CREATE POLICY "consolidation_periods_write" ON "public"."consolidation_periods" TO "authenticated" USING ("public"."can_write_reporting_group"("reporting_group_id")) WITH CHECK ("public"."can_write_reporting_group"("reporting_group_id"));



ALTER TABLE "public"."document_pages" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "document_pages_insert_member" ON "public"."document_pages" FOR INSERT TO "authenticated" WITH CHECK ("public"."is_org_member"("organisation_id"));



CREATE POLICY "document_pages_select_member" ON "public"."document_pages" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "document_pages_update_member" ON "public"."document_pages" FOR UPDATE TO "authenticated" USING ("public"."is_org_member"("organisation_id")) WITH CHECK ("public"."is_org_member"("organisation_id"));



ALTER TABLE "public"."document_processing_jobs" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "document_processing_jobs_insert_member" ON "public"."document_processing_jobs" FOR INSERT TO "authenticated" WITH CHECK ("public"."is_org_member"("organisation_id"));



CREATE POLICY "document_processing_jobs_select_member" ON "public"."document_processing_jobs" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "document_processing_jobs_update_member" ON "public"."document_processing_jobs" FOR UPDATE TO "authenticated" USING ("public"."is_org_member"("organisation_id")) WITH CHECK ("public"."is_org_member"("organisation_id"));



ALTER TABLE "public"."document_upload_batches" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "document_upload_batches_insert_member" ON "public"."document_upload_batches" FOR INSERT TO "authenticated" WITH CHECK ("public"."is_org_member"("organisation_id"));



CREATE POLICY "document_upload_batches_select_member" ON "public"."document_upload_batches" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "document_upload_batches_update_member" ON "public"."document_upload_batches" FOR UPDATE TO "authenticated" USING ("public"."is_org_member"("organisation_id")) WITH CHECK ("public"."is_org_member"("organisation_id"));



ALTER TABLE "public"."emails_sent" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."exchange_rates" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "exchange_rates_select" ON "public"."exchange_rates" FOR SELECT TO "authenticated" USING ((("reporting_group_id" IS NOT NULL) AND "public"."can_read_reporting_group"("reporting_group_id")));



CREATE POLICY "exchange_rates_write" ON "public"."exchange_rates" TO "authenticated" USING ((("reporting_group_id" IS NOT NULL) AND "public"."can_write_reporting_group"("reporting_group_id"))) WITH CHECK ((("reporting_group_id" IS NOT NULL) AND "public"."can_write_reporting_group"("reporting_group_id")));



ALTER TABLE "public"."gl_journal_lines" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "gl_journal_lines_select_member" ON "public"."gl_journal_lines" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "gl_journal_lines_write_accountants" ON "public"."gl_journal_lines" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."gl_journals" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "gl_journals_select_member" ON "public"."gl_journals" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "gl_journals_write_accountants" ON "public"."gl_journals" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."invoice_agent_suggestions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "invoice_agent_suggestions_insert_accountants" ON "public"."invoice_agent_suggestions" FOR INSERT TO "authenticated" WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "invoice_agent_suggestions_select_member" ON "public"."invoice_agent_suggestions" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "invoice_agent_suggestions_update_accountants" ON "public"."invoice_agent_suggestions" FOR UPDATE TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."invoice_audit_events" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "invoice_audit_events_insert_member" ON "public"."invoice_audit_events" FOR INSERT TO "authenticated" WITH CHECK ("public"."is_org_member"("organisation_id"));



CREATE POLICY "invoice_audit_events_select_member" ON "public"."invoice_audit_events" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "invoice_audit_events_select_org_member" ON "public"."invoice_audit_events" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



ALTER TABLE "public"."invoice_audit_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."invoice_extraction_feedback" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."invoice_line_item_allocations" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "invoice_line_item_allocations_select_member" ON "public"."invoice_line_item_allocations" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "invoice_line_item_allocations_write_accountants" ON "public"."invoice_line_item_allocations" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."invoice_line_items" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."invoice_page_groups" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."invoice_parse_attempts" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."invoice_supplier_comparison_ignores" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "invoice_supplier_comparison_ignores_delete_accountants" ON "public"."invoice_supplier_comparison_ignores" FOR DELETE TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "invoice_supplier_comparison_ignores_insert_accountants" ON "public"."invoice_supplier_comparison_ignores" FOR INSERT TO "authenticated" WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "invoice_supplier_comparison_ignores_select_member" ON "public"."invoice_supplier_comparison_ignores" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



ALTER TABLE "public"."invoices_extracted" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."invoices_raw" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "invoices_raw_delete_org_admin" ON "public"."invoices_raw" FOR DELETE TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]));



CREATE POLICY "invoices_raw_insert_org_member" ON "public"."invoices_raw" FOR INSERT TO "authenticated" WITH CHECK ("public"."is_org_member"("organisation_id"));



CREATE POLICY "invoices_raw_select_org_member" ON "public"."invoices_raw" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "invoices_raw_update_org_member" ON "public"."invoices_raw" FOR UPDATE TO "authenticated" USING ("public"."is_org_member"("organisation_id")) WITH CHECK ("public"."is_org_member"("organisation_id"));



CREATE POLICY "kyc_doc_delete" ON "public"."supplier_kyc_documents" FOR DELETE TO "authenticated" USING (("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]) OR ("uploaded_by" = "auth"."uid"())));



CREATE POLICY "kyc_doc_insert" ON "public"."supplier_kyc_documents" FOR INSERT TO "authenticated" WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "kyc_doc_select" ON "public"."supplier_kyc_documents" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "kyc_req_delete" ON "public"."supplier_kyc_requests" FOR DELETE TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]));



CREATE POLICY "kyc_req_insert" ON "public"."supplier_kyc_requests" FOR INSERT TO "authenticated" WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "kyc_req_select" ON "public"."supplier_kyc_requests" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "kyc_req_update" ON "public"."supplier_kyc_requests" FOR UPDATE TO "authenticated" USING (("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]) OR (("requested_by" = "auth"."uid"()) AND ("status" = 'draft'::"text"))));



ALTER TABLE "public"."org_integrations" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "org_integrations_select_member" ON "public"."org_integrations" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "org_integrations_write" ON "public"."org_integrations" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "org_users_delete_admin" ON "public"."organisation_users" FOR DELETE TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]));



CREATE POLICY "org_users_insert_self_or_admin" ON "public"."organisation_users" FOR INSERT TO "authenticated" WITH CHECK (("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]) OR (("user_id" = "auth"."uid"()) AND ("role" = 'owner'::"public"."organisation_role") AND ("status" = 'active'::"public"."membership_status") AND (NOT (EXISTS ( SELECT 1
   FROM "public"."organisation_users" "ou"
  WHERE (("ou"."organisation_id" = "organisation_users"."organisation_id") AND ("ou"."status" = 'active'::"public"."membership_status"))))))));



CREATE POLICY "org_users_select_member" ON "public"."organisation_users" FOR SELECT TO "authenticated" USING ((("user_id" = "auth"."uid"()) OR "public"."is_org_member"("organisation_id")));



CREATE POLICY "org_users_update_admin" ON "public"."organisation_users" FOR UPDATE TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]));



ALTER TABLE "public"."organisation_invoice_branding" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "organisation_invoice_branding_select_member" ON "public"."organisation_invoice_branding" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "organisation_invoice_branding_write_admin" ON "public"."organisation_invoice_branding" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]));



ALTER TABLE "public"."organisation_module_settings" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "organisation_module_settings_select_member" ON "public"."organisation_module_settings" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "organisation_module_settings_write_admin" ON "public"."organisation_module_settings" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]));



ALTER TABLE "public"."organisation_users" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."organisations" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "organisations_insert_authenticated" ON "public"."organisations" FOR INSERT TO "authenticated" WITH CHECK (("auth"."uid"() IS NOT NULL));



CREATE POLICY "organisations_select_member" ON "public"."organisations" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("id"));



CREATE POLICY "organisations_update_admins" ON "public"."organisations" FOR UPDATE TO "authenticated" USING ("public"."has_org_role"("id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"]));



ALTER TABLE "public"."payments" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."profiles" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."reconciliation_lines" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."reconciliation_results" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "reconciliation_results_delete_own_org" ON "public"."reconciliation_results" FOR DELETE TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "reconciliation_results_insert_own_org" ON "public"."reconciliation_results" FOR INSERT TO "authenticated" WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "reconciliation_results_select_own_org" ON "public"."reconciliation_results" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "reconciliation_results_update_own_org" ON "public"."reconciliation_results" FOR UPDATE TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."reconciliations" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."remittances" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."reporting_group_entities" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "reporting_group_entities_select" ON "public"."reporting_group_entities" FOR SELECT TO "authenticated" USING ("public"."can_read_reporting_group"("reporting_group_id"));



CREATE POLICY "reporting_group_entities_write" ON "public"."reporting_group_entities" TO "authenticated" USING ("public"."can_write_reporting_group"("reporting_group_id")) WITH CHECK ("public"."can_write_reporting_group"("reporting_group_id"));



ALTER TABLE "public"."reporting_group_users" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "reporting_group_users_select" ON "public"."reporting_group_users" FOR SELECT TO "authenticated" USING ("public"."can_read_reporting_group"("reporting_group_id"));



CREATE POLICY "reporting_group_users_write" ON "public"."reporting_group_users" TO "authenticated" USING ("public"."can_write_reporting_group"("reporting_group_id")) WITH CHECK ("public"."can_write_reporting_group"("reporting_group_id"));



ALTER TABLE "public"."reporting_groups" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "reporting_groups_insert" ON "public"."reporting_groups" FOR INSERT TO "authenticated" WITH CHECK ("public"."has_org_role"("owner_organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "reporting_groups_select" ON "public"."reporting_groups" FOR SELECT TO "authenticated" USING ("public"."can_read_reporting_group"("id"));



CREATE POLICY "reporting_groups_update" ON "public"."reporting_groups" FOR UPDATE TO "authenticated" USING ("public"."can_write_reporting_group"("id")) WITH CHECK ("public"."can_write_reporting_group"("id"));



ALTER TABLE "public"."statement_lines" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."statements_raw" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "supplier_allocation_rule_splits_select_member" ON "public"."supplier_line_item_allocation_rule_splits" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "supplier_allocation_rule_splits_write_accountants" ON "public"."supplier_line_item_allocation_rule_splits" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



CREATE POLICY "supplier_allocation_rules_select_member" ON "public"."supplier_line_item_allocation_rules" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "supplier_allocation_rules_write_accountants" ON "public"."supplier_line_item_allocation_rules" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."supplier_branches" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "supplier_branches_select_member" ON "public"."supplier_branches" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "supplier_branches_write_accountants" ON "public"."supplier_branches" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."supplier_contacts" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."supplier_extraction_profiles" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."supplier_kyc_documents" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."supplier_kyc_requests" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."supplier_line_item_allocation_rule_splits" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."supplier_line_item_allocation_rules" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."suppliers" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "suppliers_delete_admin" ON "public"."suppliers" FOR DELETE TO "authenticated" USING ((("organisation_id" IS NULL) OR "public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role"])));



CREATE POLICY "suppliers_select_member" ON "public"."suppliers" FOR SELECT TO "authenticated" USING ((("organisation_id" IS NOT NULL) AND "public"."is_org_member"("organisation_id")));



ALTER TABLE "public"."themes" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "themes_select_purchased_active" ON "public"."themes" FOR SELECT TO "authenticated" USING ((("is_active" = true) AND (EXISTS ( SELECT 1
   FROM "public"."user_theme_entitlements" "ute"
  WHERE (("ute"."theme_id" = "themes"."id") AND ("ute"."user_id" = "auth"."uid"()))))));



ALTER TABLE "public"."tracking_dimensions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "tracking_dimensions_select" ON "public"."tracking_dimensions" FOR SELECT TO "authenticated" USING ("public"."is_org_member"("organisation_id"));



CREATE POLICY "tracking_dimensions_write" ON "public"."tracking_dimensions" TO "authenticated" USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])) WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));



ALTER TABLE "public"."tracking_values" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "tracking_values_select" ON "public"."tracking_values" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."tracking_dimensions" "td"
  WHERE (("td"."id" = "tracking_values"."dimension_id") AND "public"."is_org_member"("td"."organisation_id")))));



CREATE POLICY "tracking_values_write" ON "public"."tracking_values" TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."tracking_dimensions" "td"
  WHERE (("td"."id" = "tracking_values"."dimension_id") AND "public"."has_org_role"("td"."organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM "public"."tracking_dimensions" "td"
  WHERE (("td"."id" = "tracking_values"."dimension_id") AND "public"."has_org_role"("td"."organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"])))));



ALTER TABLE "public"."user_roles" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."user_theme_entitlements" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "user_theme_entitlements_select_own" ON "public"."user_theme_entitlements" FOR SELECT TO "authenticated" USING (("user_id" = "auth"."uid"()));



ALTER TABLE "public"."user_theme_preferences" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "user_theme_preferences_insert_own_entitled" ON "public"."user_theme_preferences" FOR INSERT TO "authenticated" WITH CHECK ((("user_id" = "auth"."uid"()) AND (("active_theme_id" IS NULL) OR (EXISTS ( SELECT 1
   FROM ("public"."user_theme_entitlements" "ute"
     JOIN "public"."themes" "t" ON (("t"."id" = "ute"."theme_id")))
  WHERE (("ute"."user_id" = "auth"."uid"()) AND ("ute"."theme_id" = "user_theme_preferences"."active_theme_id") AND ("t"."is_active" = true)))))));



CREATE POLICY "user_theme_preferences_select_own" ON "public"."user_theme_preferences" FOR SELECT TO "authenticated" USING (("user_id" = "auth"."uid"()));



CREATE POLICY "user_theme_preferences_update_own_entitled" ON "public"."user_theme_preferences" FOR UPDATE TO "authenticated" USING (("user_id" = "auth"."uid"())) WITH CHECK ((("user_id" = "auth"."uid"()) AND (("active_theme_id" IS NULL) OR (EXISTS ( SELECT 1
   FROM ("public"."user_theme_entitlements" "ute"
     JOIN "public"."themes" "t" ON (("t"."id" = "ute"."theme_id")))
  WHERE (("ute"."user_id" = "auth"."uid"()) AND ("ute"."theme_id" = "user_theme_preferences"."active_theme_id") AND ("t"."is_active" = true)))))));



ALTER TABLE "public"."whatsapp_pending_selections" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "whatsapp_pending_selections: deny anon" ON "public"."whatsapp_pending_selections" AS RESTRICTIVE TO "anon" USING (false) WITH CHECK (false);



CREATE POLICY "whatsapp_pending_selections: deny authenticated" ON "public"."whatsapp_pending_selections" AS RESTRICTIVE TO "authenticated" USING (false) WITH CHECK (false);



ALTER TABLE "public"."xero_connections" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."xero_tenants" ENABLE ROW LEVEL SECURITY;


GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";



REVOKE ALL ON FUNCTION "public"."assert_asset_type_admin"("p_org_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."assert_asset_type_admin"("p_org_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."assert_asset_type_admin"("p_org_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."assert_asset_type_admin"("p_org_id" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."assert_bank_write"("p_org_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."assert_bank_write"("p_org_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."assert_bank_write"("p_org_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."assert_bank_write"("p_org_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."asset_type_account_names"("p_name" "text", "p_category" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."asset_type_account_names"("p_name" "text", "p_category" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."asset_type_account_names"("p_name" "text", "p_category" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."asset_type_usage"("p_asset_type_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."asset_type_usage"("p_asset_type_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."asset_type_usage"("p_asset_type_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."asset_type_usage"("p_asset_type_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."assign_org_inbound_email"() TO "anon";
GRANT ALL ON FUNCTION "public"."assign_org_inbound_email"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."assign_org_inbound_email"() TO "service_role";



GRANT ALL ON FUNCTION "public"."can_read_reporting_group"("_group_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."can_read_reporting_group"("_group_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."can_read_reporting_group"("_group_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."can_write_org"("_user_id" "uuid", "_org_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."can_write_org"("_user_id" "uuid", "_org_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."can_write_org"("_user_id" "uuid", "_org_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."can_write_reporting_group"("_group_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."can_write_reporting_group"("_group_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."can_write_reporting_group"("_group_id" "uuid") TO "service_role";



GRANT ALL ON TABLE "public"."asset_types" TO "anon";
GRANT ALL ON TABLE "public"."asset_types" TO "authenticated";
GRANT ALL ON TABLE "public"."asset_types" TO "service_role";



REVOKE ALL ON FUNCTION "public"."create_asset_type_with_accounts"("p_org_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."create_asset_type_with_accounts"("p_org_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) TO "anon";
GRANT ALL ON FUNCTION "public"."create_asset_type_with_accounts"("p_org_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) TO "authenticated";
GRANT ALL ON FUNCTION "public"."create_asset_type_with_accounts"("p_org_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) TO "service_role";



GRANT ALL ON FUNCTION "public"."create_org_system_accounts"("p_org_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."create_org_system_accounts"("p_org_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."create_org_system_accounts"("p_org_id" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."delete_bank_statement_lines_atomic"("p_org_id" "uuid", "p_line_ids" "uuid"[], "p_actor_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."delete_bank_statement_lines_atomic"("p_org_id" "uuid", "p_line_ids" "uuid"[], "p_actor_user_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."delete_bank_statement_lines_atomic"("p_org_id" "uuid", "p_line_ids" "uuid"[], "p_actor_user_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."delete_bank_statement_lines_atomic"("p_org_id" "uuid", "p_line_ids" "uuid"[], "p_actor_user_id" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."delete_bank_statement_uploads_atomic"("p_org_id" "uuid", "p_upload_ids" "uuid"[], "p_actor_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."delete_bank_statement_uploads_atomic"("p_org_id" "uuid", "p_upload_ids" "uuid"[], "p_actor_user_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."delete_bank_statement_uploads_atomic"("p_org_id" "uuid", "p_upload_ids" "uuid"[], "p_actor_user_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."delete_bank_statement_uploads_atomic"("p_org_id" "uuid", "p_upload_ids" "uuid"[], "p_actor_user_id" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."get_bank_account_balance_summary"("p_org_id" "uuid", "p_bank_account_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."get_bank_account_balance_summary"("p_org_id" "uuid", "p_bank_account_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."get_bank_account_balance_summary"("p_org_id" "uuid", "p_bank_account_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_bank_account_balance_summary"("p_org_id" "uuid", "p_bank_account_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."has_org_role"("_org_id" "uuid", "_roles" "public"."organisation_role"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."has_org_role"("_org_id" "uuid", "_roles" "public"."organisation_role"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."has_org_role"("_org_id" "uuid", "_roles" "public"."organisation_role"[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."has_role"("_user_id" "uuid", "_org_id" "uuid", "_role" "public"."app_role") TO "anon";
GRANT ALL ON FUNCTION "public"."has_role"("_user_id" "uuid", "_org_id" "uuid", "_role" "public"."app_role") TO "authenticated";
GRANT ALL ON FUNCTION "public"."has_role"("_user_id" "uuid", "_org_id" "uuid", "_role" "public"."app_role") TO "service_role";



GRANT ALL ON FUNCTION "public"."is_member_of"("_user_id" "uuid", "_org_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."is_member_of"("_user_id" "uuid", "_org_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."is_member_of"("_user_id" "uuid", "_org_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."is_org_member"("_org_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."is_org_member"("_org_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."is_org_member"("_org_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."is_valid_auto_link_amount_tiers"("value" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."is_valid_auto_link_amount_tiers"("value" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."is_valid_auto_link_amount_tiers"("value" "jsonb") TO "service_role";



GRANT ALL ON FUNCTION "public"."on_organisation_created"() TO "anon";
GRANT ALL ON FUNCTION "public"."on_organisation_created"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."on_organisation_created"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."post_invoice_to_gl_atomic"("p_org_id" "uuid", "p_invoice_id" "uuid", "p_user_id" "uuid", "p_journal_date" "date", "p_description" "text", "p_total" numeric, "p_lines" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."post_invoice_to_gl_atomic"("p_org_id" "uuid", "p_invoice_id" "uuid", "p_user_id" "uuid", "p_journal_date" "date", "p_description" "text", "p_total" numeric, "p_lines" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."post_invoice_to_gl_atomic"("p_org_id" "uuid", "p_invoice_id" "uuid", "p_user_id" "uuid", "p_journal_date" "date", "p_description" "text", "p_total" numeric, "p_lines" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."post_invoice_to_gl_atomic"("p_org_id" "uuid", "p_invoice_id" "uuid", "p_user_id" "uuid", "p_journal_date" "date", "p_description" "text", "p_total" numeric, "p_lines" "jsonb") TO "service_role";



GRANT ALL ON FUNCTION "public"."prevent_duplicate_org_name_for_user"() TO "anon";
GRANT ALL ON FUNCTION "public"."prevent_duplicate_org_name_for_user"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."prevent_duplicate_org_name_for_user"() TO "service_role";



GRANT ALL ON FUNCTION "public"."prevent_system_account_delete"() TO "anon";
GRANT ALL ON FUNCTION "public"."prevent_system_account_delete"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."prevent_system_account_delete"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."preview_asset_type_removal"("p_org_id" "uuid", "p_asset_type_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."preview_asset_type_removal"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."preview_asset_type_removal"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."preview_asset_type_removal"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."protect_last_owner"() TO "anon";
GRANT ALL ON FUNCTION "public"."protect_last_owner"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."protect_last_owner"() TO "service_role";



GRANT ALL ON FUNCTION "public"."protect_system_accounts"() TO "anon";
GRANT ALL ON FUNCTION "public"."protect_system_accounts"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."protect_system_accounts"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."refresh_bank_account_statement_state"("p_org_id" "uuid", "p_bank_account_ids" "uuid"[]) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."refresh_bank_account_statement_state"("p_org_id" "uuid", "p_bank_account_ids" "uuid"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."refresh_bank_account_statement_state"("p_org_id" "uuid", "p_bank_account_ids" "uuid"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."refresh_bank_account_statement_state"("p_org_id" "uuid", "p_bank_account_ids" "uuid"[]) TO "service_role";



REVOKE ALL ON FUNCTION "public"."remove_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."remove_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."remove_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."remove_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."restore_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."restore_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."restore_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."restore_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."set_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."set_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."set_updated_at"() TO "service_role";



GRANT ALL ON FUNCTION "public"."storage_object_org_id"("_name" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."storage_object_org_id"("_name" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."storage_object_org_id"("_name" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."update_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."update_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) TO "anon";
GRANT ALL ON FUNCTION "public"."update_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) TO "authenticated";
GRANT ALL ON FUNCTION "public"."update_asset_type_with_accounts"("p_org_id" "uuid", "p_asset_type_id" "uuid", "p_name" "text", "p_category" "text", "p_useful_life_months" integer, "p_residual_value_percent" numeric) TO "service_role";



GRANT ALL ON FUNCTION "public"."update_updated_at_column"() TO "anon";
GRANT ALL ON FUNCTION "public"."update_updated_at_column"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."update_updated_at_column"() TO "service_role";



GRANT ALL ON TABLE "public"."account_budgets" TO "anon";
GRANT ALL ON TABLE "public"."account_budgets" TO "authenticated";
GRANT ALL ON TABLE "public"."account_budgets" TO "service_role";



GRANT ALL ON TABLE "public"."account_mappings" TO "anon";
GRANT ALL ON TABLE "public"."account_mappings" TO "authenticated";
GRANT ALL ON TABLE "public"."account_mappings" TO "service_role";



GRANT ALL ON TABLE "public"."accounts" TO "anon";
GRANT ALL ON TABLE "public"."accounts" TO "authenticated";
GRANT ALL ON TABLE "public"."accounts" TO "service_role";



GRANT ALL ON TABLE "public"."audit_log" TO "anon";
GRANT ALL ON TABLE "public"."audit_log" TO "authenticated";
GRANT ALL ON TABLE "public"."audit_log" TO "service_role";



GRANT ALL ON TABLE "public"."bank_accounts" TO "anon";
GRANT ALL ON TABLE "public"."bank_accounts" TO "authenticated";
GRANT ALL ON TABLE "public"."bank_accounts" TO "service_role";



GRANT ALL ON TABLE "public"."bank_audit_events" TO "anon";
GRANT ALL ON TABLE "public"."bank_audit_events" TO "authenticated";
GRANT ALL ON TABLE "public"."bank_audit_events" TO "service_role";



GRANT ALL ON TABLE "public"."bank_parsing_rules" TO "anon";
GRANT ALL ON TABLE "public"."bank_parsing_rules" TO "authenticated";
GRANT ALL ON TABLE "public"."bank_parsing_rules" TO "service_role";



GRANT ALL ON TABLE "public"."bank_statement_lines" TO "anon";
GRANT ALL ON TABLE "public"."bank_statement_lines" TO "authenticated";
GRANT ALL ON TABLE "public"."bank_statement_lines" TO "service_role";



GRANT ALL ON TABLE "public"."bank_statement_uploads" TO "anon";
GRANT ALL ON TABLE "public"."bank_statement_uploads" TO "authenticated";
GRANT ALL ON TABLE "public"."bank_statement_uploads" TO "service_role";



GRANT ALL ON TABLE "public"."bank_transaction_rules" TO "anon";
GRANT ALL ON TABLE "public"."bank_transaction_rules" TO "authenticated";
GRANT ALL ON TABLE "public"."bank_transaction_rules" TO "service_role";



GRANT ALL ON TABLE "public"."bank_transaction_suggestions" TO "anon";
GRANT ALL ON TABLE "public"."bank_transaction_suggestions" TO "authenticated";
GRANT ALL ON TABLE "public"."bank_transaction_suggestions" TO "service_role";



GRANT ALL ON TABLE "public"."bills_synced" TO "anon";
GRANT ALL ON TABLE "public"."bills_synced" TO "authenticated";
GRANT ALL ON TABLE "public"."bills_synced" TO "service_role";



GRANT ALL ON TABLE "public"."consolidation_account_mappings" TO "anon";
GRANT ALL ON TABLE "public"."consolidation_account_mappings" TO "authenticated";
GRANT ALL ON TABLE "public"."consolidation_account_mappings" TO "service_role";



GRANT ALL ON TABLE "public"."consolidation_adjustment_lines" TO "anon";
GRANT ALL ON TABLE "public"."consolidation_adjustment_lines" TO "authenticated";
GRANT ALL ON TABLE "public"."consolidation_adjustment_lines" TO "service_role";



GRANT ALL ON TABLE "public"."consolidation_adjustments" TO "anon";
GRANT ALL ON TABLE "public"."consolidation_adjustments" TO "authenticated";
GRANT ALL ON TABLE "public"."consolidation_adjustments" TO "service_role";



GRANT ALL ON TABLE "public"."consolidation_entity_balances" TO "anon";
GRANT ALL ON TABLE "public"."consolidation_entity_balances" TO "authenticated";
GRANT ALL ON TABLE "public"."consolidation_entity_balances" TO "service_role";



GRANT ALL ON TABLE "public"."consolidation_periods" TO "anon";
GRANT ALL ON TABLE "public"."consolidation_periods" TO "authenticated";
GRANT ALL ON TABLE "public"."consolidation_periods" TO "service_role";



GRANT ALL ON TABLE "public"."document_pages" TO "anon";
GRANT ALL ON TABLE "public"."document_pages" TO "authenticated";
GRANT ALL ON TABLE "public"."document_pages" TO "service_role";



GRANT ALL ON TABLE "public"."document_processing_jobs" TO "anon";
GRANT ALL ON TABLE "public"."document_processing_jobs" TO "authenticated";
GRANT ALL ON TABLE "public"."document_processing_jobs" TO "service_role";



GRANT ALL ON TABLE "public"."document_upload_batches" TO "anon";
GRANT ALL ON TABLE "public"."document_upload_batches" TO "authenticated";
GRANT ALL ON TABLE "public"."document_upload_batches" TO "service_role";



GRANT ALL ON TABLE "public"."emails_sent" TO "anon";
GRANT ALL ON TABLE "public"."emails_sent" TO "authenticated";
GRANT ALL ON TABLE "public"."emails_sent" TO "service_role";



GRANT ALL ON TABLE "public"."exchange_rates" TO "anon";
GRANT ALL ON TABLE "public"."exchange_rates" TO "authenticated";
GRANT ALL ON TABLE "public"."exchange_rates" TO "service_role";



GRANT ALL ON TABLE "public"."gl_journal_lines" TO "anon";
GRANT ALL ON TABLE "public"."gl_journal_lines" TO "authenticated";
GRANT ALL ON TABLE "public"."gl_journal_lines" TO "service_role";



GRANT ALL ON TABLE "public"."gl_journals" TO "anon";
GRANT ALL ON TABLE "public"."gl_journals" TO "authenticated";
GRANT ALL ON TABLE "public"."gl_journals" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_agent_suggestions" TO "anon";
GRANT ALL ON TABLE "public"."invoice_agent_suggestions" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_agent_suggestions" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_audit_events" TO "anon";
GRANT ALL ON TABLE "public"."invoice_audit_events" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_audit_events" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_audit_log" TO "anon";
GRANT ALL ON TABLE "public"."invoice_audit_log" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_audit_log" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_extraction_feedback" TO "anon";
GRANT ALL ON TABLE "public"."invoice_extraction_feedback" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_extraction_feedback" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_line_item_allocations" TO "anon";
GRANT ALL ON TABLE "public"."invoice_line_item_allocations" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_line_item_allocations" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_line_items" TO "anon";
GRANT ALL ON TABLE "public"."invoice_line_items" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_line_items" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_page_groups" TO "anon";
GRANT ALL ON TABLE "public"."invoice_page_groups" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_page_groups" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_parse_attempts" TO "anon";
GRANT ALL ON TABLE "public"."invoice_parse_attempts" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_parse_attempts" TO "service_role";



GRANT ALL ON TABLE "public"."invoice_supplier_comparison_ignores" TO "anon";
GRANT ALL ON TABLE "public"."invoice_supplier_comparison_ignores" TO "authenticated";
GRANT ALL ON TABLE "public"."invoice_supplier_comparison_ignores" TO "service_role";



GRANT ALL ON TABLE "public"."invoices_extracted" TO "anon";
GRANT ALL ON TABLE "public"."invoices_extracted" TO "authenticated";
GRANT ALL ON TABLE "public"."invoices_extracted" TO "service_role";



GRANT ALL ON TABLE "public"."invoices_raw" TO "anon";
GRANT ALL ON TABLE "public"."invoices_raw" TO "authenticated";
GRANT ALL ON TABLE "public"."invoices_raw" TO "service_role";



GRANT ALL ON TABLE "public"."org_integrations" TO "anon";
GRANT ALL ON TABLE "public"."org_integrations" TO "authenticated";
GRANT ALL ON TABLE "public"."org_integrations" TO "service_role";



GRANT ALL ON TABLE "public"."organisation_invoice_branding" TO "anon";
GRANT ALL ON TABLE "public"."organisation_invoice_branding" TO "authenticated";
GRANT ALL ON TABLE "public"."organisation_invoice_branding" TO "service_role";



GRANT ALL ON TABLE "public"."organisation_module_settings" TO "anon";
GRANT ALL ON TABLE "public"."organisation_module_settings" TO "authenticated";
GRANT ALL ON TABLE "public"."organisation_module_settings" TO "service_role";



GRANT ALL ON TABLE "public"."organisation_users" TO "anon";
GRANT ALL ON TABLE "public"."organisation_users" TO "authenticated";
GRANT ALL ON TABLE "public"."organisation_users" TO "service_role";



GRANT ALL ON TABLE "public"."organisations" TO "anon";
GRANT ALL ON TABLE "public"."organisations" TO "authenticated";
GRANT ALL ON TABLE "public"."organisations" TO "service_role";



GRANT ALL ON TABLE "public"."payments" TO "anon";
GRANT ALL ON TABLE "public"."payments" TO "authenticated";
GRANT ALL ON TABLE "public"."payments" TO "service_role";



GRANT ALL ON TABLE "public"."profiles" TO "anon";
GRANT ALL ON TABLE "public"."profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."profiles" TO "service_role";



GRANT ALL ON TABLE "public"."reconciliation_lines" TO "anon";
GRANT ALL ON TABLE "public"."reconciliation_lines" TO "authenticated";
GRANT ALL ON TABLE "public"."reconciliation_lines" TO "service_role";



GRANT ALL ON TABLE "public"."reconciliation_results" TO "anon";
GRANT ALL ON TABLE "public"."reconciliation_results" TO "authenticated";
GRANT ALL ON TABLE "public"."reconciliation_results" TO "service_role";



GRANT ALL ON TABLE "public"."reconciliations" TO "anon";
GRANT ALL ON TABLE "public"."reconciliations" TO "authenticated";
GRANT ALL ON TABLE "public"."reconciliations" TO "service_role";



GRANT ALL ON TABLE "public"."remittances" TO "anon";
GRANT ALL ON TABLE "public"."remittances" TO "authenticated";
GRANT ALL ON TABLE "public"."remittances" TO "service_role";



GRANT ALL ON TABLE "public"."reporting_group_entities" TO "anon";
GRANT ALL ON TABLE "public"."reporting_group_entities" TO "authenticated";
GRANT ALL ON TABLE "public"."reporting_group_entities" TO "service_role";



GRANT ALL ON TABLE "public"."reporting_group_users" TO "anon";
GRANT ALL ON TABLE "public"."reporting_group_users" TO "authenticated";
GRANT ALL ON TABLE "public"."reporting_group_users" TO "service_role";



GRANT ALL ON TABLE "public"."reporting_groups" TO "anon";
GRANT ALL ON TABLE "public"."reporting_groups" TO "authenticated";
GRANT ALL ON TABLE "public"."reporting_groups" TO "service_role";



GRANT ALL ON TABLE "public"."statement_lines" TO "anon";
GRANT ALL ON TABLE "public"."statement_lines" TO "authenticated";
GRANT ALL ON TABLE "public"."statement_lines" TO "service_role";



GRANT ALL ON TABLE "public"."statements_raw" TO "anon";
GRANT ALL ON TABLE "public"."statements_raw" TO "authenticated";
GRANT ALL ON TABLE "public"."statements_raw" TO "service_role";



GRANT ALL ON TABLE "public"."supplier_branches" TO "anon";
GRANT ALL ON TABLE "public"."supplier_branches" TO "authenticated";
GRANT ALL ON TABLE "public"."supplier_branches" TO "service_role";



GRANT ALL ON TABLE "public"."supplier_contacts" TO "anon";
GRANT ALL ON TABLE "public"."supplier_contacts" TO "authenticated";
GRANT ALL ON TABLE "public"."supplier_contacts" TO "service_role";



GRANT ALL ON TABLE "public"."supplier_extraction_profiles" TO "anon";
GRANT ALL ON TABLE "public"."supplier_extraction_profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."supplier_extraction_profiles" TO "service_role";



GRANT ALL ON TABLE "public"."supplier_kyc_documents" TO "anon";
GRANT ALL ON TABLE "public"."supplier_kyc_documents" TO "authenticated";
GRANT ALL ON TABLE "public"."supplier_kyc_documents" TO "service_role";



GRANT ALL ON TABLE "public"."supplier_kyc_requests" TO "anon";
GRANT ALL ON TABLE "public"."supplier_kyc_requests" TO "authenticated";
GRANT ALL ON TABLE "public"."supplier_kyc_requests" TO "service_role";



GRANT ALL ON TABLE "public"."supplier_line_item_allocation_rule_splits" TO "anon";
GRANT ALL ON TABLE "public"."supplier_line_item_allocation_rule_splits" TO "authenticated";
GRANT ALL ON TABLE "public"."supplier_line_item_allocation_rule_splits" TO "service_role";



GRANT ALL ON TABLE "public"."supplier_line_item_allocation_rules" TO "anon";
GRANT ALL ON TABLE "public"."supplier_line_item_allocation_rules" TO "authenticated";
GRANT ALL ON TABLE "public"."supplier_line_item_allocation_rules" TO "service_role";



GRANT ALL ON TABLE "public"."suppliers" TO "anon";
GRANT ALL ON TABLE "public"."suppliers" TO "authenticated";
GRANT ALL ON TABLE "public"."suppliers" TO "service_role";



GRANT ALL ON TABLE "public"."themes" TO "anon";
GRANT ALL ON TABLE "public"."themes" TO "authenticated";
GRANT ALL ON TABLE "public"."themes" TO "service_role";



GRANT ALL ON TABLE "public"."tracking_dimensions" TO "anon";
GRANT ALL ON TABLE "public"."tracking_dimensions" TO "authenticated";
GRANT ALL ON TABLE "public"."tracking_dimensions" TO "service_role";



GRANT ALL ON TABLE "public"."tracking_values" TO "anon";
GRANT ALL ON TABLE "public"."tracking_values" TO "authenticated";
GRANT ALL ON TABLE "public"."tracking_values" TO "service_role";



GRANT ALL ON TABLE "public"."user_organisations" TO "authenticated";
GRANT ALL ON TABLE "public"."user_organisations" TO "service_role";



GRANT ALL ON TABLE "public"."user_roles" TO "anon";
GRANT ALL ON TABLE "public"."user_roles" TO "authenticated";
GRANT ALL ON TABLE "public"."user_roles" TO "service_role";



GRANT ALL ON TABLE "public"."user_theme_entitlements" TO "anon";
GRANT ALL ON TABLE "public"."user_theme_entitlements" TO "authenticated";
GRANT ALL ON TABLE "public"."user_theme_entitlements" TO "service_role";



GRANT ALL ON TABLE "public"."user_theme_preferences" TO "anon";
GRANT ALL ON TABLE "public"."user_theme_preferences" TO "authenticated";
GRANT ALL ON TABLE "public"."user_theme_preferences" TO "service_role";



GRANT ALL ON TABLE "public"."whatsapp_pending_selections" TO "anon";
GRANT ALL ON TABLE "public"."whatsapp_pending_selections" TO "authenticated";
GRANT ALL ON TABLE "public"."whatsapp_pending_selections" TO "service_role";



GRANT ALL ON TABLE "public"."xero_connections" TO "anon";
GRANT ALL ON TABLE "public"."xero_connections" TO "authenticated";
GRANT ALL ON TABLE "public"."xero_connections" TO "service_role";



GRANT ALL ON TABLE "public"."xero_tenants" TO "anon";
GRANT ALL ON TABLE "public"."xero_tenants" TO "authenticated";
GRANT ALL ON TABLE "public"."xero_tenants" TO "service_role";



ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";







