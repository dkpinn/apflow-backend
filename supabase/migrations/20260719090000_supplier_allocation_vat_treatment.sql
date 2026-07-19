alter table public.supplier_line_item_allocation_rule_splits
  add column if not exists vat_treatment text
  check (vat_treatment is null or vat_treatment in ('full', 'blocked', 'exempt', 'zero_rated'));

alter table public.invoice_line_item_allocations
  add column if not exists vat_treatment text
  check (vat_treatment is null or vat_treatment in ('full', 'blocked', 'exempt', 'zero_rated'));

comment on column public.supplier_line_item_allocation_rule_splits.vat_treatment is
  'Optional explicit VAT treatment for this rule split; null inherits the selected account treatment.';

comment on column public.invoice_line_item_allocations.vat_treatment is
  'VAT treatment applied to this accounting allocation; null inherits the parent line/account treatment.';
