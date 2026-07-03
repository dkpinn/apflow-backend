-- Review and apply with Supabase CLI. Do not move to app/db/applied until pushed.
--
-- Strengthen the bank transaction extraction guardrail:
-- PDF/image/VLM-derived bank statement uploads must have a corrected gold JSON
-- fixture and a latest passing benchmark before any bank_transaction journal can
-- be inserted or posted. Checkbox approval alone is not enough for high-risk
-- document sources.

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
  source_format text;
  parser_strategy text;
  pdf_rescue_selected text;
  approved_by text;
  corrected_fixture_count integer := 0;
  independent_corrected_fixture_count integer := 0;
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

  source_format := lower(coalesce(
    source_upload.source_format,
    source_upload.extraction_evidence->>'source_format',
    source_upload.raw_extraction->>'source_format',
    ''
  ));
  parser_strategy := lower(coalesce(
    source_upload.extraction_evidence->>'parser_strategy',
    source_upload.raw_extraction->>'parser_strategy',
    ''
  ));
  pdf_rescue_selected := lower(coalesce(
    source_upload.extraction_evidence->'pdf_rescue'->>'selected',
    source_upload.raw_extraction->'pdf_rescue'->>'selected',
    ''
  ));
  approved_by := source_upload.extraction_evidence->'manual_review'->>'approved_by';

  IF source_format IN ('pdf', 'image', 'vlm', 'png', 'jpg', 'jpeg', 'webp', 'heic', 'heif', 'tif', 'tiff')
     OR parser_strategy = 'vlm'
     OR parser_strategy LIKE 'vlm\_%' ESCAPE '\'
     OR parser_strategy LIKE '%\_vlm%' ESCAPE '\'
     OR pdf_rescue_selected = 'vlm'
  THEN
    SELECT count(*)
      INTO corrected_fixture_count
      FROM public.bank_statement_gold_files gf
     WHERE gf.organisation_id = NEW.organisation_id
       AND (
         gf.gold_json->>'_apflow_source_upload_id' = source_upload.id::text
         OR EXISTS (
           SELECT 1
             FROM public.bank_audit_events event
            WHERE event.organisation_id = NEW.organisation_id
              AND event.event_type = 'bank_statement_gold_file_created'
              AND event.bank_statement_upload_id = source_upload.id
              AND event.details->>'gold_file_id' = gf.id::text
         )
       );

    IF corrected_fixture_count = 0 THEN
      RAISE EXCEPTION
        'Bank transaction journal is blocked: PDF/image/VLM bank statement extraction requires a corrected gold fixture and passing benchmark';
    END IF;

    IF approved_by IS NULL OR approved_by = '' THEN
      RAISE EXCEPTION
        'Bank transaction journal is blocked: PDF/image/VLM bank statement extraction requires recorded manual approval';
    END IF;

    SELECT count(*)
      INTO independent_corrected_fixture_count
      FROM public.bank_statement_gold_files gf
     WHERE gf.organisation_id = NEW.organisation_id
       AND gf.verified_by IS NOT NULL
       AND gf.verified_by::text <> approved_by
       AND (
         gf.gold_json->>'_apflow_source_upload_id' = source_upload.id::text
         OR EXISTS (
           SELECT 1
             FROM public.bank_audit_events event
            WHERE event.organisation_id = NEW.organisation_id
              AND event.event_type = 'bank_statement_gold_file_created'
              AND event.bank_statement_upload_id = source_upload.id
              AND event.details->>'gold_file_id' = gf.id::text
         )
       );

    IF independent_corrected_fixture_count = 0 THEN
      RAISE EXCEPTION
        'Bank transaction journal is blocked: PDF/image/VLM bank statement extraction approval must be performed by a reviewer different from the corrected gold fixture verifier';
    END IF;
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

REVOKE ALL ON FUNCTION public.prevent_unverified_bank_transaction_journal() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.prevent_unverified_bank_transaction_journal()
  TO authenticated, service_role;
