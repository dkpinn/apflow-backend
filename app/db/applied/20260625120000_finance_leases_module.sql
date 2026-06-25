-- Finance Leases module: IFRS 16 sub-ledger with amortization schedule,
-- GL posting events, document upload, and period-end ST/LT reclassification.

-- ── 1. finance_leases (header) ────────────────────────────────────────────────
create table if not exists public.finance_leases (
  id                            uuid primary key default gen_random_uuid(),
  organisation_id               uuid not null references public.organisations(id) on delete cascade,

  lessor_name                   text not null,
  asset_description             text not null,
  reference_number              text,

  commencement_date             date not null,
  end_date                      date not null,
  lease_term_months             integer not null generated always as (
                                  (
                                    (date_part('year',  end_date) - date_part('year',  commencement_date)) * 12 +
                                    (date_part('month', end_date) - date_part('month', commencement_date))
                                  )::integer
                                ) stored,

  payment_amount                numeric(14,2) not null check (payment_amount > 0),
  payment_frequency             text not null default 'monthly'
                                  check (payment_frequency in ('monthly','quarterly','semi_annual','annual')),
  annual_interest_rate          numeric(7,4)  not null check (annual_interest_rate > 0),

  initial_liability             numeric(14,2) not null check (initial_liability >= 0),
  documentation_fees            numeric(14,2) not null default 0 check (documentation_fees >= 0),
  rou_asset_cost                numeric(14,2) not null check (rou_asset_cost >= 0),

  -- 6 user-selected GL accounts
  rou_asset_account_id          uuid references public.accounts(id) on delete restrict,
  accum_depreciation_account_id uuid references public.accounts(id) on delete restrict,
  depreciation_expense_account_id uuid references public.accounts(id) on delete restrict,
  interest_expense_account_id   uuid references public.accounts(id) on delete restrict,
  liability_lt_account_id       uuid references public.accounts(id) on delete restrict,
  liability_st_account_id       uuid references public.accounts(id) on delete restrict,

  -- Running balances kept current after each posting
  current_liability_balance     numeric(14,2) not null default 0,
  current_rou_net_book_value    numeric(14,2) not null default 0,
  last_posted_period            integer,

  status                        text not null default 'draft'
                                  check (status in ('draft','active','completed','cancelled')),
  inception_posted_at           timestamptz,
  inception_journal_id          uuid references public.gl_journals(id) on delete set null,

  notes                         text,

  active                        boolean not null default true,
  archived_at                   timestamptz,
  archived_by                   uuid references auth.users(id) on delete set null,

  created_by                    uuid references auth.users(id) on delete set null,
  updated_by                    uuid references auth.users(id) on delete set null,
  created_at                    timestamptz not null default now(),
  updated_at                    timestamptz not null default now(),

  constraint finance_leases_dates_check check (end_date > commencement_date),
  constraint finance_leases_archive_state_check check (
    (active and archived_at is null) or (not active and archived_at is not null)
  )
);

create index if not exists finance_leases_org_status_idx
  on public.finance_leases(organisation_id, status, active, commencement_date desc);


-- ── 2. finance_lease_schedule (amortization rows) ────────────────────────────
create table if not exists public.finance_lease_schedule (
  id              uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  lease_id        uuid not null references public.finance_leases(id) on delete cascade,

  period_number   integer       not null,
  period_date     date          not null,
  opening_balance numeric(14,2) not null,
  payment_amount  numeric(14,2) not null,
  interest_amount numeric(14,2) not null,
  principal_amount numeric(14,2) not null,
  closing_balance numeric(14,2) not null,

  additional_debit  numeric(14,2) not null default 0 check (additional_debit  >= 0),
  additional_credit numeric(14,2) not null default 0 check (additional_credit >= 0),
  notes             text,

  posted          boolean     not null default false,
  journal_id      uuid        references public.gl_journals(id) on delete set null,
  posted_at       timestamptz,
  posted_by       uuid        references auth.users(id) on delete set null,

  unique (lease_id, period_number)
);

create index if not exists finance_lease_schedule_lease_period_idx
  on public.finance_lease_schedule(lease_id, period_number);

create index if not exists finance_lease_schedule_org_date_idx
  on public.finance_lease_schedule(organisation_id, period_date, posted);


-- ── 3. finance_lease_documents ───────────────────────────────────────────────
create table if not exists public.finance_lease_documents (
  id              uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  lease_id        uuid references public.finance_leases(id) on delete cascade,

  original_filename  text   not null,
  mime_type          text   not null,
  storage_bucket     text   not null default 'finance-lease-docs',
  storage_path       text   not null,
  file_size_bytes    bigint,

  extraction_status  text not null default 'uploaded'
                       check (extraction_status in ('uploaded','processing','completed','failed')),
  extracted_data     jsonb,
  extraction_error   text,
  extraction_model   text,

  uploaded_by  uuid references auth.users(id) on delete set null,
  created_at   timestamptz not null default now()
);

create index if not exists finance_lease_documents_lease_idx
  on public.finance_lease_documents(lease_id, created_at desc);


-- ── 4. finance_lease_gl_postings (event audit log) ──────────────────────────
create table if not exists public.finance_lease_gl_postings (
  id              uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  lease_id        uuid not null references public.finance_leases(id) on delete cascade,
  journal_id      uuid not null references public.gl_journals(id) on delete restrict,

  event_type      text not null check (event_type in (
                    'inception',
                    'payment',
                    'depreciation',
                    'period_end_reclassification',
                    'reversal'
                  )),
  period_number   integer,
  journal_date    date not null,
  description     text,
  amount          numeric(14,2),

  created_by  uuid references auth.users(id) on delete set null,
  created_at  timestamptz not null default now()
);

create index if not exists finance_lease_gl_postings_lease_idx
  on public.finance_lease_gl_postings(lease_id, event_type, journal_date desc);


-- ── 5. RLS (identical pattern to inventory module) ───────────────────────────
do $$
declare t text;
begin
  foreach t in array array[
    'finance_leases',
    'finance_lease_schedule',
    'finance_lease_documents',
    'finance_lease_gl_postings'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all privileges on table public.%I from public, anon', t);
    execute format('grant all privileges on table public.%I to service_role', t);
    execute format(
      'create policy %I on public.%I for select to authenticated
       using (public.is_org_member(organisation_id))',
      t || '_select_member', t
    );
    execute format(
      'create policy %I on public.%I for all to authenticated
       using (public.has_org_role(organisation_id,
         array[''owner'',''admin'',''accountant'']::public.organisation_role[]))
       with check (public.has_org_role(organisation_id,
         array[''owner'',''admin'',''accountant'']::public.organisation_role[]))',
      t || '_write_accountants', t
    );
  end loop;
exception when duplicate_object then null;
end $$;


-- ── 6. SECURITY DEFINER: post_lease_inception_atomic ─────────────────────────
create or replace function public.post_lease_inception_atomic(
  p_org_id       uuid,
  p_lease_id     uuid,
  p_user_id      uuid,
  p_journal_date date,
  p_description  text,
  p_lines        jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_lease        public.finance_leases%rowtype;
  v_journal_id   uuid := gen_random_uuid();
  v_debit_total  numeric(14,2);
  v_credit_total numeric(14,2);
  v_line_count   integer;
begin
  -- Auth: service_role bypasses; authenticated users need accountant+
  if auth.role() is distinct from 'service_role' then
    if auth.uid() is null
       or p_user_id is distinct from auth.uid()
       or not public.has_org_role(p_org_id,
            array['owner','admin','accountant']::public.organisation_role[])
    then
      raise exception 'Not authorised to post finance lease inception';
    end if;
  end if;

  select * into v_lease
  from public.finance_leases
  where id = p_lease_id and organisation_id = p_org_id
  for update;

  if not found then
    raise exception 'Finance lease not found';
  end if;
  if v_lease.status <> 'draft' then
    raise exception 'Lease has already been activated (status: %)', v_lease.status;
  end if;
  if v_lease.inception_posted_at is not null then
    raise exception 'Inception journal has already been posted';
  end if;
  if v_lease.rou_asset_account_id is null or v_lease.liability_lt_account_id is null then
    raise exception 'GL accounts must be set before posting inception';
  end if;

  -- Compute and validate line totals
  select
    count(*),
    round(coalesce(sum((l->>'debit_amount')::numeric),  0), 2),
    round(coalesce(sum((l->>'credit_amount')::numeric), 0), 2)
  into v_line_count, v_debit_total, v_credit_total
  from jsonb_array_elements(p_lines) as l;

  if v_line_count < 2 then
    raise exception 'Inception journal must have at least 2 lines';
  end if;
  if v_debit_total <> v_credit_total then
    raise exception 'Inception journal does not balance (Dr % ≠ Cr %)',
      v_debit_total, v_credit_total;
  end if;

  -- GL journal header
  insert into public.gl_journals (
    id, organisation_id, source_type, source_id,
    journal_date, description, status,
    total_debit, total_credit,
    created_by, posted_by, posted_at
  ) values (
    v_journal_id, p_org_id, 'finance_lease', p_lease_id,
    coalesce(p_journal_date, current_date),
    coalesce(p_description, 'Finance Lease Inception – ' || v_lease.lessor_name),
    'posted',
    v_debit_total, v_credit_total,
    p_user_id, p_user_id, now()
  );

  -- GL journal lines from JSON
  insert into public.gl_journal_lines (
    organisation_id, gl_journal_id, account_id, description,
    debit_amount, credit_amount, tracking, sort_order
  )
  select
    p_org_id,
    v_journal_id,
    (l->>'account_id')::uuid,
    l->>'description',
    coalesce((l->>'debit_amount')::numeric,  0),
    coalesce((l->>'credit_amount')::numeric, 0),
    '{}'::jsonb,
    coalesce((l->>'sort_order')::integer, 0)
  from jsonb_array_elements(p_lines) as l;

  -- Event log
  insert into public.finance_lease_gl_postings (
    organisation_id, lease_id, journal_id,
    event_type, journal_date, description, amount, created_by
  ) values (
    p_org_id, p_lease_id, v_journal_id,
    'inception',
    coalesce(p_journal_date, current_date),
    p_description,
    v_debit_total,
    p_user_id
  );

  -- Activate lease and set initial balances
  update public.finance_leases set
    status                    = 'active',
    inception_posted_at       = now(),
    inception_journal_id      = v_journal_id,
    current_liability_balance = initial_liability,
    current_rou_net_book_value = rou_asset_cost,
    updated_by                = p_user_id,
    updated_at                = now()
  where id = p_lease_id and organisation_id = p_org_id;

  return jsonb_build_object(
    'journal_id',   v_journal_id,
    'total_debit',  v_debit_total,
    'total_credit', v_credit_total,
    'lines',        v_line_count
  );
end;
$$;

revoke all on function public.post_lease_inception_atomic(uuid,uuid,uuid,date,text,jsonb) from public;
grant execute on function public.post_lease_inception_atomic(uuid,uuid,uuid,date,text,jsonb)
  to service_role, authenticated;


-- ── 7. SECURITY DEFINER: post_lease_period_end_reclassification_atomic ───────
create or replace function public.post_lease_period_end_reclassification_atomic(
  p_org_id          uuid,
  p_lease_id        uuid,
  p_user_id         uuid,
  p_period_end_date date,
  p_description     text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_lease      public.finance_leases%rowtype;
  v_journal_id uuid := gen_random_uuid();
  v_st_amount  numeric(14,2);
  v_cutoff     date;
begin
  if auth.role() is distinct from 'service_role' then
    if auth.uid() is null
       or p_user_id is distinct from auth.uid()
       or not public.has_org_role(p_org_id,
            array['owner','admin','accountant']::public.organisation_role[])
    then
      raise exception 'Not authorised to post period-end reclassification';
    end if;
  end if;

  select * into v_lease
  from public.finance_leases
  where id = p_lease_id and organisation_id = p_org_id
  for update;

  if not found then
    raise exception 'Finance lease not found';
  end if;
  if v_lease.status <> 'active' then
    raise exception 'Only active leases can be reclassified (status: %)', v_lease.status;
  end if;
  if v_lease.liability_lt_account_id is null or v_lease.liability_st_account_id is null then
    raise exception 'Both liability accounts must be set for reclassification';
  end if;

  v_cutoff := p_period_end_date + interval '12 months';

  select coalesce(sum(principal_amount), 0)
  into v_st_amount
  from public.finance_lease_schedule
  where lease_id        = p_lease_id
    and organisation_id = p_org_id
    and not posted
    and period_date     >  p_period_end_date
    and period_date     <= v_cutoff;

  if v_st_amount <= 0 then
    raise exception 'No unposted principal within 12 months of period-end % to reclassify',
      p_period_end_date;
  end if;

  -- Reclassification: Dr Lease Liability LT / Cr Lease Liability ST
  insert into public.gl_journals (
    id, organisation_id, source_type, source_id,
    journal_date, description, status,
    total_debit, total_credit,
    created_by, posted_by, posted_at
  ) values (
    v_journal_id, p_org_id, 'finance_lease', p_lease_id,
    p_period_end_date,
    coalesce(p_description, 'Finance Lease ST/LT reclassification – ' || v_lease.asset_description),
    'posted',
    v_st_amount, v_st_amount,
    p_user_id, p_user_id, now()
  );

  insert into public.gl_journal_lines (
    organisation_id, gl_journal_id, account_id, description,
    debit_amount, credit_amount, tracking, sort_order
  ) values
    (p_org_id, v_journal_id, v_lease.liability_lt_account_id,
     'Reclassify to current portion – ' || v_lease.asset_description,
     v_st_amount, 0, '{}'::jsonb, 0),
    (p_org_id, v_journal_id, v_lease.liability_st_account_id,
     'Current portion of lease liability – ' || v_lease.asset_description,
     0, v_st_amount, '{}'::jsonb, 1);

  insert into public.finance_lease_gl_postings (
    organisation_id, lease_id, journal_id,
    event_type, journal_date, description, amount, created_by
  ) values (
    p_org_id, p_lease_id, v_journal_id,
    'period_end_reclassification',
    p_period_end_date,
    p_description,
    v_st_amount,
    p_user_id
  );

  return jsonb_build_object(
    'journal_id',        v_journal_id,
    'short_term_amount', v_st_amount,
    'period_end_date',   p_period_end_date,
    'cutoff_date',       v_cutoff
  );
end;
$$;

revoke all on function public.post_lease_period_end_reclassification_atomic(uuid,uuid,uuid,date,text) from public;
grant execute on function public.post_lease_period_end_reclassification_atomic(uuid,uuid,uuid,date,text)
  to service_role, authenticated;


-- ── 8. updated_at trigger for finance_leases ─────────────────────────────────
create or replace function public.finance_leases_set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists finance_leases_updated_at on public.finance_leases;
create trigger finance_leases_updated_at
  before update on public.finance_leases
  for each row execute function public.finance_leases_set_updated_at();
