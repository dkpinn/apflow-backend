-- ============================================================
-- WhatsApp Pending Org Selections — Phase B28
-- Stores in-progress "which organisation?" conversations for
-- senders who belong to more than one organisation.
-- A pending row expires after 10 minutes; the backend clears it
-- once the user replies with their selection.
-- ============================================================

create table if not exists public.whatsapp_pending_selections (
  id          uuid        primary key default gen_random_uuid(),
  phone       text        not null,                              -- normalised E.164, e.g. +27821234567
  options     jsonb       not null,                              -- [{"org_id":"...","org_name":"..."}]
  media_id    text        not null,                              -- Meta media_id to download on selection
  mime_type   text        not null,                              -- e.g. image/jpeg, application/pdf
  filename    text,                                              -- original filename (documents only)
  uploaded_by uuid        references auth.users(id) on delete set null,
  expires_at  timestamptz not null default now() + interval '10 minutes',
  created_at  timestamptz not null default now()
);

-- One active pending selection per sender phone at a time
-- (UPSERT on phone replaces any previous pending for that number)
create unique index if not exists whatsapp_pending_phone_idx
  on public.whatsapp_pending_selections(phone);

create index if not exists whatsapp_pending_expires_idx
  on public.whatsapp_pending_selections(expires_at);

-- Enable RLS immediately so no window exists between creation and hardening.
-- The backend accesses this table exclusively via service_role, which bypasses
-- RLS automatically — no permissive policies are needed.
alter table public.whatsapp_pending_selections enable row level security;
alter table public.whatsapp_pending_selections force row level security;

create policy "whatsapp_pending_selections: deny authenticated"
  on public.whatsapp_pending_selections
  as restrictive
  to authenticated
  using (false)
  with check (false);

create policy "whatsapp_pending_selections: deny anon"
  on public.whatsapp_pending_selections
  as restrictive
  to anon
  using (false)
  with check (false);

select 'im_whatsapp_pending_selections_phase_b28_applied' as migration_note;
