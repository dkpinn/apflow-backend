-- Phase B32: Add invoice_approver and permissions columns to organisation_users
-- Fixes the Users & Access page which queries these columns but they don't exist in the schema.

alter table public.organisation_users
  add column if not exists invoice_approver boolean not null default false,
  add column if not exists permissions       jsonb    not null default '{}'::jsonb;

comment on column public.organisation_users.invoice_approver is
  'When true, this member can approve invoices regardless of their role.';

comment on column public.organisation_users.permissions is
  'Optional granular permission overrides for this member. Keys are MemberPermissionKey values.';
