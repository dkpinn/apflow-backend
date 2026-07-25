-- Atomically post a finance-lease payment and optional depreciation.

create unique index if not exists finance_lease_gl_postings_one_period_event
  on public.finance_lease_gl_postings(lease_id, event_type, period_number)
  where period_number is not null and event_type in ('payment', 'depreciation');

create or replace function public.post_lease_payment_atomic(
  p_org_id uuid,
  p_lease_id uuid,
  p_period_number integer,
  p_user_id uuid,
  p_journal_date date,
  p_payment_description text,
  p_payment_lines jsonb,
  p_post_depreciation boolean,
  p_depreciation_description text,
  p_depreciation_amount numeric,
  p_depreciation_lines jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_lease public.finance_leases%rowtype;
  v_schedule public.finance_lease_schedule%rowtype;
  v_payment_journal_id uuid := gen_random_uuid();
  v_depreciation_journal_id uuid;
  v_payment_debit numeric(14,2);
  v_payment_credit numeric(14,2);
  v_payment_line_count integer;
  v_expected_payment_line_count integer := 3;
  v_depreciation_debit numeric(14,2) := 0;
  v_depreciation_credit numeric(14,2) := 0;
  v_depreciation_line_count integer := 0;
  v_lease_status text;
begin
  if auth.role() is distinct from 'service_role' then
    if auth.uid() is null
       or p_user_id is distinct from auth.uid()
       or not public.has_org_role(
            p_org_id,
            array['owner','admin','accountant']::public.organisation_role[]
          )
    then
      raise exception 'Not authorised to post finance lease payment';
    end if;
  end if;

  if p_period_number is null or p_period_number < 1 then
    raise exception 'Finance lease period number must be positive';
  end if;
  if p_journal_date is null then
    raise exception 'Finance lease journal date is required';
  end if;

  select * into v_lease
  from public.finance_leases
  where id = p_lease_id and organisation_id = p_org_id
  for update;

  if not found then raise exception 'Finance lease not found'; end if;
  if v_lease.status <> 'active' then
    raise exception 'Only active leases can have payment journals posted';
  end if;

  select * into v_schedule
  from public.finance_lease_schedule
  where lease_id = p_lease_id
    and organisation_id = p_org_id
    and period_number = p_period_number
  for update;

  if not found then raise exception 'Finance lease period % not found', p_period_number; end if;
  if v_schedule.posted then
    raise exception 'Finance lease period % has already been posted', p_period_number;
  end if;

  if exists (
    select 1 from public.organisation_accounting_periods p
    where p.organisation_id = p_org_id
      and p_journal_date between p.period_start and p.period_end
      and (p.status in ('closed', 'locked')
           or (p.lock_date is not null and p_journal_date <= p.lock_date))
  ) then
    raise exception 'The accounting period for this finance lease payment is closed or locked';
  end if;

  if jsonb_typeof(p_payment_lines) is distinct from 'array' then
    raise exception 'Payment journal lines must be a JSON array';
  end if;
  select count(*),
         round(coalesce(sum((line->>'debit_amount')::numeric), 0), 2),
         round(coalesce(sum((line->>'credit_amount')::numeric), 0), 2)
    into v_payment_line_count, v_payment_debit, v_payment_credit
  from jsonb_array_elements(p_payment_lines) line;

  if v_payment_line_count < 2 or v_payment_debit <= 0 then
    raise exception 'Payment journal must contain at least two non-zero lines';
  end if;
  if v_payment_debit <> v_payment_credit then
    raise exception 'Payment journal does not balance (Dr % / Cr %)',
      v_payment_debit, v_payment_credit;
  end if;
  if exists (
    select 1 from jsonb_array_elements(p_payment_lines) line
    left join public.accounts a
      on a.id = (line->>'account_id')::uuid
     and a.organisation_id = p_org_id and a.active
    where a.id is null
  ) then
    raise exception 'Payment journal contains an invalid or cross-organisation account';
  end if;
  if v_schedule.additional_debit > 0 then
    v_expected_payment_line_count := v_expected_payment_line_count + 1;
  end if;
  if v_schedule.additional_credit > 0 then
    v_expected_payment_line_count := v_expected_payment_line_count + 1;
  end if;

  if v_payment_line_count <> v_expected_payment_line_count
     or not exists (
       select 1 from jsonb_array_elements(p_payment_lines) line
       where coalesce((line->>'sort_order')::integer, 0) = 0
         and (line->>'account_id')::uuid = v_lease.liability_lt_account_id
         and round(coalesce((line->>'debit_amount')::numeric, 0), 2) = round(v_schedule.principal_amount, 2)
         and round(coalesce((line->>'credit_amount')::numeric, 0), 2) = 0
     )
     or not exists (
       select 1 from jsonb_array_elements(p_payment_lines) line
       where coalesce((line->>'sort_order')::integer, 0) = 1
         and (line->>'account_id')::uuid = v_lease.interest_expense_account_id
         and round(coalesce((line->>'debit_amount')::numeric, 0), 2) = round(v_schedule.interest_amount, 2)
         and round(coalesce((line->>'credit_amount')::numeric, 0), 2) = 0
     )
     or not exists (
       select 1 from jsonb_array_elements(p_payment_lines) line
       where coalesce((line->>'sort_order')::integer, 0) = 2
         and round(coalesce((line->>'debit_amount')::numeric, 0), 2) = 0
         and round(coalesce((line->>'credit_amount')::numeric, 0), 2) = round(v_schedule.payment_amount, 2)
     )
  then
    raise exception 'Payment journal does not match the locked lease schedule';
  end if;
  if v_schedule.additional_debit > 0 and not exists (
    select 1 from jsonb_array_elements(p_payment_lines) line
    where coalesce((line->>'sort_order')::integer, 0) = 10
      and round(coalesce((line->>'debit_amount')::numeric, 0), 2) = round(v_schedule.additional_debit, 2)
      and round(coalesce((line->>'credit_amount')::numeric, 0), 2) = 0
  ) then
    raise exception 'Payment journal additional debit does not match the locked lease schedule';
  end if;
  if v_schedule.additional_credit > 0 and not exists (
    select 1 from jsonb_array_elements(p_payment_lines) line
    where coalesce((line->>'sort_order')::integer, 0) = 11
      and round(coalesce((line->>'debit_amount')::numeric, 0), 2) = 0
      and round(coalesce((line->>'credit_amount')::numeric, 0), 2) = round(v_schedule.additional_credit, 2)
  ) then
    raise exception 'Payment journal additional credit does not match the locked lease schedule';
  end if;
  if exists (
    select 1 from jsonb_array_elements(p_payment_lines) line
    where coalesce((line->>'sort_order')::integer, 0) in (2, 10, 11)
      and (line->>'account_id')::uuid is distinct from (
        select a.id from public.accounts a
        where a.id = (line->>'account_id')::uuid
          and a.organisation_id = p_org_id and a.active
      )
  ) then
    raise exception 'Payment journal bank account is invalid';
  end if;
  if (
    select count(distinct (line->>'account_id')::uuid)
    from jsonb_array_elements(p_payment_lines) line
    where coalesce((line->>'sort_order')::integer, 0) in (2, 10, 11)
  ) <> 1 then
    raise exception 'Payment journal must use one bank account';
  end if;

  if coalesce(p_post_depreciation, false) then
    if coalesce(p_depreciation_amount, 0) <= 0 then
      raise exception 'Depreciation amount must be positive';
    end if;
    if round(p_depreciation_amount, 2) <> round(
      v_lease.rou_asset_cost / nullif(v_lease.lease_term_months, 0), 2
    ) then
      raise exception 'Depreciation amount does not match the lease schedule';
    end if;
    if jsonb_typeof(p_depreciation_lines) is distinct from 'array' then
      raise exception 'Depreciation journal lines must be a JSON array';
    end if;
    select count(*),
           round(coalesce(sum((line->>'debit_amount')::numeric), 0), 2),
           round(coalesce(sum((line->>'credit_amount')::numeric), 0), 2)
      into v_depreciation_line_count, v_depreciation_debit, v_depreciation_credit
    from jsonb_array_elements(p_depreciation_lines) line;

    if v_depreciation_line_count < 2
       or v_depreciation_debit <> round(p_depreciation_amount, 2)
       or v_depreciation_debit <> v_depreciation_credit
    then
      raise exception 'Depreciation journal is invalid or unbalanced';
    end if;
    if exists (
      select 1 from jsonb_array_elements(p_depreciation_lines) line
      left join public.accounts a
        on a.id = (line->>'account_id')::uuid
       and a.organisation_id = p_org_id and a.active
      where a.id is null
    ) then
      raise exception 'Depreciation journal contains an invalid or cross-organisation account';
    end if;
    if v_depreciation_line_count <> 2
       or not exists (
         select 1 from jsonb_array_elements(p_depreciation_lines) line
         where coalesce((line->>'sort_order')::integer, 0) = 0
           and (line->>'account_id')::uuid = v_lease.depreciation_expense_account_id
           and round(coalesce((line->>'debit_amount')::numeric, 0), 2) = round(p_depreciation_amount, 2)
           and round(coalesce((line->>'credit_amount')::numeric, 0), 2) = 0
       )
       or not exists (
         select 1 from jsonb_array_elements(p_depreciation_lines) line
         where coalesce((line->>'sort_order')::integer, 0) = 1
           and (line->>'account_id')::uuid = v_lease.accum_depreciation_account_id
           and round(coalesce((line->>'debit_amount')::numeric, 0), 2) = 0
           and round(coalesce((line->>'credit_amount')::numeric, 0), 2) = round(p_depreciation_amount, 2)
       )
    then
      raise exception 'Depreciation journal does not match the lease accounts';
    end if;
  end if;

  insert into public.gl_journals (
    id, organisation_id, source_type, source_id, journal_date, description,
    status, total_debit, total_credit, created_by, posted_by, posted_at
  ) values (
    v_payment_journal_id, p_org_id, 'finance_lease', p_lease_id,
    p_journal_date, p_payment_description, 'posted',
    v_payment_debit, v_payment_credit, p_user_id, p_user_id, now()
  );

  insert into public.gl_journal_lines (
    organisation_id, gl_journal_id, account_id, description,
    debit_amount, credit_amount, tracking, sort_order
  )
  select p_org_id, v_payment_journal_id, (line->>'account_id')::uuid,
         line->>'description', coalesce((line->>'debit_amount')::numeric, 0),
         coalesce((line->>'credit_amount')::numeric, 0), '{}'::jsonb,
         coalesce((line->>'sort_order')::integer, 0)
  from jsonb_array_elements(p_payment_lines) line;

  insert into public.finance_lease_gl_postings (
    organisation_id, lease_id, journal_id, event_type, period_number,
    journal_date, description, amount, created_by
  ) values (
    p_org_id, p_lease_id, v_payment_journal_id, 'payment', p_period_number,
    p_journal_date, p_payment_description, v_payment_debit, p_user_id
  );

  if coalesce(p_post_depreciation, false) then
    v_depreciation_journal_id := gen_random_uuid();
    insert into public.gl_journals (
      id, organisation_id, source_type, source_id, journal_date, description,
      status, total_debit, total_credit, created_by, posted_by, posted_at
    ) values (
      v_depreciation_journal_id, p_org_id, 'finance_lease', p_lease_id,
      p_journal_date, p_depreciation_description, 'posted',
      v_depreciation_debit, v_depreciation_credit, p_user_id, p_user_id, now()
    );

    insert into public.gl_journal_lines (
      organisation_id, gl_journal_id, account_id, description,
      debit_amount, credit_amount, tracking, sort_order
    )
    select p_org_id, v_depreciation_journal_id, (line->>'account_id')::uuid,
           line->>'description', coalesce((line->>'debit_amount')::numeric, 0),
           coalesce((line->>'credit_amount')::numeric, 0), '{}'::jsonb,
           coalesce((line->>'sort_order')::integer, 0)
    from jsonb_array_elements(p_depreciation_lines) line;

    insert into public.finance_lease_gl_postings (
      organisation_id, lease_id, journal_id, event_type, period_number,
      journal_date, description, amount, created_by
    ) values (
      p_org_id, p_lease_id, v_depreciation_journal_id, 'depreciation', p_period_number,
      p_journal_date, p_depreciation_description, p_depreciation_amount, p_user_id
    );
  end if;

  update public.finance_lease_schedule
  set posted = true, journal_id = v_payment_journal_id,
      posted_at = now(), posted_by = p_user_id
  where id = v_schedule.id;

  update public.finance_leases
  set current_liability_balance = greatest(
        round(current_liability_balance - v_schedule.principal_amount, 2), 0),
      current_rou_net_book_value = case
        when coalesce(p_post_depreciation, false) then greatest(
          round(current_rou_net_book_value - p_depreciation_amount, 2), 0)
        else current_rou_net_book_value end,
      last_posted_period = greatest(coalesce(last_posted_period, 0), p_period_number),
      status = case when not exists (
        select 1 from public.finance_lease_schedule s
        where s.lease_id = p_lease_id and s.organisation_id = p_org_id and not s.posted
      ) then 'completed' else status end,
      updated_by = p_user_id, updated_at = now()
  where id = p_lease_id and organisation_id = p_org_id
  returning status into v_lease_status;

  return jsonb_build_object(
    'payment_journal_id', v_payment_journal_id,
    'depreciation_journal_id', v_depreciation_journal_id,
    'period_number', p_period_number,
    'lease_status', v_lease_status
  );
end;
$$;

revoke all on function public.post_lease_payment_atomic(
  uuid,uuid,integer,uuid,date,text,jsonb,boolean,text,numeric,jsonb
) from public;
grant execute on function public.post_lease_payment_atomic(
  uuid,uuid,integer,uuid,date,text,jsonb,boolean,text,numeric,jsonb
) to authenticated, service_role;
