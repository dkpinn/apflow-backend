-- Phase C19: Atomic bank statement deletion with accounting-history guards.
-- Apply in Supabase SQL editor, then move this file to app/db/applied/.

CREATE OR REPLACE FUNCTION public.assert_bank_write(p_org_id UUID)
RETURNS void
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND (
       auth.uid() IS NULL
       OR NOT public.has_org_role(
         p_org_id,
         ARRAY['owner','admin','accountant']::public.organisation_role[]
       )
     )
  THEN
    RAISE EXCEPTION 'Only organisation owners, admins, and accountants can delete bank data';
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.refresh_bank_account_statement_state(
  p_org_id UUID,
  p_bank_account_ids UUID[]
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  account_id UUID;
  latest_upload RECORD;
BEGIN
  FOREACH account_id IN ARRAY coalesce(p_bank_account_ids, ARRAY[]::UUID[])
  LOOP
    SELECT
      upload.id,
      upload.closing_balance
    INTO latest_upload
    FROM public.bank_statement_uploads upload
    WHERE upload.organisation_id = p_org_id
      AND upload.bank_account_id = account_id
      AND upload.extraction_status = 'extracted'
    ORDER BY
      coalesce(
        upload.statement_period_to,
        (
          SELECT max(line.line_date)
          FROM public.bank_statement_lines line
          WHERE line.bank_statement_upload_id = upload.id
        )
      ) DESC NULLS LAST,
      (
        SELECT max(line.line_date)
        FROM public.bank_statement_lines line
        WHERE line.bank_statement_upload_id = upload.id
      ) DESC NULLS LAST,
      upload.uploaded_at DESC,
      upload.id DESC
    LIMIT 1;

    IF FOUND THEN
      UPDATE public.bank_accounts
      SET
        last_statement_upload_id = latest_upload.id,
        current_reconciled_balance = coalesce(
          latest_upload.closing_balance,
          opening_balance
        ),
        updated_at = now()
      WHERE id = account_id
        AND organisation_id = p_org_id;
    ELSE
      UPDATE public.bank_accounts
      SET
        last_statement_upload_id = NULL,
        current_reconciled_balance = opening_balance,
        updated_at = now()
      WHERE id = account_id
        AND organisation_id = p_org_id;
    END IF;
  END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_bank_account_balance_summary(
  p_org_id UUID,
  p_bank_account_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  account_row public.bank_accounts%ROWTYPE;
  latest_upload RECORD;
  imported_balance NUMERIC(14,2);
  tb_balance NUMERIC(14,2);
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND (
       auth.uid() IS NULL
       OR NOT public.is_org_member(p_org_id)
     )
  THEN
    RAISE EXCEPTION 'You do not have access to this organisation';
  END IF;

  SELECT *
  INTO account_row
  FROM public.bank_accounts
  WHERE id = p_bank_account_id
    AND organisation_id = p_org_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Bank account not found';
  END IF;

  SELECT
    upload.id,
    upload.statement_period_to,
    upload.opening_balance,
    upload.closing_balance,
    upload.uploaded_at,
    (
      SELECT max(line.line_date)
      FROM public.bank_statement_lines line
      WHERE line.bank_statement_upload_id = upload.id
    ) AS latest_transaction_date
  INTO latest_upload
  FROM public.bank_statement_uploads upload
  WHERE upload.organisation_id = p_org_id
    AND upload.bank_account_id = p_bank_account_id
    AND upload.extraction_status = 'extracted'
  ORDER BY
    coalesce(
      upload.statement_period_to,
      (
        SELECT max(line.line_date)
        FROM public.bank_statement_lines line
        WHERE line.bank_statement_upload_id = upload.id
      )
    ) DESC NULLS LAST,
    (
      SELECT max(line.line_date)
      FROM public.bank_statement_lines line
      WHERE line.bank_statement_upload_id = upload.id
    ) DESC NULLS LAST,
    upload.uploaded_at DESC,
    upload.id DESC
  LIMIT 1;

  IF latest_upload.id IS NOT NULL
     AND latest_upload.opening_balance IS NOT NULL
  THEN
    SELECT round(
      latest_upload.opening_balance + coalesce(sum(line.signed_amount), 0),
      2
    )
    INTO imported_balance
    FROM public.bank_statement_lines line
    WHERE line.organisation_id = p_org_id
      AND line.bank_statement_upload_id = latest_upload.id;
  END IF;

  IF account_row.gl_account_id IS NOT NULL THEN
    SELECT round(
      coalesce(sum(line.debit_amount - line.credit_amount), 0),
      2
    )
    INTO tb_balance
    FROM public.gl_journal_lines line
    JOIN public.gl_journals journal
      ON journal.id = line.gl_journal_id
     AND journal.organisation_id = p_org_id
     AND journal.status = 'posted'
    WHERE line.organisation_id = p_org_id
      AND line.account_id = account_row.gl_account_id;
  END IF;

  RETURN jsonb_build_object(
    'bank_statement_balance', latest_upload.closing_balance,
    'calculated_imported_balance', imported_balance,
    'current_tb_balance', tb_balance,
    'latest_statement_upload_id', latest_upload.id,
    'statement_period_to', latest_upload.statement_period_to,
    'latest_transaction_date', latest_upload.latest_transaction_date,
    'bank_balance_status', CASE
      WHEN latest_upload.closing_balance IS NULL THEN 'unavailable'
      ELSE 'available'
    END,
    'imported_balance_status', CASE
      WHEN imported_balance IS NULL THEN 'unavailable'
      ELSE 'available'
    END,
    'tb_balance_status', CASE
      WHEN account_row.gl_account_id IS NULL THEN 'gl_account_not_linked'
      ELSE 'available'
    END
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.delete_bank_statement_lines_atomic(
  p_org_id UUID,
  p_line_ids UUID[],
  p_actor_user_id UUID
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
BEGIN
  PERFORM public.assert_bank_write(p_org_id);
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND p_actor_user_id IS DISTINCT FROM auth.uid()
  THEN
    RAISE EXCEPTION 'Bank deletion actor does not match the authenticated user';
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
    );

  IF blocked IS NOT NULL THEN
    RAISE EXCEPTION USING
      MESSAGE = 'Bank statement deletion blocked by posted or reversed journal history',
      DETAIL = blocked::TEXT,
      ERRCODE = 'P0001';
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
        'deleted_count', deleted_count
      )
    );
  END LOOP;

  RETURN jsonb_build_object(
    'deleted_count', deleted_count,
    'line_ids', to_jsonb(requested_ids),
    'affected_upload_ids', to_jsonb(affected_upload_ids),
    'affected_account_ids', to_jsonb(affected_account_ids)
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.delete_bank_statement_uploads_atomic(
  p_org_id UUID,
  p_upload_ids UUID[],
  p_actor_user_id UUID
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
BEGIN
  PERFORM public.assert_bank_write(p_org_id);
  IF auth.role() IS DISTINCT FROM 'service_role'
     AND p_actor_user_id IS DISTINCT FROM auth.uid()
  THEN
    RAISE EXCEPTION 'Bank deletion actor does not match the authenticated user';
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
  END IF;

  DELETE FROM public.bank_statement_uploads
  WHERE organisation_id = p_org_id
    AND id = ANY(requested_ids);
  GET DIAGNOSTICS deleted_count = ROW_COUNT;

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
        'files', files
      )
    );
  END LOOP;

  RETURN jsonb_build_object(
    'deleted_count', deleted_count,
    'upload_ids', to_jsonb(requested_ids),
    'affected_account_ids', to_jsonb(affected_account_ids),
    'files', files
  );
END;
$$;

REVOKE ALL ON FUNCTION public.assert_bank_write(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.refresh_bank_account_statement_state(UUID, UUID[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_bank_account_balance_summary(UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.delete_bank_statement_lines_atomic(UUID, UUID[], UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.delete_bank_statement_uploads_atomic(UUID, UUID[], UUID) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.get_bank_account_balance_summary(UUID, UUID)
  TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.delete_bank_statement_lines_atomic(UUID, UUID[], UUID)
  TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.delete_bank_statement_uploads_atomic(UUID, UUID[], UUID)
  TO authenticated, service_role;

SELECT 'bank_deletion_balance_hardening_phase_c19_ready' AS migration_note;
