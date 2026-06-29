alter table public.remittances
  add column if not exists payment_run_id uuid references public.supplier_payment_runs(id) on delete set null,
  add column if not exists supplier_name text,
  add column if not exists supplier_email text,
  add column if not exists invoice_count integer not null default 0,
  add column if not exists invoice_references jsonb not null default '[]'::jsonb,
  add column if not exists generated_by uuid;

create index if not exists remittances_payment_run_idx
  on public.remittances(payment_run_id);

create unique index if not exists remittances_payment_run_supplier_uidx
  on public.remittances(payment_run_id, supplier_id)
  where payment_run_id is not null;
