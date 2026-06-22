-- Phase C21: atomically create balanced bank-transaction draft journals.

CREATE OR REPLACE FUNCTION public.create_bank_draft_journals_atomic(
  p_org_id UUID,
  p_items JSONB,
  p_actor_user_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  item JSONB;
  allocation JSONB;
  source_line public.bank_statement_lines%ROWTYPE;
  allocation_account RECORD;
  bank_gl_id UUID;
  vat_control_id UUID;
  journal_id UUID;
  line_id UUID;
  account_id UUID;
  required_dimension_ids UUID[] := ARRAY[]::UUID[];
  required_dimension_id UUID;
  tracking JSONB;
  effective_treatment TEXT;
  gross_amount NUMERIC(14,2);
  allocation_total NUMERIC(14,2);
  net_amount NUMERIC(14,2);
  vat_amount NUMERIC(14,2);
  vat_total NUMERIC(14,2);
  vat_rate NUMERIC(7,4);
  rounding_difference NUMERIC(14,2);
  journal_total NUMERIC(14,2);
  sort_index SMALLINT;
  organisation_is_vat_vendor BOOLEAN;
  item_count INTEGER;
  unique_line_count INTEGER;
  found_line_count INTEGER;
  result_items JSONB := '[]'::JSONB;
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
    RAISE EXCEPTION 'Only organisation owners, admins, and accountants can create bank drafts';
  END IF;

  IF auth.role() IS DISTINCT FROM 'service_role'
     AND p_actor_user_id IS DISTINCT FROM auth.uid()
  THEN
    RAISE EXCEPTION 'Bank draft actor does not match the authenticated user';
  END IF;

  IF jsonb_typeof(p_items) IS DISTINCT FROM 'array'
     OR jsonb_array_length(p_items) = 0
  THEN
    RAISE EXCEPTION 'At least one bank transaction is required';
  END IF;

  SELECT count(*), count(DISTINCT value->>'line_id')
  INTO item_count, unique_line_count
  FROM jsonb_array_elements(p_items);

  IF unique_line_count <> item_count THEN
    RAISE EXCEPTION 'Each bank statement line may appear only once in a bulk draft';
  END IF;

  -- Lock every source line before validating any item. This prevents two
  -- concurrent calls from creating drafts for the same transaction.
  PERFORM line.id
  FROM public.bank_statement_lines line
  WHERE line.organisation_id = p_org_id
    AND line.id IN (
      SELECT (value->>'line_id')::UUID
      FROM jsonb_array_elements(p_items)
    )
  FOR UPDATE;

  SELECT count(*)
  INTO found_line_count
  FROM public.bank_statement_lines line
  WHERE line.organisation_id = p_org_id
    AND line.id IN (
      SELECT (value->>'line_id')::UUID
      FROM jsonb_array_elements(p_items)
    );

  IF found_line_count <> item_count THEN
    RAISE EXCEPTION 'One or more bank statement lines were not found in this organisation';
  END IF;

  SELECT nullif(trim(org.vat_number), '') IS NOT NULL
  INTO organisation_is_vat_vendor
  FROM public.organisations org
  WHERE org.id = p_org_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Organisation not found';
  END IF;

  SELECT CASE WHEN setting.tracking_enabled
      THEN setting.required_tracking_dimension_ids
      ELSE ARRAY[]::UUID[]
    END
  INTO required_dimension_ids
  FROM public.organisation_module_settings setting
  WHERE setting.organisation_id = p_org_id
    AND setting.module_key = 'bank_cash';

  required_dimension_ids := coalesce(required_dimension_ids, ARRAY[]::UUID[]);

  FOREACH required_dimension_id IN ARRAY required_dimension_ids
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM public.tracking_dimensions dimension
      WHERE dimension.id = required_dimension_id
        AND dimension.organisation_id = p_org_id
        AND dimension.active = true
    ) THEN
      RAISE EXCEPTION 'A required Bank/Cash tracking dimension is missing or inactive';
    END IF;
  END LOOP;

  FOR item IN SELECT value FROM jsonb_array_elements(p_items)
  LOOP
    line_id := (item->>'line_id')::UUID;
    SELECT *
    INTO source_line
    FROM public.bank_statement_lines line
    WHERE line.id = line_id
      AND line.organisation_id = p_org_id;

    IF source_line.posting_status <> 'unposted'
       OR source_line.gl_journal_id IS NOT NULL
    THEN
      RAISE EXCEPTION 'Bank statement line % already has draft or posted journal history', line_id;
    END IF;

    journal_total := round(abs(source_line.signed_amount), 2);
    IF journal_total = 0 THEN
      RAISE EXCEPTION 'Cannot create a draft journal for zero-value bank statement line %', line_id;
    END IF;

    IF jsonb_typeof(item->'allocations') IS DISTINCT FROM 'array'
       OR jsonb_array_length(item->'allocations') = 0
    THEN
      RAISE EXCEPTION 'Bank statement line % requires at least one allocation', line_id;
    END IF;

    SELECT account.gl_account_id
    INTO bank_gl_id
    FROM public.bank_accounts account
    WHERE account.id = source_line.bank_account_id
      AND account.organisation_id = p_org_id;

    IF bank_gl_id IS NULL THEN
      RAISE EXCEPTION 'Bank account needs a linked GL account before drafting line %', line_id;
    END IF;

    journal_id := gen_random_uuid();
    allocation_total := 0;
    vat_total := 0;
    sort_index := 0;

    INSERT INTO public.gl_journals (
      id, organisation_id, source_type, source_id, journal_date,
      description, status, total_debit, total_credit, created_by
    ) VALUES (
      journal_id, p_org_id, 'bank_transaction', line_id, source_line.line_date,
      coalesce(source_line.description, 'Bank transaction'), 'draft',
      journal_total, journal_total, p_actor_user_id
    );

    FOR allocation IN SELECT value FROM jsonb_array_elements(item->'allocations')
    LOOP
      account_id := (allocation->>'account_id')::UUID;
      gross_amount := round((allocation->>'gross_amount')::NUMERIC, 2);
      IF gross_amount <= 0 THEN
        RAISE EXCEPTION 'Every allocation for bank statement line % must be greater than zero', line_id;
      END IF;

      SELECT account.id, account.vat_treatment, account.is_system
      INTO allocation_account
      FROM public.accounts account
      WHERE account.id = account_id
        AND account.organisation_id = p_org_id;

      IF NOT FOUND THEN
        RAISE EXCEPTION 'Allocation account % does not belong to this organisation', account_id;
      END IF;
      IF coalesce(allocation_account.is_system, false) THEN
        RAISE EXCEPTION 'System account % cannot be selected as a bank allocation account', account_id;
      END IF;
      IF account_id = bank_gl_id THEN
        RAISE EXCEPTION 'The linked bank GL account cannot allocate its own transaction';
      END IF;

      effective_treatment := lower(coalesce(
        nullif(allocation->>'vat_treatment', ''),
        allocation_account.vat_treatment,
        'full'
      ));
      IF effective_treatment NOT IN ('full', 'blocked', 'exempt', 'zero_rated') THEN
        RAISE EXCEPTION 'Unsupported VAT treatment %', effective_treatment;
      END IF;

      vat_rate := coalesce(nullif(allocation->>'vat_rate', '')::NUMERIC, 15);
      IF vat_rate < 0 OR vat_rate > 100 THEN
        RAISE EXCEPTION 'VAT rate must be between 0 and 100';
      END IF;

      tracking := coalesce(allocation->'tracking', '{}'::JSONB);
      IF jsonb_typeof(tracking) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Allocation tracking must be a JSON object';
      END IF;

      FOREACH required_dimension_id IN ARRAY required_dimension_ids
      LOOP
        IF NOT tracking ? required_dimension_id::TEXT
           OR nullif(trim(tracking->>required_dimension_id::TEXT), '') IS NULL
        THEN
          RAISE EXCEPTION 'Bank/Cash posting requires tracking for dimension %', required_dimension_id;
        END IF;
        IF NOT EXISTS (
          SELECT 1
          FROM public.tracking_values tracking_value
          WHERE tracking_value.id::TEXT = tracking->>required_dimension_id::TEXT
            AND tracking_value.dimension_id = required_dimension_id
            AND tracking_value.active = true
        ) THEN
          RAISE EXCEPTION 'Tracking value for dimension % is invalid or inactive', required_dimension_id;
        END IF;
      END LOOP;

      IF organisation_is_vat_vendor
         AND effective_treatment = 'full'
         AND vat_rate > 0
      THEN
        vat_amount := round(gross_amount * vat_rate / (100 + vat_rate), 2);
      ELSE
        vat_amount := 0;
      END IF;
      net_amount := gross_amount - vat_amount;
      allocation_total := allocation_total + gross_amount;
      vat_total := vat_total + vat_amount;

      INSERT INTO public.gl_journal_lines (
        organisation_id, gl_journal_id, account_id, description,
        debit_amount, credit_amount, tracking, sort_order
      ) VALUES (
        p_org_id, journal_id, account_id,
        coalesce(source_line.description, 'Bank transaction'),
        CASE WHEN source_line.signed_amount < 0 THEN net_amount ELSE 0 END,
        CASE WHEN source_line.signed_amount > 0 THEN net_amount ELSE 0 END,
        tracking, sort_index
      );
      sort_index := sort_index + 1;
    END LOOP;

    IF abs(allocation_total - journal_total) > 0.02 THEN
      RAISE EXCEPTION
        'Allocations for bank statement line % total %, expected %',
        line_id, allocation_total, journal_total;
    END IF;

    -- The API accepts a two-cent split tolerance, but the journal itself must
    -- balance exactly. Put the residual on the largest non-VAT allocation leg.
    rounding_difference := journal_total - allocation_total;
    IF rounding_difference <> 0 THEN
      IF source_line.signed_amount < 0 THEN
        UPDATE public.gl_journal_lines line
        SET debit_amount = line.debit_amount + rounding_difference
        WHERE line.id = (
          SELECT candidate.id
          FROM public.gl_journal_lines candidate
          WHERE candidate.gl_journal_id = journal_id
          ORDER BY candidate.debit_amount DESC, candidate.sort_order
          LIMIT 1
        );
      ELSE
        UPDATE public.gl_journal_lines line
        SET credit_amount = line.credit_amount + rounding_difference
        WHERE line.id = (
          SELECT candidate.id
          FROM public.gl_journal_lines candidate
          WHERE candidate.gl_journal_id = journal_id
          ORDER BY candidate.credit_amount DESC, candidate.sort_order
          LIMIT 1
        );
      END IF;
    END IF;

    IF vat_total > 0 THEN
      SELECT account.id
      INTO vat_control_id
      FROM public.accounts account
      WHERE account.organisation_id = p_org_id
        AND account.system_key = 'vat_control'
      LIMIT 1;

      IF vat_control_id IS NULL THEN
        RAISE EXCEPTION 'VAT Control system account is required for claimable VAT';
      END IF;

      INSERT INTO public.gl_journal_lines (
        organisation_id, gl_journal_id, account_id, description,
        debit_amount, credit_amount, tracking, sort_order
      ) VALUES (
        p_org_id, journal_id, vat_control_id,
        coalesce(source_line.description, 'Bank transaction') || ' - VAT',
        CASE WHEN source_line.signed_amount < 0 THEN vat_total ELSE 0 END,
        CASE WHEN source_line.signed_amount > 0 THEN vat_total ELSE 0 END,
        '{}'::JSONB, sort_index
      );
      sort_index := sort_index + 1;
    END IF;

    INSERT INTO public.gl_journal_lines (
      organisation_id, gl_journal_id, account_id, description,
      debit_amount, credit_amount, tracking, sort_order
    ) VALUES (
      p_org_id, journal_id, bank_gl_id,
      coalesce(source_line.description, 'Bank transaction'),
      CASE WHEN source_line.signed_amount > 0 THEN journal_total ELSE 0 END,
      CASE WHEN source_line.signed_amount < 0 THEN journal_total ELSE 0 END,
      '{}'::JSONB, sort_index
    );

    IF (
      SELECT round(coalesce(sum(line.debit_amount), 0), 2)
      FROM public.gl_journal_lines line
      WHERE line.gl_journal_id = journal_id
    ) <> journal_total
    OR (
      SELECT round(coalesce(sum(line.credit_amount), 0), 2)
      FROM public.gl_journal_lines line
      WHERE line.gl_journal_id = journal_id
    ) <> journal_total
    THEN
      RAISE EXCEPTION 'Generated journal for bank statement line % is not balanced', line_id;
    END IF;

    UPDATE public.bank_statement_lines
    SET posting_status = 'draft',
        allocation_status = CASE
          WHEN jsonb_array_length(item->'allocations') > 1 THEN 'split'
          ELSE 'allocated'
        END,
        review_status = 'reviewed',
        gl_journal_id = journal_id,
        reviewed_by = p_actor_user_id,
        reviewed_at = now(),
        updated_at = now()
    WHERE id = line_id
      AND organisation_id = p_org_id;

    INSERT INTO public.bank_audit_events (
      organisation_id, bank_account_id, bank_statement_upload_id,
      bank_statement_line_id, gl_journal_id, event_type,
      actor_user_id, actor_type, details
    ) VALUES (
      p_org_id, source_line.bank_account_id, source_line.bank_statement_upload_id,
      line_id, journal_id, 'bank_journal_drafted',
      p_actor_user_id, 'user',
      jsonb_build_object(
        'bulk', true,
        'allocation_count', jsonb_array_length(item->'allocations'),
        'vat_amount', vat_total
      )
    );

    result_items := result_items || jsonb_build_array(jsonb_build_object(
      'line_id', line_id,
      'journal_id', journal_id,
      'total_debit', journal_total,
      'total_credit', journal_total,
      'lines', (
        SELECT coalesce(jsonb_agg(to_jsonb(line) ORDER BY line.sort_order), '[]'::JSONB)
        FROM public.gl_journal_lines line
        WHERE line.gl_journal_id = journal_id
      )
    ));
  END LOOP;

  RETURN jsonb_build_object(
    'created_count', jsonb_array_length(result_items),
    'items', result_items
  );
END;
$$;

REVOKE ALL ON FUNCTION public.create_bank_draft_journals_atomic(UUID, JSONB, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.create_bank_draft_journals_atomic(UUID, JSONB, UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.create_bank_draft_journals_atomic(UUID, JSONB, UUID) TO service_role;

SELECT 'bank_bulk_draft_hardening_phase_c21_applied' AS migration_note;
