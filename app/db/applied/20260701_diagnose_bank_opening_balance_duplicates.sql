-- Read-only diagnostics. Do not move to app/db/applied; this is not a migration.
-- Replace the two UUIDs below, then run in Supabase SQL editor or psql.

-- 0) If you are unsure of the IDs, find the Bank/Cash account first.
-- Adjust the name/code filters as needed, then copy organisation_id and bank_account_id
-- into the queries below.
SELECT
  ba.organisation_id,
  ba.id AS bank_account_id,
  ba.name AS bank_account_name,
  ba.account_number_mask,
  ba.gl_account_id,
  a.code AS gl_account_code,
  a.name AS gl_account_name,
  ba.active
FROM public.bank_accounts ba
LEFT JOIN public.accounts a
  ON a.organisation_id = ba.organisation_id
 AND a.id = ba.gl_account_id
WHERE ba.name ILIKE '%Consolidator%'
   OR a.code = '6200001'
ORDER BY ba.name, a.code
LIMIT 20;

-- 1) Show every posted GL line that contributes to this Bank/Cash account's TB balance.
WITH target_bank AS (
  SELECT
    ba.organisation_id,
    ba.id AS bank_account_id,
    ba.name AS bank_account_name,
    ba.gl_account_id
  FROM public.bank_accounts ba
  WHERE ba.organisation_id = '<organisation-id>'::uuid
    AND ba.id = '<bank-account-id>'::uuid
),
posted_lines AS (
  SELECT
    j.id AS journal_id,
    j.source_type,
    j.source_id,
    j.journal_date,
    j.description AS journal_description,
    j.status,
    jl.id AS journal_line_id,
    jl.description AS line_description,
    jl.debit_amount,
    jl.credit_amount,
    jl.debit_amount - jl.credit_amount AS signed_balance
  FROM target_bank tb
  JOIN public.gl_journal_lines jl
    ON jl.organisation_id = tb.organisation_id
   AND jl.account_id = tb.gl_account_id
  JOIN public.gl_journals j
    ON j.id = jl.gl_journal_id
   AND j.organisation_id = tb.organisation_id
  WHERE j.status = 'posted'
)
SELECT *
FROM posted_lines
ORDER BY journal_date, journal_id, journal_line_id;

-- 2) Show only posted opening-balance lines for this Bank/Cash account.
WITH target_bank AS (
  SELECT
    ba.organisation_id,
    ba.id AS bank_account_id,
    ba.name AS bank_account_name,
    ba.gl_account_id
  FROM public.bank_accounts ba
  WHERE ba.organisation_id = '<organisation-id>'::uuid
    AND ba.id = '<bank-account-id>'::uuid
)
SELECT
  j.id AS journal_id,
  j.journal_date,
  j.description AS journal_description,
  jl.id AS journal_line_id,
  jl.description AS line_description,
  jl.debit_amount,
  jl.credit_amount,
  jl.debit_amount - jl.credit_amount AS signed_opening_balance
FROM target_bank tb
JOIN public.gl_journal_lines jl
  ON jl.organisation_id = tb.organisation_id
 AND jl.account_id = tb.gl_account_id
JOIN public.gl_journals j
  ON j.id = jl.gl_journal_id
 AND j.organisation_id = tb.organisation_id
WHERE j.status = 'posted'
  AND j.source_type = 'opening_balance'
ORDER BY j.journal_date, j.id, jl.id;

-- 3) Totals by source type. The bank header's Current TB Balance is the posted_total.
WITH target_bank AS (
  SELECT
    ba.organisation_id,
    ba.id AS bank_account_id,
    ba.name AS bank_account_name,
    ba.gl_account_id
  FROM public.bank_accounts ba
  WHERE ba.organisation_id = '<organisation-id>'::uuid
    AND ba.id = '<bank-account-id>'::uuid
)
SELECT
  coalesce(j.source_type, 'unknown') AS source_type,
  count(*) AS line_count,
  sum(jl.debit_amount - jl.credit_amount) AS posted_total
FROM target_bank tb
JOIN public.gl_journal_lines jl
  ON jl.organisation_id = tb.organisation_id
 AND jl.account_id = tb.gl_account_id
JOIN public.gl_journals j
  ON j.id = jl.gl_journal_id
 AND j.organisation_id = tb.organisation_id
WHERE j.status = 'posted'
GROUP BY coalesce(j.source_type, 'unknown')
ORDER BY source_type;

-- 4) Show each posted opening-balance journal in full, including the retained earnings line.
-- This is the view to use before choosing which duplicate journal to reverse.
WITH target_bank AS (
  SELECT
    ba.organisation_id,
    ba.id AS bank_account_id,
    ba.name AS bank_account_name,
    ba.gl_account_id
  FROM public.bank_accounts ba
  WHERE ba.organisation_id = '<organisation-id>'::uuid
    AND ba.id = '<bank-account-id>'::uuid
),
opening_journals AS (
  SELECT DISTINCT j.id
  FROM target_bank tb
  JOIN public.gl_journal_lines jl
    ON jl.organisation_id = tb.organisation_id
   AND jl.account_id = tb.gl_account_id
  JOIN public.gl_journals j
    ON j.id = jl.gl_journal_id
   AND j.organisation_id = tb.organisation_id
  WHERE j.status = 'posted'
    AND j.source_type = 'opening_balance'
)
SELECT
  j.id AS journal_id,
  j.journal_date,
  j.description AS journal_description,
  j.total_debit,
  j.total_credit,
  jl.id AS journal_line_id,
  a.code AS account_code,
  a.name AS account_name,
  a.system_key,
  jl.description AS line_description,
  jl.debit_amount,
  jl.credit_amount,
  jl.debit_amount - jl.credit_amount AS signed_balance
FROM opening_journals oj
JOIN public.gl_journals j ON j.id = oj.id
JOIN public.gl_journal_lines jl ON jl.gl_journal_id = j.id
LEFT JOIN public.accounts a
  ON a.organisation_id = j.organisation_id
 AND a.id = jl.account_id
ORDER BY j.journal_date, j.id, jl.sort_order, jl.id;

-- 5) Repair template after reviewing query 4.
-- Choose the duplicate journal_id to exclude from reports, then uncomment and run
-- this transaction with that exact UUID. This does not delete history; it marks the
-- duplicate opening journal as reversed so Trial Balance/Bank summary no longer use it.
--
-- BEGIN;
--
-- UPDATE public.gl_journals
--    SET status = 'reversed',
--        reversed_at = now(),
--        updated_at = now()
--  WHERE id = '<duplicate-opening-journal-id>'::uuid
--    AND organisation_id = '<organisation-id>'::uuid
--    AND source_type = 'opening_balance'
--    AND status = 'posted';
--
-- -- Safety check: should now show one opening_balance line and 4489.79 total
-- -- for the bank account if the correct duplicate was reversed.
-- WITH target_bank AS (
--   SELECT organisation_id, gl_account_id
--   FROM public.bank_accounts
--   WHERE organisation_id = '<organisation-id>'::uuid
--     AND id = '<bank-account-id>'::uuid
-- )
-- SELECT
--   count(*) AS remaining_opening_line_count,
--   sum(jl.debit_amount - jl.credit_amount) AS remaining_opening_total
-- FROM target_bank tb
-- JOIN public.gl_journal_lines jl
--   ON jl.organisation_id = tb.organisation_id
--  AND jl.account_id = tb.gl_account_id
-- JOIN public.gl_journals j
--   ON j.id = jl.gl_journal_id
--  AND j.organisation_id = tb.organisation_id
-- WHERE j.status = 'posted'
--   AND j.source_type = 'opening_balance';
--
-- COMMIT;
