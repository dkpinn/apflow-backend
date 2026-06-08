-- ============================================================
-- Statement line review workflow
-- Apply this migration via the Lovable Cloud migration tool.
-- ============================================================

-- 1. Add review_status to statement_lines
alter table public.statement_lines
  add column if not exists review_status text not null default 'pending'
    check (review_status in ('pending', 'resolved', 'ignored'));

create index if not exists statement_lines_review_status_idx
  on public.statement_lines(review_status);

-- 2. Optional: track manual review actions (audit trail)
alter table public.statement_lines
  add column if not exists review_action text,
  add column if not exists review_invoice_id uuid,
  add column if not exists reviewed_at timestamptz,
  add column if not exists reviewed_by uuid;
