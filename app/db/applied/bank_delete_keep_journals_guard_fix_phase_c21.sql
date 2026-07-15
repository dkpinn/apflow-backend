-- Phase C21: Fix the 'keep_journals' delete guard so reverse-then-delete works.
-- Apply in Supabase SQL editor, then move this file to app/db/applied/.
--
-- Bug (phase C20): in 'keep_journals' mode the guard blocked a line when ANY journal
-- with status='posted' matched source_id=line.id AND source_type IN
-- ('bank_transaction','bank_transaction_reversal'). The reverse-then-delete flow first
-- reverses each posted journal, which creates a NEW contra journal
-- (source_type='bank_transaction_reversal', source_id=line.id, status='posted') — so the
-- freshly-created reversal journal matched the guard and blocked its own deletion.
--
-- Fix: in 'keep_journals' mode only an ORIGINAL 'bank_transaction' journal that is still
-- 'posted' blocks. Reversed originals (status='reversed') and posted contra reversals are
-- expected and allowed. 'block' mode is unchanged; 'hard' still skips the guard.

DROP FUNCTION IF EXISTS public.delete_bank_statement_lines_atomic(UUID, UUID[], UUID, TEXT);
DROP FUNCTION IF EXISTS public.delete_bank_statement_uploads_atomic(UUID, UUID[], UUID, TEXT);

CREATE FUNCTION public.delete_bank_statement_lines_atomic(
  p_org_id UUID,
  p_line_ids UUID[],
  p_actor_user_id UUID,
  p_mode TEXT DEFAULT 'block'
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  requested_ids UUID[];
  found_count INTEGER;
  blocked JSONB;
  affected_upload_ids UUID[];
  affected_account_ids UUID[];
  deleted_count INTEGER;
  account_id UUID;
  journal_ids UUID[];
BEGIN
  IF p_mode IS NULL OR p_mode NOT IN ('block', 'keep_journals', 'hard') THEN
    RAISE EXCEPTION 'Invalid delete mode: %', p_mode;
  END IF;

  PERFORM public.assert_bank_write(p_org_id);
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND p_actor_user_id IS DISTINCT FROM auth.uid()
  THEN
    RAISE EXCEPTION 'Bank deletion actor does not match the authenticated user';
  END IF;

  IF p_mode = 'hard'
     AND auth.role() IS DISTINCT FROM 'service_role'
     AND NOT public.has_org_role(
       p_org_id,
       ARRAY['owner', 'admin']::public.organisation_role[]
     )
  THEN
    RAISE EXCEPTION 'Only organisation owners and admins can permanently delete posted bank transactions';
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

  -- Guard: 'block' refuses any posted/reversed history; 'keep_journals' only refuses
  -- an original bank_transaction journal that is still posted; 'hard' skips the guard.
  IF p_mode IN ('block', 'keep_journals') THEN
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
            CASE
              WHEN p_mode = 'block' THEN
                (
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
              ELSE
                -- keep_journals: only an ORIGINAL posted bank_transaction journal blocks.
                journal.source_type = 'bank_transaction'
                AND (journal.source_id = line.id OR journal.id = line.gl_journal_id)
                AND journal.status = 'posted'
            END
          )
      );

    IF blocked IS NOT NULL THEN
      RAISE EXCEPTION USING
        MESSAGE = 'Bank statement deletion blocked by posted or reversed journal history',
        DETAIL = blocked::TEXT,
        ERRCODE = 'P0001';
    END IF;
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

  -- Hard mode: remove every remaining journal tied to these lines (posted, reversed,
  -- and their contra reversals). gl_journal_lines cascade via FK; a single DELETE over
  -- both originals and their reversals satisfies the reversal_of_journal_id FK.
  IF p_mode = 'hard' THEN
    SELECT coalesce(array_agg(j.id), ARRAY[]::UUID[])
    INTO journal_ids
    FROM public.gl_journals j
    WHERE j.organisation_id = p_org_id
      AND (
        (j.source_type IN ('bank_transaction', 'bank_transaction_reversal')
          AND j.source_id = ANY(requested_ids))
        OR j.id IN (
          SELECT line.gl_journal_id
          FROM public.bank_statement_lines line
          WHERE line.organisation_id = p_org_id
            AND line.id = ANY(requested_ids)
            AND line.gl_journal_id IS NOT NULL
        )
        OR j.reversal_of_journal_id IN (
          SELECT j2.id
          FROM public.gl_journals j2
          WHERE j2.organisation_id = p_org_id
            AND j2.source_type = 'bank_transaction'
            AND j2.source_id = ANY(requested_ids)
        )
      );

    IF cardinality(journal_ids) > 0 THEN
      DELETE FROM public.gl_journals WHERE id = ANY(journal_ids);
    END IF;
  END IF;

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

  -- Put rule auto-posting on hold once we've torn down postings.
  IF p_mode IN ('keep_journals', 'hard') THEN
    UPDATE public.bank_accounts
    SET
      auto_post_paused = true,
      auto_post_paused_at = now(),
      auto_post_paused_reason = 'statement_deleted'
    WHERE organisation_id = p_org_id
      AND id = ANY(affected_account_ids);
  END IF;

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
        'deleted_count', deleted_count,
        'mode', p_mode
      )
    );
  END LOOP;

  RETURN jsonb_build_object(
    'deleted_count', deleted_count,
    'line_ids', to_jsonb(requested_ids),
    'affected_upload_ids', to_jsonb(affected_upload_ids),
    'affected_account_ids', to_jsonb(affected_account_ids),
    'mode', p_mode
  );
END;
$$;

CREATE FUNCTION public.delete_bank_statement_uploads_atomic(
  p_org_id UUID,
  p_upload_ids UUID[],
  p_actor_user_id UUID,
  p_mode TEXT DEFAULT 'block'
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
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
  journal_ids UUID[];
BEGIN
  IF p_mode IS NULL OR p_mode NOT IN ('block', 'keep_journals', 'hard') THEN
    RAISE EXCEPTION 'Invalid delete mode: %', p_mode;
  END IF;

  PERFORM public.assert_bank_write(p_org_id);
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND p_actor_user_id IS DISTINCT FROM auth.uid()
  THEN
    RAISE EXCEPTION 'Bank deletion actor does not match the authenticated user';
  END IF;

  IF p_mode = 'hard'
     AND auth.role() IS DISTINCT FROM 'service_role'
     AND NOT public.has_org_role(
       p_org_id,
       ARRAY['owner', 'admin']::public.organisation_role[]
     )
  THEN
    RAISE EXCEPTION 'Only organisation owners and admins can permanently delete posted bank transactions';
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

  -- Guard: 'block' refuses any posted/reversed history; 'keep_journals' only refuses
  -- an original bank_transaction journal that is still posted; 'hard' skips the guard.
  IF p_mode IN ('block', 'keep_journals') THEN
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
              CASE
                WHEN p_mode = 'block' THEN
                  (
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
                ELSE
                  -- keep_journals: only an ORIGINAL posted bank_transaction journal blocks.
                  journal.source_type = 'bank_transaction'
                  AND (journal.source_id = line.id OR journal.id = line.gl_journal_id)
                  AND journal.status = 'posted'
              END
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

    -- Hard mode: remove every remaining journal tied to these lines (posted, reversed,
    -- and their contra reversals).
    IF p_mode = 'hard' THEN
      SELECT coalesce(array_agg(j.id), ARRAY[]::UUID[])
      INTO journal_ids
      FROM public.gl_journals j
      WHERE j.organisation_id = p_org_id
        AND (
          (j.source_type IN ('bank_transaction', 'bank_transaction_reversal')
            AND j.source_id = ANY(line_ids))
          OR j.id IN (
            SELECT line.gl_journal_id
            FROM public.bank_statement_lines line
            WHERE line.organisation_id = p_org_id
              AND line.id = ANY(line_ids)
              AND line.gl_journal_id IS NOT NULL
          )
          OR j.reversal_of_journal_id IN (
            SELECT j2.id
            FROM public.gl_journals j2
            WHERE j2.organisation_id = p_org_id
              AND j2.source_type = 'bank_transaction'
              AND j2.source_id = ANY(line_ids)
          )
        );

      IF cardinality(journal_ids) > 0 THEN
        DELETE FROM public.gl_journals WHERE id = ANY(journal_ids);
      END IF;
    END IF;
  END IF;

  DELETE FROM public.bank_statement_uploads
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids);
  GET DIAGNOSTICS deleted_count = ROW_COUNT;

  -- Put rule auto-posting on hold once we've torn down postings.
  IF p_mode IN ('keep_journals', 'hard') THEN
    UPDATE public.bank_accounts
    SET
      auto_post_paused = true,
      auto_post_paused_at = now(),
      auto_post_paused_reason = 'statement_deleted'
    WHERE organisation_id = p_org_id
      AND id = ANY(affected_account_ids);
  END IF;

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
        'files', files,
        'mode', p_mode
      )
    );
  END LOOP;

  RETURN jsonb_build_object(
    'deleted_count', deleted_count,
    'upload_ids', to_jsonb(requested_ids),
    'affected_account_ids', to_jsonb(affected_account_ids),
    'files', files,
    'mode', p_mode
  );
END;
$$;

REVOKE ALL ON FUNCTION public.delete_bank_statement_lines_atomic(UUID, UUID[], UUID, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.delete_bank_statement_uploads_atomic(UUID, UUID[], UUID, TEXT) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.delete_bank_statement_lines_atomic(UUID, UUID[], UUID, TEXT)
  TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.delete_bank_statement_uploads_atomic(UUID, UUID[], UUID, TEXT)
  TO authenticated, service_role;

SELECT 'bank_delete_keep_journals_guard_fix_phase_c21_ready' AS migration_note;
