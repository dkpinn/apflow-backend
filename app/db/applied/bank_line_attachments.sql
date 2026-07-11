-- Bank line attachments — multiple supporting documents per bank transaction.
--
-- A single reconciliation line (e.g. a medical expense) can carry several files:
-- receipt, doctor's note, script, medicine receipt, etc. Files live in the
-- existing `statement-files` Supabase Storage bucket under an org-first path
-- ({orgId}/attachments/{lineId}/...); this table holds the metadata + storage
-- pointer. Complements the existing single `bank_statement_lines.receipt_document_id`
-- link (which points at an extracted receipt document) — this is for ad-hoc files.

create table if not exists public.bank_line_attachments (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  bank_statement_line_id uuid not null references public.bank_statement_lines(id) on delete cascade,
  storage_bucket text not null default 'statement-files',
  storage_path text not null,
  original_filename text,
  mime_type text,
  file_size_bytes bigint,
  doc_kind text not null default 'other'
    check (doc_kind in ('receipt','note','script','invoice','other')),
  uploaded_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create index if not exists bank_line_attachments_line_idx
  on public.bank_line_attachments(bank_statement_line_id, created_at);
create index if not exists bank_line_attachments_org_idx
  on public.bank_line_attachments(organisation_id);

alter table public.bank_line_attachments enable row level security;

-- Mirrors bank_statement_lines: any org member can read; owner/admin/accountant write.
drop policy if exists "bank_line_attachments_select_member" on public.bank_line_attachments;
create policy "bank_line_attachments_select_member" on public.bank_line_attachments
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "bank_line_attachments_write_accountants" on public.bank_line_attachments;
create policy "bank_line_attachments_write_accountants" on public.bank_line_attachments
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));
