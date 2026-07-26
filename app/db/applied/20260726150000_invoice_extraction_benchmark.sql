begin;

create table if not exists public.invoice_extraction_gold_documents (
    id uuid primary key default gen_random_uuid(),
    organisation_id uuid not null references public.organisations(id) on delete cascade,
    invoice_raw_id uuid not null references public.invoices_raw(id) on delete cascade,
    invoice_extracted_id uuid references public.invoices_extracted(id) on delete set null,
    document_kind text not null check (document_kind in ('invoice', 'credit_note', 'receipt')),
    source_format text not null check (source_format in ('pdf', 'image')),
    dataset_split text not null default 'development' check (dataset_split in ('development', 'validation', 'locked')),
    source_file_name text,
    source_file_type text,
    source_sha256 text,
    gold_json jsonb not null,
    notes text,
    verified_by uuid references auth.users(id) on delete set null,
    verified_at timestamptz,
    created_by uuid references auth.users(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (invoice_raw_id)
);

create table if not exists public.invoice_extraction_benchmark_runs (
    id uuid primary key default gen_random_uuid(),
    gold_document_id uuid not null references public.invoice_extraction_gold_documents(id) on delete cascade,
    invoice_extracted_id uuid references public.invoices_extracted(id) on delete set null,
    extractor_version text,
    extracted_json jsonb not null,
    correct_values integer not null,
    total_values integer not null,
    accuracy numeric(8,6) not null,
    correction_count integer not null,
    within_two_corrections boolean not null,
    exact_document boolean not null,
    critical_error_count integer not null,
    discrepancies jsonb not null default '[]'::jsonb,
    extraction_rerun boolean not null default false,
    run_by uuid references auth.users(id) on delete set null,
    created_at timestamptz not null default now()
);

create index if not exists invoice_extraction_gold_split_idx
    on public.invoice_extraction_gold_documents(dataset_split, document_kind, source_format, created_at);
create index if not exists invoice_extraction_benchmark_runs_latest_idx
    on public.invoice_extraction_benchmark_runs(gold_document_id, created_at desc);

alter table public.invoice_extraction_gold_documents enable row level security;
alter table public.invoice_extraction_benchmark_runs enable row level security;

-- These tables contain cross-organisation source truth and are intentionally
-- service-role only. API access is separately restricted to platform owners.
revoke all on public.invoice_extraction_gold_documents from anon, authenticated;
revoke all on public.invoice_extraction_benchmark_runs from anon, authenticated;
grant all on public.invoice_extraction_gold_documents to service_role;
grant all on public.invoice_extraction_benchmark_runs to service_role;

commit;
