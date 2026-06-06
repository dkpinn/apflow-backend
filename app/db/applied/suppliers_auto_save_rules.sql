alter table public.suppliers
  add column if not exists auto_save_rules boolean not null default false;
