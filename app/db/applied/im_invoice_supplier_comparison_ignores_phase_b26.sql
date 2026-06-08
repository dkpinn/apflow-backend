-- ============================================================
-- APPayPal Invoice Supplier Comparison Ignores Phase B26
-- Stores per-invoice supplier comparison fields a reviewer has
-- intentionally ignored because no master/invoice change is needed.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create table if not exists public.invoice_supplier_comparison_ignores (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  invoice_extracted_id uuid not null references public.invoices_extracted(id) on delete cascade,
  supplier_id uuid references public.suppliers(id) on delete set null,
  field_key text not null,
  reason text,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  constraint invoice_supplier_comparison_ignores_field_check check (
    field_key in (
      'supplier_name_extracted',
      'supplier_telephone_extracted',
      'supplier_email_extracted',
      'supplier_website_extracted',
      'supplier_del_address_extracted',
      'company_registration_number_extracted'
    )
  ),
  unique (organisation_id, invoice_extracted_id, field_key)
);

create index if not exists invoice_supplier_comparison_ignores_invoice_idx
  on public.invoice_supplier_comparison_ignores(invoice_extracted_id);

create index if not exists invoice_supplier_comparison_ignores_org_idx
  on public.invoice_supplier_comparison_ignores(organisation_id, created_at desc);

alter table public.invoice_supplier_comparison_ignores enable row level security;

drop policy if exists "invoice_supplier_comparison_ignores_select_member"
  on public.invoice_supplier_comparison_ignores;
create policy "invoice_supplier_comparison_ignores_select_member"
  on public.invoice_supplier_comparison_ignores for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "invoice_supplier_comparison_ignores_insert_accountants"
  on public.invoice_supplier_comparison_ignores;
create policy "invoice_supplier_comparison_ignores_insert_accountants"
  on public.invoice_supplier_comparison_ignores for insert to authenticated
  with check (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "invoice_supplier_comparison_ignores_delete_accountants"
  on public.invoice_supplier_comparison_ignores;
create policy "invoice_supplier_comparison_ignores_delete_accountants"
  on public.invoice_supplier_comparison_ignores for delete to authenticated
  using (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

select 'invoice_supplier_comparison_ignores_phase_b26_applied' as migration_note;
