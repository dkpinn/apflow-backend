-- Consolidate duplicate opening-balance journals (org-wide).
--
-- Read-only diagnostics + a GUARDED repair. This is not an automatic migration —
-- review the diagnostic output first, then run the repair transaction.
--
-- Why this exists: the Chart of Accounts opening-balance editor used to default its
-- date to *today* and key the journal by that date. Editing on a day other than the
-- original conversion date posted a SECOND opening-balance journal (containing only
-- the edited account + retained earnings), so the edited account was counted in BOTH
-- journals and the Trial Balance / bank "Current TB Balance" doubled. The code fix
-- (opening balances are now a single per-org journal) stops NEW duplicates; this
-- script cleans up any that already exist.
--
-- Repair strategy: keep the EARLIEST active opening-balance journal per org (the
-- original, complete conversion-date journal) and reverse the later partial ones.
-- After running, re-enter any opening balances that had genuinely changed via the
-- editor — it now updates the single remaining journal in place.

-- 1) Which organisations have more than one active opening-balance journal?
SELECT
  j.organisation_id,
  count(*)                              AS active_opening_journal_count,
  array_agg(j.journal_date ORDER BY j.journal_date) AS dates,
  array_agg(j.id ORDER BY j.journal_date)           AS journal_ids
FROM public.gl_journals j
WHERE j.source_type = 'opening_balance'
  AND j.status <> 'reversed'
GROUP BY j.organisation_id
HAVING count(*) > 1
ORDER BY active_opening_journal_count DESC;

-- 2) Line-level detail for those journals, so you can see what each one contains
--    before repairing (the earliest is the one that will be kept).
WITH dup_orgs AS (
  SELECT j.organisation_id
  FROM public.gl_journals j
  WHERE j.source_type = 'opening_balance'
    AND j.status <> 'reversed'
  GROUP BY j.organisation_id
  HAVING count(*) > 1
)
SELECT
  j.organisation_id,
  j.id                AS journal_id,
  j.journal_date,
  j.status,
  j.total_debit,
  j.total_credit,
  a.code              AS account_code,
  a.name              AS account_name,
  a.system_key,
  jl.debit_amount,
  jl.credit_amount
FROM public.gl_journals j
JOIN dup_orgs d              ON d.organisation_id = j.organisation_id
JOIN public.gl_journal_lines jl ON jl.gl_journal_id = j.id
LEFT JOIN public.accounts a ON a.organisation_id = j.organisation_id AND a.id = jl.account_id
WHERE j.source_type = 'opening_balance'
  AND j.status <> 'reversed'
ORDER BY j.organisation_id, j.journal_date, j.id, jl.sort_order;

-- 3) Dry run: exactly which journals the repair below WOULD reverse
--    (everything except the earliest active opening-balance journal per org).
WITH ranked AS (
  SELECT
    j.id,
    j.organisation_id,
    j.journal_date,
    row_number() OVER (
      PARTITION BY j.organisation_id
      ORDER BY j.journal_date, j.created_at, j.id
    ) AS rn
  FROM public.gl_journals j
  WHERE j.source_type = 'opening_balance'
    AND j.status <> 'reversed'
)
SELECT id AS journal_to_reverse, organisation_id, journal_date
FROM ranked
WHERE rn > 1
ORDER BY organisation_id, journal_date;

-- 4) Repair. Reverses every active opening-balance journal EXCEPT the earliest per
--    org. History is preserved (status = 'reversed'), so Trial Balance / bank
--    summary stop counting the duplicates. Review queries 1-3 first, then run:
--
-- BEGIN;
--
-- WITH ranked AS (
--   SELECT
--     j.id,
--     row_number() OVER (
--       PARTITION BY j.organisation_id
--       ORDER BY j.journal_date, j.created_at, j.id
--     ) AS rn
--   FROM public.gl_journals j
--   WHERE j.source_type = 'opening_balance'
--     AND j.status <> 'reversed'
-- )
-- UPDATE public.gl_journals j
--    SET status = 'reversed',
--        reversed_at = now(),
--        updated_at = now()
--   FROM ranked r
--  WHERE r.id = j.id
--    AND r.rn > 1;
--
-- -- Verify: every org should now have exactly one active opening-balance journal.
-- SELECT organisation_id, count(*) AS active_opening_journal_count
-- FROM public.gl_journals
-- WHERE source_type = 'opening_balance' AND status <> 'reversed'
-- GROUP BY organisation_id
-- ORDER BY active_opening_journal_count DESC;
--
-- COMMIT;
--
-- After committing: open Settings → Chart of Accounts and re-enter any opening
-- balances that had genuinely changed since the original conversion date. The
-- editor now updates the single remaining journal in place (no new duplicates).
