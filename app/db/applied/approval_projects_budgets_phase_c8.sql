-- Approval rules, user limits, delegation, projects, and budgets.
-- Phase C8. Apply manually in Supabase. Idempotent.

create table if not exists public.approval_workflows (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  workflow_type text not null default 'invoice' check (workflow_type in ('invoice')),
  name text not null default 'Invoice approval',
  description text,
  active boolean not null default true,
  created_by uuid references auth.users(id),
  updated_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.approval_steps (
  id uuid primary key default gen_random_uuid(),
  workflow_id uuid not null references public.approval_workflows(id) on delete cascade,
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  step_order integer not null check (step_order > 0),
  name text not null,
  approver_user_id uuid references auth.users(id) on delete set null,
  approver_role public.organisation_role,
  due_in_hours integer not null default 24 check (due_in_hours >= 0),
  is_final_step boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint approval_steps_approver_check check (approver_user_id is not null or approver_role is not null),
  constraint approval_steps_order_uidx unique (workflow_id, step_order)
);

create table if not exists public.approval_rules (
  id uuid primary key default gen_random_uuid(),
  workflow_id uuid not null references public.approval_workflows(id) on delete cascade,
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  name text not null default 'Default rule',
  active boolean not null default true,
  min_amount numeric(14, 2),
  max_amount numeric(14, 2),
  account_ids uuid[] not null default '{}'::uuid[],
  tracking_filters jsonb not null default '{}'::jsonb,
  priority integer not null default 100,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint approval_rules_amount_check check (
    min_amount is null or max_amount is null or min_amount <= max_amount
  )
);

create table if not exists public.approval_requests (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  workflow_id uuid references public.approval_workflows(id) on delete set null,
  workflow_type text not null default 'invoice' check (workflow_type in ('invoice')),
  source_table text not null default 'invoices_extracted',
  source_id uuid not null,
  amount numeric(14, 2) not null default 0,
  status text not null default 'pending' check (
    status in ('pending','approved','rejected','cancelled')
  ),
  requested_by uuid references auth.users(id),
  requested_at timestamptz not null default now(),
  completed_at timestamptz,
  current_step_order integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint approval_requests_source_uidx unique (organisation_id, workflow_type, source_id)
);

create table if not exists public.approval_request_steps (
  id uuid primary key default gen_random_uuid(),
  request_id uuid not null references public.approval_requests(id) on delete cascade,
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  workflow_step_id uuid references public.approval_steps(id) on delete set null,
  step_order integer not null check (step_order > 0),
  name text not null,
  approver_user_id uuid references auth.users(id) on delete set null,
  approver_role public.organisation_role,
  delegated_from_user_id uuid references auth.users(id) on delete set null,
  status text not null default 'waiting' check (
    status in ('waiting','pending','included','approved','rejected','skipped')
  ),
  due_at timestamptz,
  included_at timestamptz,
  actioned_by uuid references auth.users(id),
  actioned_at timestamptz,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint approval_request_steps_order_uidx unique (request_id, step_order)
);

create table if not exists public.approval_delegations (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  from_user_id uuid not null references auth.users(id) on delete cascade,
  to_user_id uuid not null references auth.users(id) on delete cascade,
  workflow_type text not null default 'invoice' check (workflow_type in ('invoice')),
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  reason text,
  active boolean not null default true,
  created_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint approval_delegations_dates_check check (starts_at < ends_at),
  constraint approval_delegations_users_check check (from_user_id <> to_user_id)
);

create table if not exists public.organisation_user_account_limits (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  account_id uuid references public.accounts(id) on delete cascade,
  max_post_amount numeric(14, 2),
  max_approval_amount numeric(14, 2),
  can_post boolean not null default true,
  can_approve boolean not null default true,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint user_account_limits_amount_check check (
    max_post_amount is null or max_post_amount >= 0
  ),
  constraint user_account_limits_approval_amount_check check (
    max_approval_amount is null or max_approval_amount >= 0
  )
);

create table if not exists public.organisation_user_tracking_limits (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  tracking_dimension_id uuid not null references public.tracking_dimensions(id) on delete cascade,
  tracking_value_id uuid references public.tracking_values(id) on delete cascade,
  can_post boolean not null default true,
  can_approve boolean not null default true,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.organisation_projects (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  name text not null,
  code text,
  status text not null default 'planned' check (
    status in ('planned','active','on_hold','completed','cancelled')
  ),
  owner_user_id uuid references auth.users(id) on delete set null,
  starts_on date,
  ends_on date,
  goals text,
  notes text,
  created_by uuid references auth.users(id),
  updated_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint organisation_projects_dates_check check (
    starts_on is null or ends_on is null or starts_on <= ends_on
  ),
  constraint organisation_projects_org_code_uidx unique (organisation_id, code)
);

create table if not exists public.project_milestones (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  project_id uuid not null references public.organisation_projects(id) on delete cascade,
  name text not null,
  status text not null default 'planned' check (
    status in ('planned','in_progress','done','blocked','cancelled')
  ),
  owner_user_id uuid references auth.users(id) on delete set null,
  due_on date,
  completed_on date,
  cost_target numeric(14, 2),
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.project_budgets (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  project_id uuid not null references public.organisation_projects(id) on delete cascade,
  account_id uuid references public.accounts(id) on delete set null,
  tracking_value_id uuid references public.tracking_values(id) on delete set null,
  period_start date,
  period_end date,
  amount numeric(14, 2) not null default 0 check (amount >= 0),
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint project_budgets_period_check check (
    period_start is null or period_end is null or period_start <= period_end
  )
);

alter table public.invoices_extracted
  add column if not exists approval_request_id uuid references public.approval_requests(id) on delete set null;

create index if not exists approval_workflows_org_idx
  on public.approval_workflows(organisation_id, workflow_type, active);
create index if not exists approval_steps_workflow_idx
  on public.approval_steps(workflow_id, step_order);
create index if not exists approval_rules_workflow_idx
  on public.approval_rules(workflow_id, active, priority);
create index if not exists approval_requests_org_status_idx
  on public.approval_requests(organisation_id, workflow_type, status);
create index if not exists approval_request_steps_pending_idx
  on public.approval_request_steps(organisation_id, status, due_at);
create index if not exists approval_delegations_lookup_idx
  on public.approval_delegations(organisation_id, workflow_type, from_user_id, starts_at, ends_at)
  where active = true;
create index if not exists user_account_limits_lookup_idx
  on public.organisation_user_account_limits(organisation_id, user_id, account_id, active);
create index if not exists user_tracking_limits_lookup_idx
  on public.organisation_user_tracking_limits(organisation_id, user_id, tracking_dimension_id, tracking_value_id, active);
create index if not exists organisation_projects_org_status_idx
  on public.organisation_projects(organisation_id, status);
create index if not exists project_milestones_project_idx
  on public.project_milestones(project_id, status, due_on);
create index if not exists project_budgets_project_idx
  on public.project_budgets(project_id, account_id);

drop trigger if exists approval_workflows_set_updated_at on public.approval_workflows;
create trigger approval_workflows_set_updated_at
  before update on public.approval_workflows
  for each row execute function public.set_updated_at();

drop trigger if exists approval_steps_set_updated_at on public.approval_steps;
create trigger approval_steps_set_updated_at
  before update on public.approval_steps
  for each row execute function public.set_updated_at();

drop trigger if exists approval_rules_set_updated_at on public.approval_rules;
create trigger approval_rules_set_updated_at
  before update on public.approval_rules
  for each row execute function public.set_updated_at();

drop trigger if exists approval_requests_set_updated_at on public.approval_requests;
create trigger approval_requests_set_updated_at
  before update on public.approval_requests
  for each row execute function public.set_updated_at();

drop trigger if exists approval_request_steps_set_updated_at on public.approval_request_steps;
create trigger approval_request_steps_set_updated_at
  before update on public.approval_request_steps
  for each row execute function public.set_updated_at();

drop trigger if exists approval_delegations_set_updated_at on public.approval_delegations;
create trigger approval_delegations_set_updated_at
  before update on public.approval_delegations
  for each row execute function public.set_updated_at();

drop trigger if exists user_account_limits_set_updated_at on public.organisation_user_account_limits;
create trigger user_account_limits_set_updated_at
  before update on public.organisation_user_account_limits
  for each row execute function public.set_updated_at();

drop trigger if exists user_tracking_limits_set_updated_at on public.organisation_user_tracking_limits;
create trigger user_tracking_limits_set_updated_at
  before update on public.organisation_user_tracking_limits
  for each row execute function public.set_updated_at();

drop trigger if exists organisation_projects_set_updated_at on public.organisation_projects;
create trigger organisation_projects_set_updated_at
  before update on public.organisation_projects
  for each row execute function public.set_updated_at();

drop trigger if exists project_milestones_set_updated_at on public.project_milestones;
create trigger project_milestones_set_updated_at
  before update on public.project_milestones
  for each row execute function public.set_updated_at();

drop trigger if exists project_budgets_set_updated_at on public.project_budgets;
create trigger project_budgets_set_updated_at
  before update on public.project_budgets
  for each row execute function public.set_updated_at();

alter table public.approval_workflows enable row level security;
alter table public.approval_steps enable row level security;
alter table public.approval_rules enable row level security;
alter table public.approval_requests enable row level security;
alter table public.approval_request_steps enable row level security;
alter table public.approval_delegations enable row level security;
alter table public.organisation_user_account_limits enable row level security;
alter table public.organisation_user_tracking_limits enable row level security;
alter table public.organisation_projects enable row level security;
alter table public.project_milestones enable row level security;
alter table public.project_budgets enable row level security;

do $$
declare
  t text;
begin
  foreach t in array array[
    'approval_workflows',
    'approval_steps',
    'approval_rules',
    'approval_requests',
    'approval_request_steps',
    'approval_delegations',
    'organisation_user_account_limits',
    'organisation_user_tracking_limits',
    'organisation_projects',
    'project_milestones',
    'project_budgets'
  ]
  loop
    execute format('drop policy if exists "%s_select_member" on public.%I', t, t);
    execute format(
      'create policy "%s_select_member" on public.%I for select to authenticated using (public.is_org_member(organisation_id))',
      t, t
    );

    execute format('drop policy if exists "%s_write_admin" on public.%I', t, t);
    execute format(
      'create policy "%s_write_admin" on public.%I for all to authenticated using (public.has_org_role(organisation_id, array[''owner'',''admin'']::public.organisation_role[])) with check (public.has_org_role(organisation_id, array[''owner'',''admin'']::public.organisation_role[]))',
      t, t
    );
  end loop;
end $$;

create or replace function public.approval_effective_user(
  p_org_id uuid,
  p_workflow_type text,
  p_approver_user_id uuid
)
returns uuid
language sql stable security definer set search_path = public
as $$
  select coalesce(
    (
      select d.to_user_id
      from public.approval_delegations d
      where d.organisation_id = p_org_id
        and d.workflow_type = p_workflow_type
        and d.from_user_id = p_approver_user_id
        and d.active = true
        and now() >= d.starts_at
        and now() <= d.ends_at
      order by d.created_at desc
      limit 1
    ),
    p_approver_user_id
  );
$$;

create or replace function public.refresh_approval_request(p_request_id uuid)
returns void
language plpgsql security definer set search_path = public
as $$
declare
  req record;
  overdue_step record;
  next_step record;
begin
  select * into req
  from public.approval_requests
  where id = p_request_id
    and status = 'pending';

  if not found then
    return;
  end if;

  for overdue_step in
    select *
    from public.approval_request_steps
    where request_id = p_request_id
      and status = 'pending'
      and due_at is not null
      and due_at < now()
    order by step_order
  loop
    if exists (
      select 1
      from public.approval_steps s
      where s.id = overdue_step.workflow_step_id
        and s.is_final_step = true
    ) then
      continue;
    end if;

    select * into next_step
    from public.approval_request_steps
    where request_id = p_request_id
      and step_order > overdue_step.step_order
      and status = 'waiting'
    order by step_order
    limit 1;

    if found then
      update public.approval_request_steps
      set status = 'included',
          included_at = coalesce(included_at, now()),
          due_at = coalesce(due_at, now() + interval '24 hours')
      where id = next_step.id;

      update public.approval_requests
      set current_step_order = least(coalesce(current_step_order, overdue_step.step_order), overdue_step.step_order)
      where id = p_request_id;
    end if;
  end loop;
end;
$$;

create or replace function public.create_invoice_approval_request(
  p_org_id uuid,
  p_invoice_id uuid,
  p_amount numeric,
  p_requested_by uuid default auth.uid()
)
returns uuid
language plpgsql security definer set search_path = public
as $$
declare
  wf record;
  req_id uuid;
  first_order integer;
begin
  select w.* into wf
  from public.approval_workflows w
  where w.organisation_id = p_org_id
    and w.workflow_type = 'invoice'
    and w.active = true
    and exists (
      select 1 from public.approval_steps s where s.workflow_id = w.id
    )
    and (
      not exists (
        select 1 from public.approval_rules r
        where r.workflow_id = w.id
          and r.active = true
      )
      or exists (
        select 1 from public.approval_rules r
        where r.workflow_id = w.id
          and r.active = true
          and (r.min_amount is null or p_amount >= r.min_amount)
          and (r.max_amount is null or p_amount <= r.max_amount)
      )
    )
  order by w.created_at
  limit 1;

  if not found then
    return null;
  end if;

  insert into public.approval_requests (
    organisation_id, workflow_id, workflow_type, source_table, source_id,
    amount, status, requested_by, current_step_order
  )
  values (
    p_org_id, wf.id, 'invoice', 'invoices_extracted', p_invoice_id,
    coalesce(p_amount, 0), 'pending', p_requested_by, null
  )
  on conflict (organisation_id, workflow_type, source_id)
  do update set
    amount = excluded.amount,
    workflow_id = excluded.workflow_id,
    updated_at = now()
  returning id into req_id;

  if not exists (select 1 from public.approval_request_steps where request_id = req_id) then
    insert into public.approval_request_steps (
      request_id, organisation_id, workflow_step_id, step_order, name,
      approver_user_id, approver_role, status, due_at
    )
    select
      req_id,
      s.organisation_id,
      s.id,
      s.step_order,
      s.name,
      s.approver_user_id,
      s.approver_role,
      case when s.step_order = (select min(s2.step_order) from public.approval_steps s2 where s2.workflow_id = wf.id)
        then 'pending'
        else 'waiting'
      end,
      case when s.step_order = (select min(s2.step_order) from public.approval_steps s2 where s2.workflow_id = wf.id)
        then now() + make_interval(hours => s.due_in_hours)
        else null
      end
    from public.approval_steps s
    where s.workflow_id = wf.id
    order by s.step_order;

    select min(step_order) into first_order
    from public.approval_request_steps
    where request_id = req_id;

    update public.approval_requests
    set current_step_order = first_order
    where id = req_id;
  end if;

  update public.invoices_extracted
  set approval_request_id = req_id,
      approval_status = 'pending'
  where id = p_invoice_id
    and organisation_id = p_org_id;

  return req_id;
end;
$$;

create or replace view public.project_budget_actuals_view as
select
  p.organisation_id,
  p.id as project_id,
  case
    when a.expense_account ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then a.expense_account::uuid
    else null
  end as account_id,
  case
    when (a.tracking ->> 'project_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then (a.tracking ->> 'project_id')::uuid
    else null
  end as tracking_project_id,
  coalesce(sum(a.amount), 0)::numeric(14, 2) as invoice_actual_amount,
  0::numeric(14, 2) as bank_actual_amount
from public.organisation_projects p
join public.invoice_line_item_allocations a
  on case
    when (a.tracking ->> 'project_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then (a.tracking ->> 'project_id')::uuid
    else null
  end = p.id
group by
  p.organisation_id,
  p.id,
  case
    when a.expense_account ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then a.expense_account::uuid
    else null
  end,
  case
    when (a.tracking ->> 'project_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then (a.tracking ->> 'project_id')::uuid
    else null
  end;

grant select on public.project_budget_actuals_view to authenticated;

select 'approval_projects_budgets_phase_c8_applied' as migration_note;
