-- Review and apply with Supabase CLI. Do not move to app/db/applied until pushed.
--
-- Database-level guardrail for bank statement extraction integrity.
-- Route handlers already block draft/posting for unapproved or benchmark-failed
-- uploads. This trigger closes the direct SQL/RPC bypass by preventing any
-- bank_transaction GL journal from being inserted/posted unless the source
-- bank statement upload is approved and any corrected gold fixture for that
-- upload has a latest passing benchmark run.

CREATE OR REPLACE FUNCTION public.bank_upload_corrected_fixture_blockers(
  p_org_id uuid,
  p_upload_id uuid
)
RETURNS text[]
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  blocker_messages text[] := ARRAY[]::text[];
  gold_document_ids text[];
  latest_failed_count integer := 0;
  missing_run_count integer := 0;
BEGIN
  SELECT array_agg(DISTINCT gf.document_id)
    INTO gold_document_ids
    FROM public.bank_statement_gold_files gf
   WHERE gf.organisation_id = p_org_id
     AND (
       gf.gold_json->>'_apflow_source_upload_id' = p_upload_id::text
       OR EXISTS (
         SELECT 1
           FROM public.bank_audit_events event
          WHERE event.organisation_id = p_org_id
            AND event.event_type = 'bank_statement_gold_file_created'
            AND event.bank_statement_upload_id = p_upload_id
            AND event.details->>'gold_file_id' = gf.id::text
       )
     );

  IF gold_document_ids IS NULL OR array_length(gold_document_ids, 1) IS NULL THEN
    RETURN blocker_messages;
  END IF;

  WITH gold_docs AS (
    SELECT unnest(gold_document_ids) AS document_id
  ),
  latest_runs AS (
    SELECT DISTINCT ON (run.document_id)
           run.document_id,
           run.can_allocate
      FROM public.bank_statement_extraction_runs run
     WHERE run.organisation_id = p_org_id
       AND run.bank_statement_upload_id = p_upload_id
       AND run.document_id = ANY(gold_document_ids)
     ORDER BY run.document_id, run.created_at DESC, run.id DESC
  )
  SELECT count(*)
    INTO missing_run_count
    FROM gold_docs doc
    LEFT JOIN latest_runs run ON run.document_id = doc.document_id
   WHERE run.document_id IS NULL;

  IF missing_run_count > 0 THEN
    blocker_messages := blocker_messages
      || 'Corrected gold fixture has not been benchmarked against the parser output';
  END IF;

  WITH latest_runs AS (
    SELECT DISTINCT ON (run.document_id)
           run.document_id,
           run.can_allocate
      FROM public.bank_statement_extraction_runs run
     WHERE run.organisation_id = p_org_id
       AND run.bank_statement_upload_id = p_upload_id
       AND run.document_id = ANY(gold_document_ids)
     ORDER BY run.document_id, run.created_at DESC, run.id DESC
  )
  SELECT count(*)
    INTO latest_failed_count
    FROM latest_runs run
   WHERE run.can_allocate IS DISTINCT FROM true;

  IF latest_failed_count > 0 THEN
    blocker_messages := blocker_messages
      || 'Corrected gold fixture benchmark did not match the parser output';
  END IF;

  RETURN blocker_messages;
END;
$$;

CREATE OR REPLACE FUNCTION public.prevent_unverified_bank_transaction_journal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  source_line public.bank_statement_lines%ROWTYPE;
  source_upload public.bank_statement_uploads%ROWTYPE;
  blockers text[];
BEGIN
  IF NEW.source_type IS DISTINCT FROM 'bank_transaction' THEN
    RETURN NEW;
  END IF;

  -- Discovery of a bad extraction must not trap existing accounting in place.
  -- Allow the unpost flow to mark the original posted bank journal as reversed;
  -- the separate reversal journal uses source_type = 'bank_transaction_reversal'.
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

  IF source_upload.extraction_status IS DISTINCT FROM 'extracted' THEN
    RAISE EXCEPTION
      'Bank transaction journal is blocked until the statement extraction is reviewed and approved';
  END IF;

  blockers := public.bank_upload_corrected_fixture_blockers(
    NEW.organisation_id,
    source_upload.id
  );

  IF array_length(blockers, 1) IS NOT NULL THEN
    RAISE EXCEPTION 'Bank transaction journal is blocked: %', array_to_string(blockers, '; ');
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS prevent_unverified_bank_transaction_journal_trigger
  ON public.gl_journals;

CREATE TRIGGER prevent_unverified_bank_transaction_journal_trigger
  BEFORE INSERT OR UPDATE OF status, source_type, source_id
  ON public.gl_journals
  FOR EACH ROW
  EXECUTE FUNCTION public.prevent_unverified_bank_transaction_journal();

REVOKE ALL ON FUNCTION public.bank_upload_corrected_fixture_blockers(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.bank_upload_corrected_fixture_blockers(uuid, uuid)
  TO authenticated, service_role;

REVOKE ALL ON FUNCTION public.prevent_unverified_bank_transaction_journal() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.prevent_unverified_bank_transaction_journal()
  TO authenticated, service_role;
