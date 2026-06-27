-- Recurring Transactions: templates + generated drafts

CREATE TABLE IF NOT EXISTS public.recurring_transaction_templates (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id   uuid NOT NULL REFERENCES public.organisations(id) ON DELETE CASCADE,
  name              text NOT NULL,
  transaction_type  text NOT NULL,  -- supplier_invoice | sales_invoice | journal_entry | bank_standing_order | finance_repayment | operational_repayment
  schedule_type     text NOT NULL,  -- weekly | monthly | quarterly | annually
  schedule_day      int,            -- day of month (1-31) for monthly/quarterly/annually; 0-6 (Mon=0) for weekly
  amount            numeric(15,2) NOT NULL,
  currency          text NOT NULL DEFAULT 'ZAR',
  supplier_id       uuid REFERENCES public.suppliers(id) ON DELETE SET NULL,
  customer_id       uuid REFERENCES public.customers(id) ON DELETE SET NULL,
  description       text,
  reference         text,
  template_data     jsonb NOT NULL DEFAULT '{}',
  status            text NOT NULL DEFAULT 'active',  -- active | paused | completed
  start_date        date NOT NULL,
  end_date          date,
  next_due_date     date NOT NULL,
  last_generated_at timestamptz,
  created_by        uuid,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.recurring_transaction_drafts (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id  uuid NOT NULL REFERENCES public.organisations(id) ON DELETE CASCADE,
  template_id      uuid NOT NULL REFERENCES public.recurring_transaction_templates(id) ON DELETE CASCADE,
  due_date         date NOT NULL,
  transaction_type text NOT NULL,
  amount           numeric(15,2) NOT NULL,
  currency         text NOT NULL DEFAULT 'ZAR',
  description      text,
  reference        text,
  draft_data       jsonb NOT NULL DEFAULT '{}',
  status           text NOT NULL DEFAULT 'pending',  -- pending | approved | skipped
  posted_record_id uuid,
  reviewed_by      uuid,
  reviewed_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_recurring_templates_org_status_due
  ON public.recurring_transaction_templates (organisation_id, status, next_due_date);

CREATE INDEX IF NOT EXISTS idx_recurring_drafts_org_status_due
  ON public.recurring_transaction_drafts (organisation_id, status, due_date);

-- Prevent double-generating a draft for the same template + due_date
CREATE UNIQUE INDEX IF NOT EXISTS idx_recurring_drafts_template_due
  ON public.recurring_transaction_drafts (template_id, due_date);

-- updated_at trigger for templates
CREATE OR REPLACE TRIGGER recurring_templates_updated_at
  BEFORE UPDATE ON public.recurring_transaction_templates
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- RLS
ALTER TABLE public.recurring_transaction_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recurring_transaction_drafts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Org members can manage recurring templates"
  ON public.recurring_transaction_templates
  FOR ALL TO authenticated
  USING (public.is_org_member(organisation_id));

CREATE POLICY "Org members can manage recurring drafts"
  ON public.recurring_transaction_drafts
  FOR ALL TO authenticated
  USING (public.is_org_member(organisation_id));
