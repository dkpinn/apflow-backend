-- ============================================================
-- APPayPal Supplier Branches Phase B29
-- Stores branch-level supplier details for suppliers whose VAT,
-- address, contact, or banking details differ by branch.
--
-- Branch data is used for review/comparison only. It does not
-- overwrite supplier master values or invoice calculations.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create table if not exists public.supplier_branches (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  supplier_id uuid not null references public.suppliers(id) on delete cascade,
  branch_name text not null,
  branch_code text,
  vat_number text,
  tax_number text,
  company_registration_number text,
  phone text,
  default_email text,
  website text,
  delivery_address text,
  postal_address text,
  bank_account_name text,
  bank_name text,
  bank_account_number text,
  bank_branch_code text,
  bank_swift_code text,
  active boolean not null default true,
  source_invoice_extracted_id uuid references public.invoices_extracted(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.invoices_extracted
  add column if not exists supplier_branch_id uuid references public.supplier_branches(id) on delete set null;

create index if not exists supplier_branches_org_supplier_idx
  on public.supplier_branches(organisation_id, supplier_id, active);

create index if not exists supplier_branches_vat_idx
  on public.supplier_branches(organisation_id, vat_number)
  where vat_number is not null;

create index if not exists invoices_extracted_supplier_branch_idx
  on public.invoices_extracted(supplier_branch_id);

drop trigger if exists supplier_branches_set_updated_at
  on public.supplier_branches;
create trigger supplier_branches_set_updated_at
  before update on public.supplier_branches
  for each row execute function public.set_updated_at();

alter table public.supplier_branches enable row level security;

drop policy if exists "supplier_branches_select_member"
  on public.supplier_branches;
create policy "supplier_branches_select_member"
  on public.supplier_branches for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "supplier_branches_write_accountants"
  on public.supplier_branches;
create policy "supplier_branches_write_accountants"
  on public.supplier_branches for all to authenticated
  using (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  )
  with check (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

select 'supplier_branches_phase_b29_applied' as migration_note;
