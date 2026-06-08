-- Phase C14: Align atomic duplicate protection with the live invoice schema.
-- Apply in Supabase SQL editor, then move this file to app/db/applied/.

CREATE OR REPLACE FUNCTION public.post_invoice_to_gl_atomic(
  p_org_id UUID,
  p_invoice_id UUID,
  p_user_id UUID,
  p_journal_date DATE,
  p_description TEXT,
  p_total NUMERIC,
  p_lines JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  invoice_row public.invoices_extracted%ROWTYPE;
  journal_id UUID := gen_random_uuid();
  line_count INTEGER;
  debit_total NUMERIC(14,2);
  credit_total NUMERIC(14,2);
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    IF auth.uid() IS NULL
       OR p_user_id IS DISTINCT FROM auth.uid()
       OR NOT public.has_org_role(
         p_org_id,
         ARRAY['owner','admin','accountant']::public.organisation_role[]
       )
    THEN
      RAISE EXCEPTION 'Not authorised to post this invoice';
    END IF;
  END IF;

  SELECT *
  INTO invoice_row
  FROM public.invoices_extracted
  WHERE id = p_invoice_id
    AND organisation_id = p_org_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Invoice not found';
  END IF;
  IF invoice_row.posting_status = 'posted' THEN
    RAISE EXCEPTION 'Invoice has already been posted to GL';
  END IF;
  IF invoice_row.supplier_id IS NOT NULL
     AND nullif(btrim(invoice_row.invoice_number), '') IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM public.invoices_extracted duplicate_invoice
       WHERE duplicate_invoice.organisation_id = p_org_id
         AND duplicate_invoice.supplier_id = invoice_row.supplier_id
         AND duplicate_invoice.invoice_number = invoice_row.invoice_number
         AND duplicate_invoice.id <> p_invoice_id
     )
  THEN
    RAISE EXCEPTION 'Duplicate invoices cannot be posted to GL';
  END IF;
  IF p_total IS NULL OR p_total <= 0 THEN
    RAISE EXCEPTION 'Invoice total must be positive';
  END IF;
  IF abs(
    round(coalesce(invoice_row.subtotal, 0) + coalesce(invoice_row.tax_amount, 0), 2)
    - round(p_total, 2)
  ) > 0.02 THEN
    RAISE EXCEPTION 'Invoice totals changed after journal preparation';
  END IF;
  IF jsonb_typeof(p_lines) <> 'array' OR jsonb_array_length(p_lines) < 2 THEN
    RAISE EXCEPTION 'A balanced journal requires at least two lines';
  END IF;

  SELECT
    count(*),
    round(coalesce(sum(debit_amount), 0), 2),
    round(coalesce(sum(credit_amount), 0), 2)
  INTO line_count, debit_total, credit_total
  FROM jsonb_to_recordset(p_lines) AS line(
    account_id UUID,
    description TEXT,
    debit_amount NUMERIC,
    credit_amount NUMERIC,
    tracking JSONB,
    sort_order INTEGER
  );

  IF debit_total <> credit_total OR debit_total <> round(p_total, 2) THEN
    RAISE EXCEPTION 'Journal lines do not balance to the invoice total';
  END IF;

  INSERT INTO public.gl_journals (
    id,
    organisation_id,
    source_type,
    source_id,
    journal_date,
    description,
    status,
    total_debit,
    total_credit,
    created_by,
    posted_by,
    posted_at
  )
  VALUES (
    journal_id,
    p_org_id,
    'invoice',
    p_invoice_id,
    coalesce(p_journal_date, current_date),
    p_description,
    'posted',
    debit_total,
    credit_total,
    p_user_id,
    p_user_id,
    now()
  );

  INSERT INTO public.gl_journal_lines (
    organisation_id,
    gl_journal_id,
    account_id,
    description,
    debit_amount,
    credit_amount,
    tracking,
    sort_order
  )
  SELECT
    p_org_id,
    journal_id,
    line.account_id,
    line.description,
    coalesce(line.debit_amount, 0),
    coalesce(line.credit_amount, 0),
    coalesce(line.tracking, '{}'::JSONB),
    coalesce(line.sort_order, 0)
  FROM jsonb_to_recordset(p_lines) AS line(
    account_id UUID,
    description TEXT,
    debit_amount NUMERIC,
    credit_amount NUMERIC,
    tracking JSONB,
    sort_order INTEGER
  );

  UPDATE public.invoices_extracted
  SET
    gl_journal_id = journal_id,
    posting_status = 'posted',
    posted_at = now(),
    posted_by = p_user_id,
    approval_status = 'approved',
    review_status = 'approved',
    approved_at = now(),
    approved_by = p_user_id,
    updated_at = now()
  WHERE id = p_invoice_id
    AND organisation_id = p_org_id;

  RETURN jsonb_build_object(
    'journal_id', journal_id,
    'total_debit', debit_total,
    'total_credit', credit_total,
    'lines', line_count
  );
END;
$$;

REVOKE ALL ON FUNCTION public.post_invoice_to_gl_atomic(
  UUID, UUID, UUID, DATE, TEXT, NUMERIC, JSONB
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.post_invoice_to_gl_atomic(
  UUID, UUID, UUID, DATE, TEXT, NUMERIC, JSONB
) TO authenticated, service_role;

SELECT 'stp_auto_post_duplicate_guard_phase_c14_applied' AS migration_note;
