-- ============================================================
-- Multi-document invoice handling — Phase B20
-- Adds organisation extraction settings, page grouping metadata,
-- and invoices_raw columns to support multi-page/multi-supplier PDFs.
-- ============================================================

-- 1) Organisation-level extraction settings -------------------
alter table if exists public.organisations
  add column if not exists extraction_strategy text default 'auto_group',
  add column if not exists ask_per_upload boolean default false,
  add column if not exists vlm_enabled boolean default false;

-- 2) invoice_page_groups metadata table -----------------------
create table if not exists public.invoice_page_groups (
  id uuid primary key default gen_random_uuid(),
  -- organisation_id intentionally omitted: derive via invoices_raw join
  invoice_raw_id uuid not null references public.invoices_raw(id) on delete cascade,
  page_numbers jsonb not null,
  supplier_detected text,
  confidence numeric,
  strategy text,
  created_at timestamptz not null default now()
);
create index if not exists invoice_page_groups_invoice_raw_idx
  on public.invoice_page_groups(invoice_raw_id);

-- 3) invoices_raw additions ----------------------------------
alter table if exists public.invoices_raw
  add column if not exists grouped_from_pages jsonb default '[]'::jsonb,
  add column if not exists page_grouping_strategy text default null,
  add column if not exists total_pages_in_upload int default null;

-- Record migration application
select 'im_invoice_multidoc_phase_b20_applied' as migration_note;
