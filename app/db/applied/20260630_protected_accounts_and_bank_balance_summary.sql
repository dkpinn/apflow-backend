-- Review and apply with Supabase CLI. Do not move to app/db/applied until pushed.

DROP FUNCTION IF EXISTS public.get_bank_account_balance_summary(uuid, uuid);

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
  upload_movement numeric := 0;
  all_imported_movement numeric := 0;
  tb_balance numeric;
BEGIN
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
   ORDER BY coalesce(u.statement_period_to, l.latest_line_date) DESC NULLS LAST,
            l.latest_line_date DESC NULLS LAST,
            u.uploaded_at DESC NULLS LAST,
            u.id DESC
   LIMIT 1;

  IF latest_upload.id IS NOT NULL THEN
    SELECT max(line_date), coalesce(sum(signed_amount), 0)
      INTO upload_latest_line_date, upload_movement
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
  END IF;

  bank_statement_balance :=
    coalesce(latest_upload.closing_balance, account_row.current_reconciled_balance, account_row.opening_balance, 0);

  calculated_imported_balance :=
    CASE
      WHEN latest_upload.id IS NOT NULL AND latest_upload.opening_balance IS NOT NULL
        THEN latest_upload.opening_balance + upload_movement
      ELSE coalesce(account_row.opening_balance, 0) + all_imported_movement
    END;

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

-- Diagnostic: user/manual-style journal lines posted to protected accounts.
-- SELECT j.id AS journal_id,
--        j.source_type,
--        j.journal_date,
--        jl.id AS journal_line_id,
--        a.id AS account_id,
--        a.code,
--        a.name,
--        CASE
--          WHEN a.is_system THEN 'system account'
--          WHEN a.system_key IS NOT NULL THEN 'system account'
--          WHEN ba.id IS NOT NULL THEN 'bank/cash control account'
--          ELSE 'module-controlled account'
--        END AS protected_reason,
--        jl.debit_amount,
--        jl.credit_amount
--   FROM public.gl_journal_lines jl
--   JOIN public.gl_journals j ON j.id = jl.gl_journal_id
--   JOIN public.accounts a ON a.id = jl.account_id
--   LEFT JOIN public.bank_accounts ba
--     ON ba.organisation_id = jl.organisation_id
--    AND ba.gl_account_id = jl.account_id
--    AND coalesce(ba.active, true)
--  WHERE jl.organisation_id = '<organisation-id>'::uuid
--    AND j.source_type IN ('manual', 'manual_journal', 'opening_balance', 'bank_transaction')
--    AND (a.is_system OR a.system_key IS NOT NULL OR ba.id IS NOT NULL)
--  ORDER BY j.journal_date, j.id, jl.sort_order;

-- Diagnostic: bank module opening balances that do not match the posted GL opening balance.
-- SELECT ba.id AS bank_account_id,
--        ba.name,
--        ba.gl_account_id,
--        ba.opening_balance AS module_opening_balance,
--        coalesce(sum(jl.debit_amount - jl.credit_amount), 0) AS gl_opening_balance
--   FROM public.bank_accounts ba
--   LEFT JOIN public.gl_journals j
--     ON j.organisation_id = ba.organisation_id
--    AND j.source_type = 'opening_balance'
--    AND j.status = 'posted'
--   LEFT JOIN public.gl_journal_lines jl
--     ON jl.gl_journal_id = j.id
--    AND jl.account_id = ba.gl_account_id
--  WHERE ba.organisation_id = '<organisation-id>'::uuid
--    AND ba.gl_account_id IS NOT NULL
--  GROUP BY ba.id, ba.name, ba.gl_account_id, ba.opening_balance
-- HAVING coalesce(ba.opening_balance, 0) <> coalesce(sum(jl.debit_amount - jl.credit_amount), 0)
--  ORDER BY ba.name;
