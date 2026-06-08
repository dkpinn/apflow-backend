-- Phase B33: Supplier KYC approval workflow
-- Creates supplier_kyc_requests and supplier_kyc_documents tables,
-- extends the suppliers table with kyc_status fields, and adds RLS policies.

-- ── 1. supplier_kyc_requests ─────────────────────────────────────────────────

create table if not exists public.supplier_kyc_requests (
  id              uuid        primary key default gen_random_uuid(),
  organisation_id uuid        not null references public.organisations(id) on delete cascade,
  supplier_id     uuid        not null references public.suppliers(id)     on delete cascade,
  trigger_type    text        not null check (trigger_type in (
                                'new_supplier','bank_change','info_change',
                                'periodic_review','other')),
  status          text        not null default 'draft' check (status in (
                                'draft','submitted','approved','rejected','cancelled')),
  notes           text,
  requested_by    uuid        references auth.users(id),
  submitted_at    timestamptz,
  reviewed_by     uuid        references auth.users(id),
  reviewed_at     timestamptz,
  reviewer_notes  text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

comment on table public.supplier_kyc_requests is
  'Know-Your-Customer approval requests for supplier onboarding and data changes.';

comment on column public.supplier_kyc_requests.trigger_type is
  'What prompted this KYC request: new_supplier, bank_change, info_change, periodic_review, other.';

comment on column public.supplier_kyc_requests.status is
  'Lifecycle: draft → submitted → approved|rejected. Can also be cancelled.';

-- ── 2. supplier_kyc_documents ────────────────────────────────────────────────

create table if not exists public.supplier_kyc_documents (
  id              uuid        primary key default gen_random_uuid(),
  kyc_request_id  uuid        not null references public.supplier_kyc_requests(id) on delete cascade,
  organisation_id uuid        not null references public.organisations(id) on delete cascade,
  document_type   text        not null check (document_type in (
                                'id_document','company_registration','bank_confirmation',
                                'vat_certificate','tax_clearance','proof_of_address','other')),
  document_label  text,
  storage_path    text        not null,
  file_name       text        not null,
  file_size       bigint,
  mime_type       text,
  uploaded_by     uuid        references auth.users(id),
  uploaded_at     timestamptz not null default now(),
  notes           text,
  created_at      timestamptz not null default now()
);

comment on table public.supplier_kyc_documents is
  'Documents uploaded as evidence for a KYC request (stored in Supabase Storage).';

-- ── 3. Extend suppliers table ─────────────────────────────────────────────────

alter table public.suppliers
  add column if not exists kyc_status     text default 'not_started'
    check (kyc_status in ('not_started','pending','approved','rejected')),
  add column if not exists kyc_verified_at  timestamptz,
  add column if not exists kyc_verified_by  uuid references auth.users(id);

comment on column public.suppliers.kyc_status     is 'Current KYC state: not_started, pending, approved, rejected.';
comment on column public.suppliers.kyc_verified_at is 'When the most recent KYC approval was granted.';
comment on column public.suppliers.kyc_verified_by is 'Who approved the most recent KYC.';

-- ── 4. updated_at trigger ────────────────────────────────────────────────────

drop trigger if exists supplier_kyc_requests_set_updated_at on public.supplier_kyc_requests;
create trigger supplier_kyc_requests_set_updated_at
  before update on public.supplier_kyc_requests
  for each row execute function public.set_updated_at();

-- ── 5. RLS ───────────────────────────────────────────────────────────────────

alter table public.supplier_kyc_requests  enable row level security;
alter table public.supplier_kyc_documents enable row level security;

-- supplier_kyc_requests
drop policy if exists "kyc_req_select" on public.supplier_kyc_requests;
create policy "kyc_req_select"
  on public.supplier_kyc_requests for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "kyc_req_insert" on public.supplier_kyc_requests;
create policy "kyc_req_insert"
  on public.supplier_kyc_requests for insert to authenticated
  with check (
    public.has_org_role(organisation_id,
      array['owner','admin','accountant']::public.organisation_role[])
  );

drop policy if exists "kyc_req_update" on public.supplier_kyc_requests;
create policy "kyc_req_update"
  on public.supplier_kyc_requests for update to authenticated
  using (
    public.has_org_role(organisation_id,
      array['owner','admin']::public.organisation_role[])
    or (requested_by = auth.uid() and status = 'draft')
  );

drop policy if exists "kyc_req_delete" on public.supplier_kyc_requests;
create policy "kyc_req_delete"
  on public.supplier_kyc_requests for delete to authenticated
  using (public.has_org_role(organisation_id,
    array['owner','admin']::public.organisation_role[]));

-- supplier_kyc_documents
drop policy if exists "kyc_doc_select" on public.supplier_kyc_documents;
create policy "kyc_doc_select"
  on public.supplier_kyc_documents for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "kyc_doc_insert" on public.supplier_kyc_documents;
create policy "kyc_doc_insert"
  on public.supplier_kyc_documents for insert to authenticated
  with check (
    public.has_org_role(organisation_id,
      array['owner','admin','accountant']::public.organisation_role[])
  );

drop policy if exists "kyc_doc_delete" on public.supplier_kyc_documents;
create policy "kyc_doc_delete"
  on public.supplier_kyc_documents for delete to authenticated
  using (
    public.has_org_role(organisation_id,
      array['owner','admin']::public.organisation_role[])
    or uploaded_by = auth.uid()
  );
