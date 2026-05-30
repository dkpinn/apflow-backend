-- ============================================================
-- WhatsApp Pending Selections — RLS Hardening Phase B33
-- Enables Row Level Security and adds explicit deny-by-default
-- policies for authenticated and anon roles.
-- The backend accesses this table exclusively via service_role,
-- which bypasses RLS automatically — no permissive policies needed.
-- ============================================================

alter table public.whatsapp_pending_selections enable row level security;

-- Force RLS even for the table owner so no role can accidentally
-- bypass these controls without the service_role privilege.
alter table public.whatsapp_pending_selections force row level security;

-- Deny all operations for authenticated users.
create policy "whatsapp_pending_selections: deny authenticated"
  on public.whatsapp_pending_selections
  as restrictive
  to authenticated
  using (false)
  with check (false);

-- Deny all operations for anonymous users.
create policy "whatsapp_pending_selections: deny anon"
  on public.whatsapp_pending_selections
  as restrictive
  to anon
  using (false)
  with check (false);

select 'im_whatsapp_pending_selections_rls_phase_b33_applied' as migration_note;
