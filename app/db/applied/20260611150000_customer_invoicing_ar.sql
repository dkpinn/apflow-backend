-- Customer invoicing and accounts receivable.
-- South Africa-first sales invoices, credit notes, receipt allocation, and GL posting.

create table if not exists public.customers (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  customer_code text,
  legal_name text not null,
  trading_name text,
  vat_number text,
  registration_number text,
  billing_address text,
  delivery_address text,
  default_email text,
  phone text,
  payment_terms_days integer not null default 30 check (payment_terms_days >= 0),
  currency text not null default 'ZAR',
  default_revenue_account_id uuid references public.accounts(id) on delete set null,
  default_vat_treatment text not null default 'standard'
    check (default_vat_treatment in ('standard','zero_rated','exempt')),
  default_tracking jsonb not null default '{}'::jsonb
    check (jsonb_typeof(default_tracking) = 'object'),
  active boolean not null default true,
  archived_at timestamptz,
  archived_by uuid references auth.users(id) on delete set null,
  created_by uuid references auth.users(id) on delete set null,
  updated_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organisation_id, customer_code)
);

create index if not exists customers_org_name_idx
  on public.customers(organisation_id, active, legal_name);
create index if not exists customers_org_vat_idx
  on public.customers(organisation_id, vat_number)
  where vat_number is not null;

create table if not exists public.customer_contacts (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  customer_id uuid not null references public.customers(id) on delete cascade,
  contact_name text not null,
  email text,
  phone text,
  role_title text,
  is_primary boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists customer_contacts_customer_idx
  on public.customer_contacts(customer_id, is_primary desc, contact_name);

create table if not exists public.sales_document_sequences (
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  document_type text not null check (document_type in ('invoice','credit_note')),
  prefix text not null,
  next_number bigint not null default 1 check (next_number > 0),
  updated_at timestamptz not null default now(),
  primary key (organisation_id, document_type)
);

create table if not exists public.sales_invoices (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  customer_id uuid not null references public.customers(id) on delete restrict,
  document_type text not null default 'invoice'
    check (document_type in ('invoice','credit_note')),
  original_invoice_id uuid references public.sales_invoices(id) on delete restrict,
  credit_reason text,
  invoice_number text,
  status text not null default 'draft'
    check (status in ('draft','pending_approval','approved','issued','voided')),
  payment_status text not null default 'unpaid'
    check (payment_status in ('unpaid','partial','paid','overdue')),
  issue_date date,
  due_date date,
  currency text not null default 'ZAR',
  customer_reference text,
  purchase_order_number text,
  notes text,
  subtotal numeric(14,2) not null default 0,
  discount_total numeric(14,2) not null default 0,
  tax_total numeric(14,2) not null default 0,
  total_amount numeric(14,2) not null default 0,
  amount_paid numeric(14,2) not null default 0,
  amount_credited numeric(14,2) not null default 0,
  amount_outstanding numeric(14,2) not null default 0,
  issuer_snapshot jsonb not null default '{}'::jsonb,
  customer_snapshot jsonb not null default '{}'::jsonb,
  branding_snapshot jsonb not null default '{}'::jsonb,
  approval_request_id uuid references public.approval_requests(id) on delete set null,
  approved_by uuid references auth.users(id) on delete set null,
  approved_at timestamptz,
  issued_by uuid references auth.users(id) on delete set null,
  issued_at timestamptz,
  gl_journal_id uuid references public.gl_journals(id) on delete restrict,
  pdf_storage_path text,
  created_by uuid references auth.users(id) on delete set null,
  updated_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (
    (document_type = 'invoice' and original_invoice_id is null and credit_reason is null)
    or
    (document_type = 'credit_note' and original_invoice_id is not null and nullif(btrim(credit_reason), '') is not null)
  ),
  unique (organisation_id, invoice_number)
);

create index if not exists sales_invoices_org_status_idx
  on public.sales_invoices(organisation_id, document_type, status, issue_date desc);
create index if not exists sales_invoices_customer_idx
  on public.sales_invoices(customer_id, issue_date desc);
create index if not exists sales_invoices_original_idx
  on public.sales_invoices(original_invoice_id)
  where original_invoice_id is not null;

create table if not exists public.sales_invoice_lines (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  sales_invoice_id uuid not null references public.sales_invoices(id) on delete cascade,
  description text not null,
  item_code text,
  quantity numeric(14,4) not null default 1 check (quantity > 0),
  unit_price numeric(14,4) not null default 0 check (unit_price >= 0),
  prices_include_vat boolean not null default false,
  discount_percent numeric(7,4) not null default 0 check (discount_percent >= 0 and discount_percent <= 100),
  discount_amount numeric(14,2) not null default 0 check (discount_amount >= 0),
  vat_treatment text not null default 'standard'
    check (vat_treatment in ('standard','zero_rated','exempt')),
  vat_rate numeric(7,4) not null default 15 check (vat_rate >= 0 and vat_rate <= 100),
  revenue_account_id uuid not null references public.accounts(id) on delete restrict,
  tracking jsonb not null default '{}'::jsonb check (jsonb_typeof(tracking) = 'object'),
  net_amount numeric(14,2) not null default 0,
  tax_amount numeric(14,2) not null default 0,
  gross_amount numeric(14,2) not null default 0,
  source_invoice_extracted_id uuid references public.invoices_extracted(id) on delete set null,
  source_invoice_line_id uuid references public.invoice_line_items(id) on delete set null,
  source_unit_cost numeric(14,4),
  markup_percent numeric(9,4),
  sort_order smallint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists sales_invoice_lines_invoice_idx
  on public.sales_invoice_lines(sales_invoice_id, sort_order, id);
create index if not exists sales_invoice_lines_source_idx
  on public.sales_invoice_lines(source_invoice_line_id)
  where source_invoice_line_id is not null;

create table if not exists public.sales_invoice_delivery_events (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  sales_invoice_id uuid not null references public.sales_invoices(id) on delete restrict,
  event_type text not null check (event_type in ('queued','sent','delivered','failed')),
  recipient_email text,
  provider text not null default 'mailgun',
  provider_message_id text,
  details jsonb not null default '{}'::jsonb,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create index if not exists sales_invoice_delivery_events_invoice_idx
  on public.sales_invoice_delivery_events(sales_invoice_id, created_at desc);
create index if not exists sales_invoice_delivery_events_provider_idx
  on public.sales_invoice_delivery_events(provider_message_id)
  where provider_message_id is not null;

create table if not exists public.sales_invoice_audit_events (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  sales_invoice_id uuid not null references public.sales_invoices(id) on delete restrict,
  event_type text not null,
  actor_user_id uuid references auth.users(id) on delete set null,
  actor_type text not null default 'user',
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists sales_invoice_audit_events_invoice_idx
  on public.sales_invoice_audit_events(sales_invoice_id, created_at);

create table if not exists public.customer_receipts (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  customer_id uuid not null references public.customers(id) on delete restrict,
  bank_account_id uuid references public.bank_accounts(id) on delete restrict,
  bank_statement_line_id uuid references public.bank_statement_lines(id) on delete set null,
  receipt_date date not null,
  amount numeric(14,2) not null check (amount > 0),
  currency text not null default 'ZAR',
  reference text,
  notes text,
  status text not null default 'posted' check (status in ('posted','reversed')),
  idempotency_key text,
  gl_journal_id uuid not null references public.gl_journals(id) on delete restrict,
  posted_by uuid references auth.users(id) on delete set null,
  posted_at timestamptz not null default now(),
  reversed_by uuid references auth.users(id) on delete set null,
  reversed_at timestamptz,
  created_at timestamptz not null default now(),
  unique (organisation_id, idempotency_key)
);

create index if not exists customer_receipts_customer_idx
  on public.customer_receipts(customer_id, receipt_date desc);

create table if not exists public.customer_receipt_allocations (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  customer_receipt_id uuid not null references public.customer_receipts(id) on delete restrict,
  sales_invoice_id uuid not null references public.sales_invoices(id) on delete restrict,
  amount numeric(14,2) not null check (amount > 0),
  created_at timestamptz not null default now(),
  unique (customer_receipt_id, sales_invoice_id)
);

create index if not exists customer_receipt_allocations_invoice_idx
  on public.customer_receipt_allocations(sales_invoice_id);

alter table public.bank_transaction_suggestions
  add column if not exists matched_sales_invoice_id uuid references public.sales_invoices(id) on delete set null;
alter table public.bank_statement_lines
  add column if not exists matched_sales_invoice_id uuid references public.sales_invoices(id) on delete set null;

alter table public.approval_workflows
  drop constraint if exists approval_workflows_workflow_type_check;
alter table public.approval_workflows
  add constraint approval_workflows_workflow_type_check
  check (workflow_type in ('invoice','sales_invoice'));

alter table public.approval_requests
  drop constraint if exists approval_requests_workflow_type_check;
alter table public.approval_requests
  add constraint approval_requests_workflow_type_check
  check (workflow_type in ('invoice','sales_invoice'));

alter table public.approval_delegations
  drop constraint if exists approval_delegations_workflow_type_check;
alter table public.approval_delegations
  add constraint approval_delegations_workflow_type_check
  check (workflow_type in ('invoice','sales_invoice'));

create or replace function public.refresh_sales_invoice_payment_status(p_invoice_id uuid)
returns void
language plpgsql security definer set search_path = public
as $$
declare
  inv public.sales_invoices%rowtype;
  paid numeric(14,2);
  credited numeric(14,2);
  outstanding numeric(14,2);
begin
  select * into inv from public.sales_invoices where id = p_invoice_id for update;
  if not found or inv.document_type <> 'invoice' or inv.status <> 'issued' then
    return;
  end if;

  select coalesce(sum(a.amount), 0) into paid
  from public.customer_receipt_allocations a
  join public.customer_receipts r on r.id = a.customer_receipt_id
  where a.sales_invoice_id = p_invoice_id and r.status = 'posted';

  select coalesce(sum(c.total_amount), 0) into credited
  from public.sales_invoices c
  where c.original_invoice_id = p_invoice_id
    and c.document_type = 'credit_note'
    and c.status = 'issued';

  outstanding := greatest(round(inv.total_amount - paid - credited, 2), 0);
  update public.sales_invoices
  set amount_paid = paid,
      amount_credited = credited,
      amount_outstanding = outstanding,
      payment_status = case
        when outstanding <= 0 then 'paid'
        when paid > 0 or credited > 0 then 'partial'
        when due_date is not null and due_date < current_date then 'overdue'
        else 'unpaid'
      end,
      updated_at = now()
  where id = p_invoice_id;
end;
$$;

create or replace function public.allocate_sales_document_number(
  p_org_id uuid,
  p_document_type text
)
returns text
language plpgsql security definer set search_path = public
as $$
declare
  allocated bigint;
  effective_prefix text;
begin
  if p_document_type not in ('invoice','credit_note') then
    raise exception 'Unsupported sales document type';
  end if;
  effective_prefix := case when p_document_type = 'invoice' then 'INV-' else 'CN-' end;
  insert into public.sales_document_sequences(organisation_id, document_type, prefix, next_number)
  values (p_org_id, p_document_type, effective_prefix, 2)
  on conflict (organisation_id, document_type)
  do update set next_number = public.sales_document_sequences.next_number + 1,
                updated_at = now()
  returning next_number - 1, prefix into allocated, effective_prefix;
  return effective_prefix || lpad(allocated::text, 6, '0');
end;
$$;

create or replace function public.prevent_issued_sales_invoice_mutation()
returns trigger
language plpgsql security definer set search_path = public
as $$
begin
  if old.status is distinct from new.status then
    if new.status = 'issued'
       and current_setting('app.sales_invoice_issue', true) is distinct from 'on' then
      raise exception 'Sales invoices must be issued through the atomic issue function';
    end if;
    if old.status = 'issued' then
      raise exception 'Issued sales invoices are immutable; create a credit note instead';
    end if;
    if new.status = 'approved'
       and not (
         public.has_org_role(old.organisation_id, array['owner','admin']::public.organisation_role[])
         or exists (
           select 1
           from public.approval_request_steps step
           join public.organisation_users member
             on member.organisation_id = old.organisation_id
            and member.user_id = auth.uid()
            and member.status = 'active'
           where step.request_id = old.approval_request_id
             and step.status = 'pending'
             and (
               step.approver_user_id = auth.uid()
               or step.approver_role = member.role
             )
         )
       ) then
      raise exception 'Not authorised to approve this sales invoice';
    end if;
    if new.status not in ('pending_approval','approved','issued') then
      raise exception 'Unsupported sales invoice status transition';
    end if;
  end if;
  if old.status <> 'draft' and (
    new.customer_id is distinct from old.customer_id
    or new.document_type is distinct from old.document_type
    or new.original_invoice_id is distinct from old.original_invoice_id
    or new.invoice_number is distinct from old.invoice_number
    or new.issue_date is distinct from old.issue_date
    or new.due_date is distinct from old.due_date
    or new.currency is distinct from old.currency
    or new.subtotal is distinct from old.subtotal
    or new.discount_total is distinct from old.discount_total
    or new.tax_total is distinct from old.tax_total
    or new.total_amount is distinct from old.total_amount
    or new.issuer_snapshot is distinct from old.issuer_snapshot
    or new.customer_snapshot is distinct from old.customer_snapshot
    or new.branding_snapshot is distinct from old.branding_snapshot
    or new.gl_journal_id is distinct from old.gl_journal_id
  ) then
    raise exception 'Submitted sales invoices cannot be edited; return to draft or create a credit note';
  end if;
  return new;
end;
$$;

create or replace function public.prevent_issued_sales_invoice_line_mutation()
returns trigger
language plpgsql security definer set search_path = public
as $$
declare
  invoice_id uuid;
  invoice_status text;
begin
  invoice_id := coalesce(new.sales_invoice_id, old.sales_invoice_id);
  select status into invoice_status from public.sales_invoices where id = invoice_id;
  if invoice_status = 'issued' then
    raise exception 'Issued sales invoice lines are immutable; create a credit note instead';
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

drop trigger if exists sales_invoices_prevent_issued_mutation on public.sales_invoices;
create trigger sales_invoices_prevent_issued_mutation
  before update on public.sales_invoices
  for each row execute function public.prevent_issued_sales_invoice_mutation();

drop trigger if exists sales_invoice_lines_prevent_issued_mutation on public.sales_invoice_lines;
create trigger sales_invoice_lines_prevent_issued_mutation
  before insert or update or delete on public.sales_invoice_lines
  for each row execute function public.prevent_issued_sales_invoice_line_mutation();

create or replace function public.issue_sales_invoice_atomic(
  p_org_id uuid,
  p_sales_invoice_id uuid,
  p_actor_user_id uuid default auth.uid()
)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  inv public.sales_invoices%rowtype;
  customer public.customers%rowtype;
  org public.organisations%rowtype;
  branding jsonb;
  receivables_id uuid;
  vat_control_id uuid;
  journal_id uuid;
  line record;
  line_count integer;
  v_net_total numeric(14,2);
  v_tax_total numeric(14,2);
  v_gross_total numeric(14,2);
  prior_credits numeric(14,2);
  base_currency text;
  approval_required boolean;
begin
  if p_actor_user_id is null
     or p_actor_user_id is distinct from auth.uid()
     or not public.has_org_role(p_org_id, array['owner','admin','accountant']::public.organisation_role[]) then
    raise exception 'Not authorised to issue sales invoices';
  end if;

  select * into inv
  from public.sales_invoices
  where id = p_sales_invoice_id and organisation_id = p_org_id
  for update;
  if not found then raise exception 'Sales invoice not found'; end if;
  if inv.status = 'issued' then raise exception 'Sales invoice has already been issued'; end if;

  select * into org from public.organisations where id = p_org_id;
  approval_required := coalesce(org.invoice_approval_required, true);
  if approval_required and inv.status <> 'approved' then
    raise exception 'Sales invoice must be approved before issue';
  end if;
  if not approval_required and inv.status not in ('draft','approved') then
    raise exception 'Sales invoice is not ready to issue';
  end if;

  base_currency := coalesce(nullif(org.base_currency, ''), nullif(org.currency, ''), 'ZAR');
  if inv.currency <> base_currency then
    raise exception 'Issued sales invoices must use the organisation base currency';
  end if;

  select * into customer
  from public.customers
  where id = inv.customer_id and organisation_id = p_org_id and active = true;
  if not found then raise exception 'Active customer not found'; end if;

  select count(*), coalesce(sum(net_amount), 0), coalesce(sum(tax_amount), 0),
         coalesce(sum(gross_amount), 0)
  into line_count, v_net_total, v_tax_total, v_gross_total
  from public.sales_invoice_lines
  where sales_invoice_id = inv.id and organisation_id = p_org_id;
  if line_count = 0 or v_gross_total <= 0 then raise exception 'Sales invoice has no billable lines'; end if;
  if abs(v_gross_total - (v_net_total + v_tax_total)) > 0.02 then
    raise exception 'Sales invoice lines do not balance';
  end if;

  if inv.document_type = 'credit_note' then
    if not exists (
      select 1 from public.sales_invoices original
      where original.id = inv.original_invoice_id
        and original.organisation_id = p_org_id
        and original.customer_id = inv.customer_id
        and original.document_type = 'invoice'
        and original.status = 'issued'
    ) then
      raise exception 'Credit note must reference an issued invoice for the same customer';
    end if;
    select coalesce(sum(total_amount), 0) into prior_credits
    from public.sales_invoices
    where original_invoice_id = inv.original_invoice_id
      and document_type = 'credit_note'
      and status = 'issued'
      and id <> inv.id;
    if prior_credits + v_gross_total > (
      select total_amount from public.sales_invoices where id = inv.original_invoice_id
    ) + 0.02 then
      raise exception 'Credit notes cannot exceed the original invoice total';
    end if;
  end if;

  select id into receivables_id from public.accounts
  where organisation_id = p_org_id and system_key = 'trade_receivables' limit 1;
  select id into vat_control_id from public.accounts
  where organisation_id = p_org_id and system_key = 'vat_control' limit 1;
  if receivables_id is null then raise exception 'Trade Receivables system account not found'; end if;
  if v_tax_total > 0 and vat_control_id is null then raise exception 'VAT Control system account not found'; end if;
  if exists (
    select 1
    from public.sales_invoice_lines l
    left join public.accounts a on a.id = l.revenue_account_id
    where l.sales_invoice_id = inv.id
      and (a.id is null or a.organisation_id <> p_org_id or a.type <> 'income' or not a.active)
  ) then
    raise exception 'Every sales line needs an active income account';
  end if;

  select coalesce(to_jsonb(b), '{}'::jsonb) into branding
  from public.organisation_invoice_branding b where b.organisation_id = p_org_id;

  insert into public.gl_journals(
    organisation_id, source_type, source_id, journal_date, description,
    status, total_debit, total_credit, created_by, posted_by, posted_at
  ) values (
    p_org_id,
    case when inv.document_type = 'invoice' then 'sales_invoice' else 'sales_credit_note' end,
    inv.id,
    coalesce(inv.issue_date, current_date),
    case when inv.document_type = 'invoice' then 'Sales invoice' else 'Sales credit note' end,
    'posted', v_gross_total, v_gross_total, p_actor_user_id, p_actor_user_id, now()
  ) returning id into journal_id;

  insert into public.gl_journal_lines(
    organisation_id, gl_journal_id, account_id, description,
    debit_amount, credit_amount, tracking, sort_order
  ) values (
    p_org_id, journal_id, receivables_id, customer.legal_name,
    case when inv.document_type = 'invoice' then v_gross_total else 0 end,
    case when inv.document_type = 'credit_note' then v_gross_total else 0 end,
    '{}'::jsonb, 0
  );

  for line in
    select * from public.sales_invoice_lines
    where sales_invoice_id = inv.id order by sort_order, id
  loop
    insert into public.gl_journal_lines(
      organisation_id, gl_journal_id, account_id, description,
      debit_amount, credit_amount, tracking, sort_order
    ) values (
      p_org_id, journal_id, line.revenue_account_id, line.description,
      case when inv.document_type = 'credit_note' then line.net_amount else 0 end,
      case when inv.document_type = 'invoice' then line.net_amount else 0 end,
      line.tracking, line.sort_order + 1
    );
  end loop;

  if v_tax_total > 0 then
    insert into public.gl_journal_lines(
      organisation_id, gl_journal_id, account_id, description,
      debit_amount, credit_amount, tracking, sort_order
    ) values (
      p_org_id, journal_id, vat_control_id, 'Output VAT',
      case when inv.document_type = 'credit_note' then v_tax_total else 0 end,
      case when inv.document_type = 'invoice' then v_tax_total else 0 end,
      '{}'::jsonb, line_count + 1
    );
  end if;

  perform set_config('app.sales_invoice_issue', 'on', true);
  update public.sales_invoices
  set invoice_number = public.allocate_sales_document_number(p_org_id, inv.document_type),
      issue_date = coalesce(issue_date, current_date),
      due_date = coalesce(
        due_date,
        coalesce(issue_date, current_date) + customer.payment_terms_days
      ),
      subtotal = v_net_total,
      tax_total = v_tax_total,
      total_amount = v_gross_total,
      amount_outstanding = case when inv.document_type = 'invoice' then v_gross_total else 0 end,
      issuer_snapshot = jsonb_build_object(
        'name', coalesce(org.legal_name, org.name),
        'trading_name', org.trading_name,
        'registration_number', org.registration_number,
        'vat_number', org.vat_number,
        'address_line_1', org.physical_address_line_1,
        'address_line_2', org.physical_address_line_2,
        'city', org.physical_city,
        'province', org.physical_province,
        'postal_code', org.physical_postal_code,
        'country', coalesce(org.physical_country, org.country),
        'email', org.accounts_email,
        'phone', org.phone
      ),
      customer_snapshot = jsonb_build_object(
        'customer_code', customer.customer_code,
        'legal_name', customer.legal_name,
        'trading_name', customer.trading_name,
        'vat_number', customer.vat_number,
        'registration_number', customer.registration_number,
        'billing_address', customer.billing_address,
        'delivery_address', customer.delivery_address,
        'email', customer.default_email,
        'phone', customer.phone
      ),
      branding_snapshot = coalesce(branding, '{}'::jsonb),
      status = 'issued',
      issued_by = p_actor_user_id,
      issued_at = now(),
      gl_journal_id = journal_id,
      updated_by = p_actor_user_id,
      updated_at = now()
  where id = inv.id;

  insert into public.sales_invoice_audit_events(
    organisation_id, sales_invoice_id, event_type, actor_user_id, details
  ) values (
    p_org_id, inv.id, 'issued', p_actor_user_id,
    jsonb_build_object('journal_id', journal_id, 'total', v_gross_total)
  );

  if inv.document_type = 'credit_note' then
    perform public.refresh_sales_invoice_payment_status(inv.original_invoice_id);
  end if;

  return jsonb_build_object(
    'sales_invoice_id', inv.id,
    'journal_id', journal_id,
    'invoice_number', (select invoice_number from public.sales_invoices where id = inv.id),
    'total_debit', v_gross_total,
    'total_credit', v_gross_total
  );
end;
$$;

create or replace function public.create_sales_invoice_approval_request(
  p_org_id uuid,
  p_sales_invoice_id uuid,
  p_amount numeric,
  p_requested_by uuid default auth.uid()
)
returns uuid
language plpgsql security definer set search_path = public
as $$
declare
  wf public.approval_workflows%rowtype;
  req_id uuid;
  first_order integer;
begin
  if p_requested_by is null or p_requested_by is distinct from auth.uid() then
    raise exception 'Invalid approval requester';
  end if;
  if not public.has_org_role(p_org_id, array['owner','admin','accountant']::public.organisation_role[]) then
    raise exception 'Not authorised to submit sales invoices';
  end if;
  select * into wf from public.approval_workflows
  where organisation_id = p_org_id and workflow_type = 'sales_invoice' and active = true
    and exists (select 1 from public.approval_steps where workflow_id = approval_workflows.id)
  order by created_at limit 1;
  if not found then return null; end if;

  insert into public.approval_requests(
    organisation_id, workflow_id, workflow_type, source_table, source_id,
    amount, status, requested_by
  ) values (
    p_org_id, wf.id, 'sales_invoice', 'sales_invoices', p_sales_invoice_id,
    coalesce(p_amount, 0), 'pending', p_requested_by
  )
  on conflict (organisation_id, workflow_type, source_id)
  do update set amount = excluded.amount, workflow_id = excluded.workflow_id, updated_at = now()
  returning id into req_id;

  if not exists (select 1 from public.approval_request_steps where request_id = req_id) then
    insert into public.approval_request_steps(
      request_id, organisation_id, workflow_step_id, step_order, name,
      approver_user_id, approver_role, status, due_at
    )
    select req_id, p_org_id, s.id, s.step_order, s.name, s.approver_user_id, s.approver_role,
      case when s.step_order = min(s.step_order) over () then 'pending' else 'waiting' end,
      case when s.step_order = min(s.step_order) over ()
        then now() + make_interval(hours => s.due_in_hours) else null end
    from public.approval_steps s where s.workflow_id = wf.id;
    select min(step_order) into first_order
    from public.approval_request_steps where request_id = req_id;
    update public.approval_requests set current_step_order = first_order where id = req_id;
  end if;

  update public.sales_invoices
  set status = 'pending_approval', approval_request_id = req_id, updated_at = now()
  where id = p_sales_invoice_id and organisation_id = p_org_id and status = 'draft';
  return req_id;
end;
$$;

create or replace function public.post_customer_receipt_atomic(
  p_org_id uuid,
  p_customer_id uuid,
  p_bank_account_id uuid,
  p_receipt_date date,
  p_amount numeric,
  p_currency text,
  p_reference text,
  p_notes text,
  p_allocations jsonb default '[]'::jsonb,
  p_bank_statement_line_id uuid default null,
  p_idempotency_key text default null,
  p_actor_user_id uuid default auth.uid()
)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  bank_gl_id uuid;
  receivables_id uuid;
  receipt_id uuid;
  journal_id uuid;
  allocation jsonb;
  invoice_id uuid;
  allocation_amount numeric(14,2);
  allocation_total numeric(14,2) := 0;
  outstanding numeric(14,2);
begin
  if p_amount <= 0 then raise exception 'Receipt amount must be positive'; end if;
  if p_actor_user_id is null or p_actor_user_id is distinct from auth.uid() then
    raise exception 'Invalid receipt actor';
  end if;
  if not public.has_org_role(p_org_id, array['owner','admin','accountant']::public.organisation_role[]) then
    raise exception 'Not authorised to post customer receipts';
  end if;
  if not exists (
    select 1 from public.customers
    where id = p_customer_id and organisation_id = p_org_id
  ) then raise exception 'Customer not found'; end if;

  if p_idempotency_key is not null then
    select id, gl_journal_id into receipt_id, journal_id
    from public.customer_receipts
    where organisation_id = p_org_id and idempotency_key = p_idempotency_key;
    if found then
      return jsonb_build_object('receipt_id', receipt_id, 'journal_id', journal_id, 'idempotent', true);
    end if;
  end if;

  select gl_account_id into bank_gl_id from public.bank_accounts
  where id = p_bank_account_id and organisation_id = p_org_id;
  if bank_gl_id is null then raise exception 'Bank account needs a linked GL account'; end if;
  select id into receivables_id from public.accounts
  where organisation_id = p_org_id and system_key = 'trade_receivables';
  if receivables_id is null then raise exception 'Trade Receivables system account not found'; end if;

  for allocation in select value from jsonb_array_elements(coalesce(p_allocations, '[]'::jsonb))
  loop
    invoice_id := (allocation->>'sales_invoice_id')::uuid;
    allocation_amount := round((allocation->>'amount')::numeric, 2);
    select amount_outstanding into outstanding
    from public.sales_invoices
    where id = invoice_id and organisation_id = p_org_id
      and customer_id = p_customer_id and document_type = 'invoice' and status = 'issued'
    for update;
    if not found then raise exception 'Receipt allocation invoice not found'; end if;
    if allocation_amount <= 0 or allocation_amount > outstanding + 0.02 then
      raise exception 'Receipt allocation exceeds the invoice outstanding amount';
    end if;
    allocation_total := allocation_total + allocation_amount;
  end loop;
  if allocation_total > p_amount + 0.02 then
    raise exception 'Receipt allocations exceed the receipt amount';
  end if;

  insert into public.gl_journals(
    organisation_id, source_type, journal_date, description, status,
    total_debit, total_credit, created_by, posted_by, posted_at
  ) values (
    p_org_id, 'customer_receipt', p_receipt_date,
    coalesce(nullif(p_reference, ''), 'Customer receipt'), 'posted',
    p_amount, p_amount, p_actor_user_id, p_actor_user_id, now()
  ) returning id into journal_id;
  insert into public.gl_journal_lines(
    organisation_id, gl_journal_id, account_id, description,
    debit_amount, credit_amount, tracking, sort_order
  ) values
    (p_org_id, journal_id, bank_gl_id, coalesce(p_reference, 'Customer receipt'), p_amount, 0, '{}'::jsonb, 0),
    (p_org_id, journal_id, receivables_id, coalesce(p_reference, 'Customer receipt'), 0, p_amount, '{}'::jsonb, 1);

  insert into public.customer_receipts(
    organisation_id, customer_id, bank_account_id, bank_statement_line_id,
    receipt_date, amount, currency, reference, notes, idempotency_key,
    gl_journal_id, posted_by
  ) values (
    p_org_id, p_customer_id, p_bank_account_id, p_bank_statement_line_id,
    p_receipt_date, p_amount, p_currency, p_reference, p_notes, p_idempotency_key,
    journal_id, p_actor_user_id
  ) returning id into receipt_id;
  update public.gl_journals set source_id = receipt_id where id = journal_id;

  for allocation in select value from jsonb_array_elements(coalesce(p_allocations, '[]'::jsonb))
  loop
    invoice_id := (allocation->>'sales_invoice_id')::uuid;
    allocation_amount := round((allocation->>'amount')::numeric, 2);
    insert into public.customer_receipt_allocations(
      organisation_id, customer_receipt_id, sales_invoice_id, amount
    ) values (p_org_id, receipt_id, invoice_id, allocation_amount);
    perform public.refresh_sales_invoice_payment_status(invoice_id);
  end loop;

  if p_bank_statement_line_id is not null then
    update public.bank_statement_lines
    set matched_sales_invoice_id = case
          when jsonb_array_length(coalesce(p_allocations, '[]'::jsonb)) = 1
          then (p_allocations->0->>'sales_invoice_id')::uuid else null end,
        match_status = 'matched',
        allocation_status = 'allocated',
        review_status = 'reviewed',
        posting_status = 'posted',
        gl_journal_id = journal_id,
        reviewed_by = p_actor_user_id,
        reviewed_at = now()
    where id = p_bank_statement_line_id and organisation_id = p_org_id;
  end if;

  return jsonb_build_object(
    'receipt_id', receipt_id,
    'journal_id', journal_id,
    'allocated_amount', allocation_total,
    'unallocated_amount', round(p_amount - allocation_total, 2),
    'idempotent', false
  );
end;
$$;

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'customers','customer_contacts','sales_document_sequences','sales_invoices',
    'sales_invoice_lines','sales_invoice_delivery_events','sales_invoice_audit_events',
    'customer_receipts','customer_receipt_allocations'
  ]
  loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('revoke all privileges on table public.%I from public, anon', table_name);
    execute format('grant all privileges on table public.%I to service_role', table_name);
  end loop;
end $$;

-- Keep these declarations explicit as well as defensive/dynamic so security
-- scanners can verify every Data API table without executing the migration.
alter table public.customers enable row level security;
alter table public.customer_contacts enable row level security;
alter table public.sales_document_sequences enable row level security;
alter table public.sales_invoices enable row level security;
alter table public.sales_invoice_lines enable row level security;
alter table public.sales_invoice_delivery_events enable row level security;
alter table public.sales_invoice_audit_events enable row level security;
alter table public.customer_receipts enable row level security;
alter table public.customer_receipt_allocations enable row level security;

revoke all privileges on table public.customers from public, anon;
revoke all privileges on table public.customer_contacts from public, anon;
revoke all privileges on table public.sales_document_sequences from public, anon;
revoke all privileges on table public.sales_invoices from public, anon;
revoke all privileges on table public.sales_invoice_lines from public, anon;
revoke all privileges on table public.sales_invoice_delivery_events from public, anon;
revoke all privileges on table public.sales_invoice_audit_events from public, anon;
revoke all privileges on table public.customer_receipts from public, anon;
revoke all privileges on table public.customer_receipt_allocations from public, anon;

grant all privileges on table public.customers to service_role;
grant all privileges on table public.customer_contacts to service_role;
grant all privileges on table public.sales_document_sequences to service_role;
grant all privileges on table public.sales_invoices to service_role;
grant all privileges on table public.sales_invoice_lines to service_role;
grant all privileges on table public.sales_invoice_delivery_events to service_role;
grant all privileges on table public.sales_invoice_audit_events to service_role;
grant all privileges on table public.customer_receipts to service_role;
grant all privileges on table public.customer_receipt_allocations to service_role;

create policy "customers_select_member" on public.customers
  for select to authenticated using (public.is_org_member(organisation_id));
create policy "customers_write_accountants" on public.customers
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

create policy "customer_contacts_select_member" on public.customer_contacts
  for select to authenticated using (public.is_org_member(organisation_id));
create policy "customer_contacts_write_accountants" on public.customer_contacts
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

create policy "sales_sequences_select_member" on public.sales_document_sequences
  for select to authenticated using (public.is_org_member(organisation_id));

create policy "sales_invoices_select_member" on public.sales_invoices
  for select to authenticated using (public.is_org_member(organisation_id));
create policy "sales_invoices_write_accountants" on public.sales_invoices
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));
create policy "sales_invoices_approve_configured" on public.sales_invoices
  for update to authenticated
  using (
    public.is_org_member(organisation_id)
    and exists (
      select 1
      from public.approval_request_steps step
      join public.organisation_users member
        on member.organisation_id = sales_invoices.organisation_id
       and member.user_id = auth.uid()
       and member.status = 'active'
      where step.request_id = sales_invoices.approval_request_id
        and step.status = 'pending'
        and (
          step.approver_user_id = auth.uid()
          or step.approver_role = member.role
        )
    )
  )
  with check (public.is_org_member(organisation_id));

create policy "sales_invoice_lines_select_member" on public.sales_invoice_lines
  for select to authenticated using (public.is_org_member(organisation_id));
create policy "sales_invoice_lines_write_accountants" on public.sales_invoice_lines
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

create policy "sales_delivery_select_member" on public.sales_invoice_delivery_events
  for select to authenticated using (public.is_org_member(organisation_id));
create policy "sales_delivery_insert_accountants" on public.sales_invoice_delivery_events
  for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

create policy "sales_audit_select_member" on public.sales_invoice_audit_events
  for select to authenticated using (public.is_org_member(organisation_id));
create policy "sales_audit_insert_accountants" on public.sales_invoice_audit_events
  for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

create policy "customer_receipts_select_member" on public.customer_receipts
  for select to authenticated using (public.is_org_member(organisation_id));

create policy "customer_receipt_allocations_select_member" on public.customer_receipt_allocations
  for select to authenticated using (public.is_org_member(organisation_id));

grant select, insert, update, delete on table public.customers to authenticated;
grant select, insert, update, delete on table public.customer_contacts to authenticated;
grant select on table public.sales_document_sequences to authenticated;
grant select, insert, update on table public.sales_invoices to authenticated;
grant select, insert, update, delete on table public.sales_invoice_lines to authenticated;
grant select, insert on table public.sales_invoice_delivery_events to authenticated;
grant select, insert on table public.sales_invoice_audit_events to authenticated;
grant select on table public.customer_receipts to authenticated;
grant select on table public.customer_receipt_allocations to authenticated;

insert into storage.buckets(id, name, public, file_size_limit, allowed_mime_types)
values ('customer-documents', 'customer-documents', false, 10485760, array['application/pdf'])
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "customer_documents_select_member" on storage.objects;
create policy "customer_documents_select_member" on storage.objects
  for select to authenticated
  using (bucket_id = 'customer-documents' and public.is_org_member(public.storage_object_org_id(name)));
drop policy if exists "customer_documents_insert_accountants" on storage.objects;
create policy "customer_documents_insert_accountants" on storage.objects
  for insert to authenticated
  with check (
    bucket_id = 'customer-documents'
    and public.has_org_role(public.storage_object_org_id(name), array['owner','admin','accountant']::public.organisation_role[])
  );

create unique index if not exists gl_journals_one_active_sales_document_source
  on public.gl_journals(organisation_id, source_type, source_id)
  where source_type in ('sales_invoice','sales_credit_note','customer_receipt')
    and status <> 'reversed';

revoke all on function public.allocate_sales_document_number(uuid, text) from public;
revoke all on function public.issue_sales_invoice_atomic(uuid, uuid, uuid) from public;
revoke all on function public.create_sales_invoice_approval_request(uuid, uuid, numeric, uuid) from public;
revoke all on function public.post_customer_receipt_atomic(
  uuid, uuid, uuid, date, numeric, text, text, text, jsonb, uuid, text, uuid
) from public;
grant execute on function public.issue_sales_invoice_atomic(uuid, uuid, uuid) to authenticated, service_role;
grant execute on function public.create_sales_invoice_approval_request(uuid, uuid, numeric, uuid)
  to authenticated, service_role;
grant execute on function public.post_customer_receipt_atomic(
  uuid, uuid, uuid, date, numeric, text, text, text, jsonb, uuid, text, uuid
) to authenticated, service_role;

drop trigger if exists customers_set_updated_at on public.customers;
create trigger customers_set_updated_at before update on public.customers
  for each row execute function public.set_updated_at();
drop trigger if exists customer_contacts_set_updated_at on public.customer_contacts;
create trigger customer_contacts_set_updated_at before update on public.customer_contacts
  for each row execute function public.set_updated_at();
drop trigger if exists sales_invoices_set_updated_at on public.sales_invoices;
create trigger sales_invoices_set_updated_at before update on public.sales_invoices
  for each row execute function public.set_updated_at();
drop trigger if exists sales_invoice_lines_set_updated_at on public.sales_invoice_lines;
create trigger sales_invoice_lines_set_updated_at before update on public.sales_invoice_lines
  for each row execute function public.set_updated_at();
