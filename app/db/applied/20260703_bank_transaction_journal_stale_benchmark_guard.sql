-- Review and apply with Supabase CLI. Do not move to app/db/applied until pushed.
--
-- Strengthen the corrected-gold benchmark guard:
-- a passing benchmark is valid only if it was run after the corrected gold file
-- was verified and after the current upload extraction completed.

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
  upload_extracted_at timestamptz;
  latest_failed_count integer := 0;
  missing_run_count integer := 0;
  stale_gold_count integer := 0;
  stale_upload_count integer := 0;
BEGIN
  SELECT upload.extracted_at
    INTO upload_extracted_at
    FROM public.bank_statement_uploads upload
   WHERE upload.organisation_id = p_org_id
     AND upload.id = p_upload_id;

  WITH upload_gold_files AS (
    SELECT DISTINCT
           gf.id,
           gf.document_id,
           coalesce(gf.verified_at, gf.created_at) AS gold_verified_at
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
       )
  ),
  latest_runs AS (
    SELECT DISTINCT ON (run.document_id)
           run.document_id,
           run.can_allocate,
           run.created_at
      FROM public.bank_statement_extraction_runs run
     WHERE run.organisation_id = p_org_id
       AND run.bank_statement_upload_id = p_upload_id
       AND run.document_id IN (SELECT document_id FROM upload_gold_files)
     ORDER BY run.document_id, run.created_at DESC, run.id DESC
  )
  SELECT
    count(*) FILTER (WHERE run.document_id IS NULL),
    count(*) FILTER (WHERE run.can_allocate IS DISTINCT FROM true),
    count(*) FILTER (
      WHERE gold.gold_verified_at IS NOT NULL
        AND (run.created_at IS NULL OR run.created_at < gold.gold_verified_at)
    ),
    count(*) FILTER (
      WHERE upload_extracted_at IS NOT NULL
        AND (run.created_at IS NULL OR run.created_at < upload_extracted_at)
    )
    INTO missing_run_count,
         latest_failed_count,
         stale_gold_count,
         stale_upload_count
    FROM upload_gold_files gold
    LEFT JOIN latest_runs run ON run.document_id = gold.document_id;

  IF missing_run_count > 0 THEN
    blocker_messages := blocker_messages
      || 'Corrected gold fixture has not been benchmarked against the parser output';
  END IF;

  IF latest_failed_count > 0 THEN
    blocker_messages := blocker_messages
      || 'Corrected gold fixture benchmark did not match the parser output';
  END IF;

  IF stale_gold_count > 0 THEN
    blocker_messages := blocker_messages
      || 'Corrected gold fixture benchmark is stale; rerun it after the latest correction';
  END IF;

  IF stale_upload_count > 0 THEN
    blocker_messages := blocker_messages
      || 'Corrected gold fixture benchmark is stale; rerun it after the latest extraction';
  END IF;

  RETURN blocker_messages;
END;
$$;

REVOKE ALL ON FUNCTION public.bank_upload_corrected_fixture_blockers(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.bank_upload_corrected_fixture_blockers(uuid, uuid)
  TO authenticated, service_role;
