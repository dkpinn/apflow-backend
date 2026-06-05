-- Phase C8: Invoice → GL posting columns.
-- Run in Supabase SQL editor, then move to app/db/applied/.

ALTER TABLE public.invoices_extracted
  ADD COLUMN IF NOT EXISTS gl_journal_id   UUID REFERENCES public.gl_journals(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS posting_status  TEXT
    CHECK (posting_status IN ('unposted', 'posted', 'reversed'))
    DEFAULT 'unposted',
  ADD COLUMN IF NOT EXISTS posted_at       TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS posted_by       UUID REFERENCES auth.users(id) ON DELETE SET NULL;

COMMENT ON COLUMN public.invoices_extracted.gl_journal_id  IS 'GL journal created when invoice was posted.';
COMMENT ON COLUMN public.invoices_extracted.posting_status IS 'unposted | posted | reversed';
