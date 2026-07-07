-- Review and apply with Supabase CLI. Do not move to app/db/applied until pushed.

CREATE OR REPLACE FUNCTION public.get_bank_account_balance_summary(
  p_org_id uuid,
  p_bank_account_id uuid
)
RETURNS TABLE (
  bank_statement_balance numeric,
  calculated_imported_balance numeric,
  current_tb_balance numeric,
  latest_statement_upload_id uuid,
  statement_period_to date,
  latest_transaction_date date,
  bank_balance_status text,
  imported_balance_status text,
  tb_balance_status text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  account_row public.bank_accounts%ROWTYPE;
  latest_upload public.bank_statement_uploads%ROWTYPE;
  upload_latest_line_date date;
  all_imported_movement numeric := 0;
  tb_balance numeric;
  coa_opening_balance numeric := 0;
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
    RAISE EXCEPTION 'Bank account % was not found for organisation %', p_bank_account_id, p_org_id;
  END IF;

  SELECT u.*
    INTO latest_upload
    FROM public.bank_statement_uploads u
    LEFT JOIN LATERAL (
      SELECT max(line_date) AS latest_line_date
        FROM public.bank_statement_lines l
       WHERE l.organisation_id = p_org_id
         AND l.bank_account_id = p_bank_account_id
         AND l.bank_statement_upload_id = u.id
    ) l ON true
   WHERE u.organisation_id = p_org_id
     AND u.bank_account_id = p_bank_account_id
     AND u.extraction_status = 'extracted'
     AND u.closing_balance IS NOT NULL
   ORDER BY coalesce(u.statement_period_to, l.latest_line_date) DESC NULLS LAST,
            l.latest_line_date DESC NULLS LAST,
            u.uploaded_at DESC NULLS LAST,
            u.id DESC
   LIMIT 1;

  IF latest_upload.id IS NOT NULL THEN
    SELECT max(line_date)
      INTO upload_latest_line_date
      FROM public.bank_statement_lines
     WHERE organisation_id = p_org_id
       AND bank_account_id = p_bank_account_id
       AND bank_statement_upload_id = latest_upload.id;
  END IF;

  SELECT coalesce(sum(signed_amount), 0)
    INTO all_imported_movement
    FROM public.bank_statement_lines
   WHERE organisation_id = p_org_id
     AND bank_account_id = p_bank_account_id;

  IF account_row.gl_account_id IS NOT NULL THEN
    SELECT coalesce(sum(jl.debit_amount - jl.credit_amount), 0)
      INTO tb_balance
      FROM public.gl_journal_lines jl
      JOIN public.gl_journals j ON j.id = jl.gl_journal_id
     WHERE jl.organisation_id = p_org_id
       AND jl.account_id = account_row.gl_account_id
       AND j.organisation_id = p_org_id
       AND j.status = 'posted';

    SELECT coalesce(sum(jl.debit_amount - jl.credit_amount), 0)
      INTO coa_opening_balance
      FROM public.gl_journal_lines jl
      JOIN public.gl_journals j ON j.id = jl.gl_journal_id
     WHERE jl.organisation_id = p_org_id
       AND jl.account_id = account_row.gl_account_id
       AND j.organisation_id = p_org_id
       AND j.status = 'posted'
       AND j.source_type = 'opening_balance';
  END IF;

  bank_statement_balance := coalesce(latest_upload.closing_balance, coa_opening_balance);

  calculated_imported_balance :=
    coa_opening_balance + all_imported_movement;

  current_tb_balance := tb_balance;
  latest_statement_upload_id := latest_upload.id;
  statement_period_to := latest_upload.statement_period_to;
  latest_transaction_date := upload_latest_line_date;
  bank_balance_status := 'available';
  imported_balance_status := 'available';
  tb_balance_status := CASE WHEN account_row.gl_account_id IS NULL THEN 'gl_account_not_linked' ELSE 'available' END;

  RETURN NEXT;
END;
$$;

REVOKE ALL ON FUNCTION public.get_bank_account_balance_summary(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_bank_account_balance_summary(uuid, uuid)
  TO authenticated, service_role;
