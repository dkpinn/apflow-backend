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

-- No RLS: this table is managed entirely by the backend service role.
-- It contains no sensitive user data beyond the phone number and the
-- Meta media_id (which expires shortly after being issued by Meta).

select 'im_whatsapp_pending_selections_phase_b28_applied' as migration_note;
