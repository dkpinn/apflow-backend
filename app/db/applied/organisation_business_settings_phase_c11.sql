create table if not exists public.organisation_module_settings (
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  module_key text not null check (
    module_key in ('supplier', 'customer', 'inventory', 'bank_cash', 'asset', 'liability', 'project')
  ),
  tracking_enabled boolean not null default false,
  required_tracking_dimension_ids uuid[] not null default '{}'::uuid[],
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (organisation_id, module_key),
  check (tracking_enabled or cardinality(required_tracking_dimension_ids) = 0),
  check (not tracking_enabled or cardinality(required_tracking_dimension_ids) > 0)
);

create table if not exists public.organisation_invoice_branding (
  organisation_id uuid primary key references public.organisations(id) on delete cascade,
  logo_storage_path text,
  primary_color text not null default '#174EA6' check (primary_color ~ '^#[0-9A-Fa-f]{6}$'),
  accent_color text not null default '#E8EEF9' check (accent_color ~ '^#[0-9A-Fa-f]{6}$'),
  text_color text not null default '#111827' check (text_color ~ '^#[0-9A-Fa-f]{6}$'),
  font_family text not null default 'inter' check (
    font_family in ('inter', 'arial', 'georgia', 'times_new_roman', 'roboto_mono')
  ),
  terms_and_conditions text not null default '',
  bank_name text not null default '',
  account_holder text not null default '',
  account_number text not null default '',
  account_type text not null default '',
  branch_code text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

insert into storage.buckets (
  id,
  name,
  public,
  file_size_limit,
  allowed_mime_types
)
values (
  'organisation-branding',
  'organisation-branding',
  false,
  2097152,
  array['image/png', 'image/jpeg', 'image/webp']
)
on conflict (id) do update
set
  name = excluded.name,
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

alter table public.organisation_module_settings enable row level security;
alter table public.organisation_invoice_branding enable row level security;

drop policy if exists "organisation_module_settings_select_member" on public.organisation_module_settings;
create policy "organisation_module_settings_select_member"
  on public.organisation_module_settings for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "organisation_module_settings_write_admin" on public.organisation_module_settings;
create policy "organisation_module_settings_write_admin"
  on public.organisation_module_settings for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "organisation_invoice_branding_select_member" on public.organisation_invoice_branding;
create policy "organisation_invoice_branding_select_member"
  on public.organisation_invoice_branding for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "organisation_invoice_branding_write_admin" on public.organisation_invoice_branding;
create policy "organisation_invoice_branding_write_admin"
  on public.organisation_invoice_branding for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "organisation_branding_select_member" on storage.objects;
create policy "organisation_branding_select_member"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'organisation-branding'
    and public.is_org_member(public.storage_object_org_id(name))
  );

drop policy if exists "organisation_branding_insert_admin" on storage.objects;
create policy "organisation_branding_insert_admin"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'organisation-branding'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop policy if exists "organisation_branding_update_admin" on storage.objects;
create policy "organisation_branding_update_admin"
  on storage.objects for update to authenticated
  using (
    bucket_id = 'organisation-branding'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  )
  with check (
    bucket_id = 'organisation-branding'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop policy if exists "organisation_branding_delete_admin" on storage.objects;
create policy "organisation_branding_delete_admin"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'organisation-branding'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop trigger if exists organisation_module_settings_set_updated_at on public.organisation_module_settings;
create trigger organisation_module_settings_set_updated_at
  before update on public.organisation_module_settings
  for each row execute function public.set_updated_at();

drop trigger if exists organisation_invoice_branding_set_updated_at on public.organisation_invoice_branding;
create trigger organisation_invoice_branding_set_updated_at
  before update on public.organisation_invoice_branding
  for each row execute function public.set_updated_at();
