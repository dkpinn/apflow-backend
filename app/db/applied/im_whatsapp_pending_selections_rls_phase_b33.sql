-- ============================================================
-- WhatsApp Pending Selections — RLS Hardening Phase B33
-- Idempotent upgrade for databases where B28 ran before RLS was
-- added at creation time.  Fresh installs have these controls
-- applied by B28 already; the ALTER TABLE statements are safe to
-- repeat and the DROP/CREATE ensures policies are current.
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
