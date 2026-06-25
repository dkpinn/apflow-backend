-- Platform-owner-only table for overriding the core VLM extraction prompt.
-- If the table is empty the backend falls back to the hardcoded default.
-- Only ever has one active row (UPSERT pattern).

create table if not exists public.bank_vlm_prompt_config (
  id          uuid primary key default gen_random_uuid(),
  prompt_text text not null,
  notes       text,
  updated_by  uuid references auth.users(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

alter table public.bank_vlm_prompt_config enable row level security;

drop policy if exists "bank_vlm_prompt_config_platform_admin" on public.bank_vlm_prompt_config;
create policy "bank_vlm_prompt_config_platform_admin" on public.bank_vlm_prompt_config
  for all to authenticated
  using  (public.is_platform_admin())
  with check (public.is_platform_admin());
