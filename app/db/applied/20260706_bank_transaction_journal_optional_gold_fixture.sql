-- Make the corrected gold fixture optional/internal for bank transaction posting,
-- and tier the import gate by trust.
--
-- Background: prior migrations (20260703_*_requires_gold_fixture,
-- _stale_benchmark_guard, and 20260704_single_user_org_gold_fixture_exemption)
-- made the DB trigger demand a corrected gold JSON fixture + a passing benchmark
-- (and, for multi-user orgs, an independent verifier) before ANY pdf/image/vlm
-- bank statement line could be posted to a journal. That treated a clean text PDF
-- that already reconciles to the penny the same as a blurry scan, and pushed an
-- internal ML-accuracy artefact (the gold fixture) into the everyday import flow.
--
-- New model (matches the application layer):
--   * The gold fixture + benchmark become an OPTIONAL, internal accuracy tool.
--     They no longer gate posting. The benchmark endpoints still work.
--   * Trust is tiered by the APPLICATION at extraction time: a clean text-PDF
--     statement whose running balance reconciles is auto-marked 'extracted' and
--     flows straight to reconciliation; a scanned/VLM/unreconciled statement is
--     routed to 'needs_review' and only becomes 'extracted' after a human approves
--     it (the 4-point attestation checklist).
--   * This trigger therefore keeps ONE hard rule: a bank transaction journal can
--     only be posted from a source upload whose extraction_status = 'extracted'.
--     That single check already enforces the trust tier — no fixture required.
--
-- To restore strict fixture enforcement, re-apply
-- 20260704_single_user_org_gold_fixture_exemption.sql and set
-- BANK_REQUIRE_GOLD_FIXTURE=1 on the API so both layers agree.

CREATE OR REPLACE FUNCTION public.prevent_unverified_bank_transaction_journal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  source_line public.bank_statement_lines%ROWTYPE;
  source_upload public.bank_statement_uploads%ROWTYPE;
BEGIN
  IF NEW.source_type IS DISTINCT FROM 'bank_transaction' THEN
    RETURN NEW;
  END IF;

  -- Allow the unpost flow to reverse already-posted bank journals.
  IF TG_OP = 'UPDATE'
     AND OLD.status = 'posted'
     AND NEW.status = 'reversed'
  THEN
    RETURN NEW;
  END IF;

  IF NEW.source_id IS NULL THEN
    RAISE EXCEPTION 'Bank transaction journals require a source bank statement line';
  END IF;

  SELECT *
    INTO source_line
    FROM public.bank_statement_lines line
   WHERE line.id = NEW.source_id
     AND line.organisation_id = NEW.organisation_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Bank transaction journal source line % was not found', NEW.source_id;
  END IF;

  SELECT *
    INTO source_upload
    FROM public.bank_statement_uploads upload
   WHERE upload.id = source_line.bank_statement_upload_id
     AND upload.organisation_id = NEW.organisation_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Bank transaction journal source upload was not found';
  END IF;

  -- The one hard gate: the extraction must have been reviewed/approved (trusted
  -- text PDFs reach this automatically; scanned/VLM statements reach it via the
  -- human attestation checklist). The optional gold fixture no longer gates here.
  IF source_upload.extraction_status IS DISTINCT FROM 'extracted' THEN
    RAISE EXCEPTION
      'Bank transaction journal is blocked until the statement extraction is reviewed and approved';
  END IF;

  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.prevent_unverified_bank_transaction_journal() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.prevent_unverified_bank_transaction_journal()
  TO authenticated, service_role;
