-- Phase B10: Chart of Accounts + Tracking Dimensions
-- Creates: org_integrations, accounts, account_mappings, tracking_dimensions, tracking_values
-- RLS: org members SELECT; owners/admins/accountants INSERT+UPDATE; no DELETE policies.
-- Apply via Supabase SQL Editor.

-- ── org_integrations ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.org_integrations (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid        NOT NULL REFERENCES public.organisations(id) ON DELETE CASCADE,
  name            text        NOT NULL,   -- slug: 'xero', 'sage', 'shopify', 'quickbooks'
  display_name    text        NOT NULL,   -- human: 'Xero', 'Sage 200', 'Shopify'
  active          boolean     NOT NULL DEFAULT true,
  position        smallint    NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organisation_id, name)
);

-- ── accounts ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.accounts (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid        NOT NULL REFERENCES public.organisations(id) ON DELETE CASCADE,
  code            text,
  name            text        NOT NULL,
  type            text        NOT NULL DEFAULT 'expense'
                                CHECK (type IN ('income','expense','asset','liability','equity','other')),
  group_name      text,
  description     text,
  active          boolean     NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ── account_mappings ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.account_mappings (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id     uuid        NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  integration_id uuid        NOT NULL REFERENCES public.org_integrations(id) ON DELETE CASCADE,
  external_code  text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, integration_id)
);

-- ── tracking_dimensions ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.tracking_dimensions (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid        NOT NULL REFERENCES public.organisations(id) ON DELETE CASCADE,
  name            text        NOT NULL,
  position        smallint    NOT NULL CHECK (position BETWEEN 1 AND 5),
  active          boolean     NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organisation_id, position)
);

-- ── tracking_values ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.tracking_values (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  dimension_id uuid        NOT NULL REFERENCES public.tracking_dimensions(id) ON DELETE CASCADE,
  code         text,
  name         text        NOT NULL,
  active       boolean     NOT NULL DEFAULT true,
  sort_order   smallint    NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ── RLS ──────────────────────────────────────────────────────────────────────
ALTER TABLE public.org_integrations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.accounts            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.account_mappings    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tracking_dimensions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tracking_values     ENABLE ROW LEVEL SECURITY;

-- SELECT: any active org member
DROP POLICY IF EXISTS "org_integrations_select_member" ON public.org_integrations;
CREATE POLICY "org_integrations_select_member" ON public.org_integrations
  FOR SELECT TO authenticated USING (public.is_org_member(organisation_id));

DROP POLICY IF EXISTS "accounts_select_member" ON public.accounts;
CREATE POLICY "accounts_select_member" ON public.accounts
  FOR SELECT TO authenticated USING (public.is_org_member(organisation_id));

DROP POLICY IF EXISTS "account_mappings_select" ON public.account_mappings;
CREATE POLICY "account_mappings_select" ON public.account_mappings
  FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM public.accounts a WHERE a.id = account_id AND public.is_org_member(a.organisation_id)));

DROP POLICY IF EXISTS "tracking_dimensions_select" ON public.tracking_dimensions;
CREATE POLICY "tracking_dimensions_select" ON public.tracking_dimensions
  FOR SELECT TO authenticated USING (public.is_org_member(organisation_id));

DROP POLICY IF EXISTS "tracking_values_select" ON public.tracking_values;
CREATE POLICY "tracking_values_select" ON public.tracking_values
  FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM public.tracking_dimensions td WHERE td.id = dimension_id AND public.is_org_member(td.organisation_id)));

-- INSERT + UPDATE: owners, admins, accountants only
DROP POLICY IF EXISTS "org_integrations_write" ON public.org_integrations;
CREATE POLICY "org_integrations_write" ON public.org_integrations
  FOR ALL TO authenticated
  USING      (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  WITH CHECK (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

DROP POLICY IF EXISTS "accounts_write" ON public.accounts;
CREATE POLICY "accounts_write" ON public.accounts
  FOR ALL TO authenticated
  USING      (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  WITH CHECK (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

DROP POLICY IF EXISTS "account_mappings_write" ON public.account_mappings;
CREATE POLICY "account_mappings_write" ON public.account_mappings
  FOR ALL TO authenticated
  USING      (EXISTS (SELECT 1 FROM public.accounts a WHERE a.id = account_id AND public.has_org_role(a.organisation_id, array['owner','admin','accountant']::public.organisation_role[])))
  WITH CHECK (EXISTS (SELECT 1 FROM public.accounts a WHERE a.id = account_id AND public.has_org_role(a.organisation_id, array['owner','admin','accountant']::public.organisation_role[])));

DROP POLICY IF EXISTS "tracking_dimensions_write" ON public.tracking_dimensions;
CREATE POLICY "tracking_dimensions_write" ON public.tracking_dimensions
  FOR ALL TO authenticated
  USING      (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  WITH CHECK (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

DROP POLICY IF EXISTS "tracking_values_write" ON public.tracking_values;
CREATE POLICY "tracking_values_write" ON public.tracking_values
  FOR ALL TO authenticated
  USING      (EXISTS (SELECT 1 FROM public.tracking_dimensions td WHERE td.id = dimension_id AND public.has_org_role(td.organisation_id, array['owner','admin','accountant']::public.organisation_role[])))
  WITH CHECK (EXISTS (SELECT 1 FROM public.tracking_dimensions td WHERE td.id = dimension_id AND public.has_org_role(td.organisation_id, array['owner','admin','accountant']::public.organisation_role[])));

-- No DELETE policies — deactivate only, enforced in application layer.
