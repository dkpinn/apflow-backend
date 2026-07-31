alter table public.invoices_extracted
  add column if not exists document_reference text;

comment on column public.invoices_extracted.document_reference is
  'Supplier, booking, order, or other document reference distinct from the invoice number.';
