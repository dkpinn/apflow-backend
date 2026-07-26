begin;

alter table public.invoice_line_items
    add column if not exists sort_order integer;

with ranked as (
    select
        id,
        row_number() over (
            partition by invoice_extracted_id
            order by created_at asc, id asc
        ) - 1 as inferred_sort_order
    from public.invoice_line_items
)
update public.invoice_line_items as line
set sort_order = ranked.inferred_sort_order
from ranked
where line.id = ranked.id
  and line.sort_order is null;

alter table public.invoice_line_items
    alter column sort_order set default 0,
    alter column sort_order set not null;

create index if not exists invoice_line_items_invoice_sort_idx
    on public.invoice_line_items (invoice_extracted_id, sort_order, id);

commit;
