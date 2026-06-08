-- group_consolidation_rls_phase_2.sql
-- ------------------------------------
-- Tighten exchange_rates SELECT policy to remove the global read of
-- reporting_group_id IS NULL rows.
--
-- Background
-- ----------
-- The original exchange_rates_select policy (group_consolidation_phase_1.sql)
-- contained:
--
--   using (reporting_group_id is null
--          or public.can_read_reporting_group(reporting_group_id))
--
-- The "reporting_group_id IS NULL" arm was intended for future "global/shared"
-- exchange rates not tied to any reporting group. In practice:
--   - No application code creates NULL-group rows (every insert sets
--     reporting_group_id from the URL parameter).
--   - No application code queries NULL-group rows (the trial-balance query
--     always filters on a specific reporting_group_id).
--
-- Allowing IS NULL therefore lets any authenticated user read rows they have
-- no business relationship to.  The write policy already guards correctly:
--
--   using (reporting_group_id is not null
--          and public.can_write_reporting_group(reporting_group_id))
--
-- This migration makes the SELECT policy consistent with the write policy and
-- with the consolidation_periods_select pattern.

drop policy if exists "exchange_rates_select" on public.exchange_rates;
create policy "exchange_rates_select"
  on public.exchange_rates for select to authenticated
  using (
    reporting_group_id is not null
    and public.can_read_reporting_group(reporting_group_id)
  );
