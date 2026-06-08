-- ============================================================
-- WhatsApp Pending Selections - RLS Hardening Phase B33
-- Existing remote migration fetched into local history.
-- ============================================================

alter table public.whatsapp_pending_selections enable row level security;
alter table public.whatsapp_pending_selections force row level security;

drop policy if exists "whatsapp_pending_selections: deny authenticated"
  on public.whatsapp_pending_selections;
create policy "whatsapp_pending_selections: deny authenticated"
  on public.whatsapp_pending_selections
  as restrictive
  to authenticated
  using (false)
  with check (false);

drop policy if exists "whatsapp_pending_selections: deny anon"
  on public.whatsapp_pending_selections;
create policy "whatsapp_pending_selections: deny anon"
  on public.whatsapp_pending_selections
  as restrictive
  to anon
  using (false)
  with check (false);

select 'im_whatsapp_pending_selections_rls_phase_b33_applied' as migration_note;
