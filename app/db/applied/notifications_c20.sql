-- ============================================================
-- Phase C20 — In-app notification centre
-- ============================================================

create table if not exists public.notifications (
  id           uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  type         text not null default 'info'
               check (type in ('review_queue','recurring_draft','approval','payment_due','info','warning')),
  title        text not null,
  body         text,
  link         text,
  source_key   text,   -- dedup handle: one notification per (org, source_key)
  read         boolean not null default false,
  read_at      timestamptz,
  created_at   timestamptz not null default now()
);

-- Prevent duplicate notifications for the same (org, source_key) pair
create unique index if not exists notifications_org_source_key_uidx
  on public.notifications(organisation_id, source_key)
  where source_key is not null;

create index if not exists notifications_org_unread_idx
  on public.notifications(organisation_id, read, created_at desc);

alter table public.notifications enable row level security;

drop policy if exists "notifications_select_member" on public.notifications;
create policy "notifications_select_member" on public.notifications
  for select to authenticated using (public.is_org_member(organisation_id));

drop policy if exists "notifications_write_accountant" on public.notifications;
create policy "notifications_write_accountant" on public.notifications
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));
