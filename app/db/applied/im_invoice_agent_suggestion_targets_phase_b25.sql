-- ============================================================
-- APPayPal Invoice Agent Suggestion Targets Phase B25
-- Adds UI focus target metadata for agent findings.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

alter table public.invoice_agent_suggestions
  add column if not exists target jsonb;

select 'invoice_agent_suggestion_targets_phase_b25_applied' as migration_note;
