-- Phase C11: Amount-tiered supplier auto-link thresholds and per-supplier STP.
-- Apply in Supabase SQL editor, then move this file to app/db/applied/.

-- Amount-tiered matching: higher invoice amounts can require more identity signals.
-- Format: [{"max_amount": 1000, "required_matches": 2}, {"max_amount": null, "required_matches": 4}]
-- Empty array [] = fall back to organisations.supplier_auto_link_min_matches (existing behaviour).
ALTER TABLE public.organisations
  ADD COLUMN IF NOT EXISTS auto_link_amount_tiers JSONB NOT NULL DEFAULT '[]';

COMMENT ON COLUMN public.organisations.auto_link_amount_tiers IS
  'Per-amount-tier supplier auto-link thresholds. Each entry: {max_amount: numeric|null, required_matches: 1-4}. '
  'Tiers evaluated ascending by max_amount; null max_amount = catch-all. '
  'Falls back to supplier_auto_link_min_matches when empty.';

-- Per-supplier straight-through processing: auto-post without human review.
ALTER TABLE public.suppliers
  ADD COLUMN IF NOT EXISTS stp_enabled BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS stp_max_amount NUMERIC(14,2);

COMMENT ON COLUMN public.suppliers.stp_enabled IS
  'When true, qualifying extractions for this supplier are auto-posted to GL without manual review.';
COMMENT ON COLUMN public.suppliers.stp_max_amount IS
  'Maximum invoice total eligible for STP. NULL = no limit. Amounts above this land in Needs Review.';
