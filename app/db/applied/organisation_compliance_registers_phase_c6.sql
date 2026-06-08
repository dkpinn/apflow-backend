-- ============================================================
-- Organisation Compliance Registers - Phase C6
-- Directors, shareholder/share register, beneficial ownership,
-- certified ID document storage, and RLS.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create table if not exists public.organisation_compliance_parties (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  party_type text not null default 'natural_person'
    check (party_type in ('natural_person', 'legal_entity')),
  status text not null default 'active'
    check (status in ('active', 'inactive', 'archived')),

  -- Natural person / SARS individual / CIPC director fields
  first_name text,
  surname text,
  other_names text,
  initials text,
  former_names text,
  date_of_birth date,
  id_number text,
  passport_number text,
  passport_country text,
  passport_issue_date date,
  nationality text,
  tax_registered_sa boolean,
  tax_reference_number text,
  tax_reference_unavailable_reason text,
  cell_number text,
  email text,
  service_address text,

  -- Legal entity / SARS other entity fields
  nature_of_business text,
  registered_name text,
  trading_name text,
  country_of_registration text,
  registration_number text,
  financial_year_end date,
  contact_initials text,
  contact_surname text,
  contact_cell text,
  contact_email text,

  created_by uuid references auth.users(id) on delete set null default auth.uid(),
  updated_by uuid references auth.users(id) on delete set null default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists org_compliance_parties_org_idx
  on public.organisation_compliance_parties(organisation_id, party_type, status);

create table if not exists public.organisation_directorships (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  party_id uuid not null references public.organisation_compliance_parties(id) on delete cascade,
  status text not null default 'active'
    check (status in ('active', 'resigned', 'removed', 'deceased', 'inactive')),
  appointment_date date,
  resignation_date date,
  occupation text,
  other_company_directorships text,
  professional_qualifications text,
  experience text,
  notes text,
  created_by uuid references auth.users(id) on delete set null default auth.uid(),
  updated_by uuid references auth.users(id) on delete set null default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists org_directorships_org_idx
  on public.organisation_directorships(organisation_id, status);

create table if not exists public.organisation_share_classes (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  name text not null,
  description text,
  authorised_shares numeric(20, 2),
  status text not null default 'active'
    check (status in ('active', 'inactive', 'archived')),
  created_by uuid references auth.users(id) on delete set null default auth.uid(),
  updated_by uuid references auth.users(id) on delete set null default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organisation_id, name)
);

create index if not exists org_share_classes_org_idx
  on public.organisation_share_classes(organisation_id, status);

create table if not exists public.organisation_share_transactions (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  share_class_id uuid not null references public.organisation_share_classes(id) on delete restrict,
  transaction_type text not null
    check (transaction_type in ('issue', 'transfer', 'cancel', 'reissue', 'adjustment')),
  transaction_date date not null default current_date,
  effective_date date,
  certificate_number text,
  from_party_id uuid references public.organisation_compliance_parties(id) on delete restrict,
  to_party_id uuid references public.organisation_compliance_parties(id) on delete restrict,
  number_of_shares numeric(20, 2) not null check (number_of_shares > 0),
  reason text,
  notes text,
  created_by uuid references auth.users(id) on delete set null default auth.uid(),
  created_at timestamptz not null default now()
);

create index if not exists org_share_transactions_org_idx
  on public.organisation_share_transactions(organisation_id, transaction_date desc, created_at desc);
create index if not exists org_share_transactions_class_idx
  on public.organisation_share_transactions(share_class_id);
create index if not exists org_share_transactions_from_party_idx
  on public.organisation_share_transactions(from_party_id);
create index if not exists org_share_transactions_to_party_idx
  on public.organisation_share_transactions(to_party_id);

create table if not exists public.organisation_beneficial_ownership_entries (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  party_id uuid not null references public.organisation_compliance_parties(id) on delete cascade,
  status text not null default 'active'
    check (status in ('active', 'inactive', 'archived')),
  ownership_mode text not null default 'direct'
    check (ownership_mode in ('direct', 'indirect')),
  reason_for_ownership text not null,
  ownership_percentage numeric(7, 4),
  control_extent text,
  chain_notes text,
  effective_from date,
  effective_to date,
  created_by uuid references auth.users(id) on delete set null default auth.uid(),
  updated_by uuid references auth.users(id) on delete set null default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists org_bo_entries_org_idx
  on public.organisation_beneficial_ownership_entries(organisation_id, status);

create table if not exists public.organisation_compliance_documents (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  party_id uuid references public.organisation_compliance_parties(id) on delete cascade,
  document_type text not null
    check (document_type in (
      'certified_id',
      'certified_passport',
      'mandate',
      'resolution',
      'power_of_attorney',
      'securities_register',
      'beneficial_interest_register',
      'ownership_organogram',
      'other'
    )),
  file_name text not null,
  storage_bucket text not null default 'compliance-documents',
  storage_path text not null,
  mime_type text,
  file_size bigint,
  certification_date date,
  expires_at date,
  notes text,
  created_by uuid references auth.users(id) on delete set null default auth.uid(),
  created_at timestamptz not null default now(),
  unique (storage_bucket, storage_path)
);

create index if not exists org_compliance_documents_org_idx
  on public.organisation_compliance_documents(organisation_id, party_id, document_type);

insert into storage.buckets (
  id,
  name,
  public,
  file_size_limit,
  allowed_mime_types
)
values (
  'compliance-documents',
  'compliance-documents',
  false,
  52428800,
  array[
    'application/pdf',
    'image/png',
    'image/jpeg',
    'image/webp'
  ]
)
on conflict (id) do update
set
  name = excluded.name,
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

create or replace function public.storage_object_org_id(_name text)
returns uuid
language plpgsql
immutable
as $$
declare
  _first_segment text;
begin
  _first_segment := split_part(coalesce(_name, ''), '/', 1);
  if _first_segment ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    return _first_segment::uuid;
  end if;
  return null;
end;
$$;

alter table public.organisation_compliance_parties enable row level security;
alter table public.organisation_directorships enable row level security;
alter table public.organisation_share_classes enable row level security;
alter table public.organisation_share_transactions enable row level security;
alter table public.organisation_beneficial_ownership_entries enable row level security;
alter table public.organisation_compliance_documents enable row level security;

drop policy if exists "org_compliance_parties_select_member" on public.organisation_compliance_parties;
create policy "org_compliance_parties_select_member"
  on public.organisation_compliance_parties for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "org_compliance_parties_insert_admins" on public.organisation_compliance_parties;
create policy "org_compliance_parties_insert_admins"
  on public.organisation_compliance_parties for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_compliance_parties_update_admins" on public.organisation_compliance_parties;
create policy "org_compliance_parties_update_admins"
  on public.organisation_compliance_parties for update to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_compliance_parties_delete_admins" on public.organisation_compliance_parties;
create policy "org_compliance_parties_delete_admins"
  on public.organisation_compliance_parties for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_directorships_select_member" on public.organisation_directorships;
create policy "org_directorships_select_member"
  on public.organisation_directorships for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "org_directorships_insert_admins" on public.organisation_directorships;
create policy "org_directorships_insert_admins"
  on public.organisation_directorships for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_directorships_update_admins" on public.organisation_directorships;
create policy "org_directorships_update_admins"
  on public.organisation_directorships for update to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_directorships_delete_admins" on public.organisation_directorships;
create policy "org_directorships_delete_admins"
  on public.organisation_directorships for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_share_classes_select_member" on public.organisation_share_classes;
create policy "org_share_classes_select_member"
  on public.organisation_share_classes for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "org_share_classes_insert_admins" on public.organisation_share_classes;
create policy "org_share_classes_insert_admins"
  on public.organisation_share_classes for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_share_classes_update_admins" on public.organisation_share_classes;
create policy "org_share_classes_update_admins"
  on public.organisation_share_classes for update to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_share_classes_delete_admins" on public.organisation_share_classes;
create policy "org_share_classes_delete_admins"
  on public.organisation_share_classes for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_share_transactions_select_member" on public.organisation_share_transactions;
create policy "org_share_transactions_select_member"
  on public.organisation_share_transactions for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "org_share_transactions_insert_admins" on public.organisation_share_transactions;
create policy "org_share_transactions_insert_admins"
  on public.organisation_share_transactions for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_share_transactions_delete_admins" on public.organisation_share_transactions;
create policy "org_share_transactions_delete_admins"
  on public.organisation_share_transactions for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_bo_entries_select_member" on public.organisation_beneficial_ownership_entries;
create policy "org_bo_entries_select_member"
  on public.organisation_beneficial_ownership_entries for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "org_bo_entries_insert_admins" on public.organisation_beneficial_ownership_entries;
create policy "org_bo_entries_insert_admins"
  on public.organisation_beneficial_ownership_entries for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_bo_entries_update_admins" on public.organisation_beneficial_ownership_entries;
create policy "org_bo_entries_update_admins"
  on public.organisation_beneficial_ownership_entries for update to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_bo_entries_delete_admins" on public.organisation_beneficial_ownership_entries;
create policy "org_bo_entries_delete_admins"
  on public.organisation_beneficial_ownership_entries for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_compliance_documents_select_member" on public.organisation_compliance_documents;
create policy "org_compliance_documents_select_member"
  on public.organisation_compliance_documents for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "org_compliance_documents_insert_admins" on public.organisation_compliance_documents;
create policy "org_compliance_documents_insert_admins"
  on public.organisation_compliance_documents for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_compliance_documents_delete_admins" on public.organisation_compliance_documents;
create policy "org_compliance_documents_delete_admins"
  on public.organisation_compliance_documents for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "compliance_documents_select_member" on storage.objects;
create policy "compliance_documents_select_member"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'compliance-documents'
    and public.is_org_member(public.storage_object_org_id(name))
  );

drop policy if exists "compliance_documents_insert_admins" on storage.objects;
create policy "compliance_documents_insert_admins"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'compliance-documents'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop policy if exists "compliance_documents_update_admins" on storage.objects;
create policy "compliance_documents_update_admins"
  on storage.objects for update to authenticated
  using (
    bucket_id = 'compliance-documents'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  )
  with check (
    bucket_id = 'compliance-documents'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop policy if exists "compliance_documents_delete_admins" on storage.objects;
create policy "compliance_documents_delete_admins"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'compliance-documents'
    and public.has_org_role(
      public.storage_object_org_id(name),
      array['owner','admin']::public.organisation_role[]
    )
  );

drop trigger if exists org_compliance_parties_set_updated_at on public.organisation_compliance_parties;
create trigger org_compliance_parties_set_updated_at
  before update on public.organisation_compliance_parties
  for each row execute function public.set_updated_at();

drop trigger if exists org_directorships_set_updated_at on public.organisation_directorships;
create trigger org_directorships_set_updated_at
  before update on public.organisation_directorships
  for each row execute function public.set_updated_at();

drop trigger if exists org_share_classes_set_updated_at on public.organisation_share_classes;
create trigger org_share_classes_set_updated_at
  before update on public.organisation_share_classes
  for each row execute function public.set_updated_at();

drop trigger if exists org_bo_entries_set_updated_at on public.organisation_beneficial_ownership_entries;
create trigger org_bo_entries_set_updated_at
  before update on public.organisation_beneficial_ownership_entries
  for each row execute function public.set_updated_at();

select 'organisation_compliance_registers_phase_c6_applied' as migration_note;
