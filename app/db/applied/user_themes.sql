-- ============================================================
-- User Themes
-- Stores purchased UI themes and each user's active cosmetic
-- preference. Theme tokens are presentation-only and must not be
-- used by calculation, parsing, reconciliation, or supplier logic.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create table if not exists public.themes (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  name text not null,
  description text,
  preview_image_url text,
  tokens jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.user_theme_entitlements (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  theme_id uuid not null references public.themes(id) on delete cascade,
  source text not null default 'store_purchase',
  created_at timestamptz not null default now(),
  unique (user_id, theme_id)
);

create table if not exists public.user_theme_preferences (
  user_id uuid primary key references auth.users(id) on delete cascade,
  active_theme_id uuid references public.themes(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists user_theme_entitlements_user_idx
  on public.user_theme_entitlements(user_id);

create index if not exists user_theme_entitlements_theme_idx
  on public.user_theme_entitlements(theme_id);

create index if not exists user_theme_preferences_active_theme_idx
  on public.user_theme_preferences(active_theme_id);

create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists themes_set_updated_at on public.themes;
create trigger themes_set_updated_at
  before update on public.themes
  for each row execute function public.set_updated_at();

drop trigger if exists user_theme_preferences_set_updated_at on public.user_theme_preferences;
create trigger user_theme_preferences_set_updated_at
  before update on public.user_theme_preferences
  for each row execute function public.set_updated_at();

alter table public.themes enable row level security;
alter table public.user_theme_entitlements enable row level security;
alter table public.user_theme_preferences enable row level security;

drop policy if exists "themes_select_purchased_active" on public.themes;
create policy "themes_select_purchased_active"
  on public.themes for select to authenticated
  using (
    is_active = true
    and exists (
      select 1
      from public.user_theme_entitlements ute
      where ute.theme_id = themes.id
        and ute.user_id = auth.uid()
    )
  );

drop policy if exists "user_theme_entitlements_select_own" on public.user_theme_entitlements;
create policy "user_theme_entitlements_select_own"
  on public.user_theme_entitlements for select to authenticated
  using (user_id = auth.uid());

drop policy if exists "user_theme_preferences_select_own" on public.user_theme_preferences;
create policy "user_theme_preferences_select_own"
  on public.user_theme_preferences for select to authenticated
  using (user_id = auth.uid());

drop policy if exists "user_theme_preferences_insert_own_entitled" on public.user_theme_preferences;
create policy "user_theme_preferences_insert_own_entitled"
  on public.user_theme_preferences for insert to authenticated
  with check (
    user_id = auth.uid()
    and (
      active_theme_id is null
      or exists (
        select 1
        from public.user_theme_entitlements ute
        join public.themes t on t.id = ute.theme_id
        where ute.user_id = auth.uid()
          and ute.theme_id = active_theme_id
          and t.is_active = true
      )
    )
  );

drop policy if exists "user_theme_preferences_update_own_entitled" on public.user_theme_preferences;
create policy "user_theme_preferences_update_own_entitled"
  on public.user_theme_preferences for update to authenticated
  using (user_id = auth.uid())
  with check (
    user_id = auth.uid()
    and (
      active_theme_id is null
      or exists (
        select 1
        from public.user_theme_entitlements ute
        join public.themes t on t.id = ute.theme_id
        where ute.user_id = auth.uid()
          and ute.theme_id = active_theme_id
          and t.is_active = true
      )
    )
  );
