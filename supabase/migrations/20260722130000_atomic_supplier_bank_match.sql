-- Atomically accept a bank match against a posted supplier invoice.
-- The invoice expense/VAT splits were posted with the invoice; settlement only
-- debits Trade Payables and credits the linked bank GL account.

alter table public.payments
  add column if not exists bank_statement_line_id uuid
    references public.bank_statement_lines(id) on delete set null;

create unique index if not exists payments_org_bank_statement_line_uidx
  on public.payments(organisation_id, bank_statement_line_id)
  where bank_statement_line_id is not null;

create or replace function public.accept_supplier_invoice_bank_match_atomic(
  p_org_id uuid,
  p_bank_statement_line_id uuid,
  p_suggestion_id uuid,
  p_user_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  bank_line public.bank_statement_lines%rowtype;
  suggestion public.bank_transaction_suggestions%rowtype;
  invoice public.invoices_extracted%rowtype;
  bank_gl_id uuid;
  payable_gl_id uuid;
  payment_id uuid;
  reconciliation_id uuid;
  journal_id uuid;
  payment_amount numeric(14,2);
  previously_paid numeric(14,2);
  outstanding_before numeric(14,2);
  outstanding_after numeric(14,2);
  payment_state text;
begin
  if auth.role() is distinct from 'service_role' then
    if auth.uid() is null
       or p_user_id is distinct from auth.uid()
       or not public.has_org_role(
         p_org_id,
         array['owner','admin','accountant']::public.organisation_role[]
       )
    then
      raise exception 'Not authorised to reconcile supplier payments';
    end if;
  end if;

  select * into bank_line
  from public.bank_statement_lines
  where id = p_bank_statement_line_id and organisation_id = p_org_id
  for update;
  if not found then raise exception 'Bank statement line not found'; end if;

  select * into suggestion
  from public.bank_transaction_suggestions
  where id = p_suggestion_id
    and organisation_id = p_org_id
    and bank_statement_line_id = p_bank_statement_line_id
    and matched_invoice_id is not null
  for update;
  if not found then raise exception 'Supplier invoice match suggestion not found for this bank line'; end if;
  if suggestion.status not in ('open', 'accepted') then
    raise exception 'Supplier invoice match suggestion is not available for acceptance';
  end if;

  -- Idempotent retry after a successful request.
  if bank_line.posting_status = 'posted'
     and bank_line.accepted_suggestion_id = p_suggestion_id then
    select id into payment_id from public.payments
    where organisation_id = p_org_id
      and bank_statement_line_id = p_bank_statement_line_id;
    return jsonb_build_object(
      'journal_id', bank_line.gl_journal_id,
      'payment_id', payment_id,
      'invoice_id', suggestion.matched_invoice_id,
      'amount', abs(round(bank_line.signed_amount, 2)),
      'idempotent', true
    );
  end if;
  if bank_line.posting_status = 'posted' then
    raise exception 'Bank statement line has already been posted';
  end if;
  if round(bank_line.signed_amount, 2) >= 0 then
    raise exception 'A supplier invoice payment must be money out';
  end if;

  select * into invoice
  from public.invoices_extracted
  where id = suggestion.matched_invoice_id and organisation_id = p_org_id
  for update;
  if not found then raise exception 'Matched supplier invoice not found'; end if;
  if invoice.posting_status <> 'posted'
     or invoice.approval_status <> 'approved'
     or invoice.review_status <> 'approved' then
    raise exception 'Matched supplier invoice must be reviewed, approved, and posted';
  end if;
  if invoice.supplier_id is null then
    raise exception 'Matched supplier invoice has no linked supplier';
  end if;

  select ba.gl_account_id into bank_gl_id
  from public.bank_accounts ba
  where ba.id = bank_line.bank_account_id and ba.organisation_id = p_org_id;
  if bank_gl_id is null then raise exception 'Bank account needs a linked GL account before posting'; end if;

  select a.id into payable_gl_id
  from public.accounts a
  where a.organisation_id = p_org_id and a.system_key = 'trade_payables'
  limit 1;
  if payable_gl_id is null then raise exception 'Trade Payables system account not found'; end if;

  if exists (
    select 1 from public.organisation_accounting_periods p
    where p.organisation_id = p_org_id
      and bank_line.line_date between p.period_start and p.period_end
      and (p.status in ('closed', 'locked') or (p.lock_date is not null and bank_line.line_date <= p.lock_date))
  ) then
    raise exception 'The accounting period for this bank payment is closed or locked';
  end if;

  select coalesce(sum(coalesce(rl.matched_amount, rl.expected_amount, 0)), 0)
    into previously_paid
  from public.reconciliation_lines rl
  where rl.organisation_id = p_org_id
    and rl.invoice_extracted_id = invoice.id
    and rl.match_status = 'matched';

  payment_amount := abs(round(bank_line.signed_amount, 2));
  outstanding_before := greatest(round(invoice.total_amount, 2) - previously_paid, 0);
  if outstanding_before <= 0 then raise exception 'Matched supplier invoice is already fully paid'; end if;
  if payment_amount > outstanding_before + 0.02 then
    raise exception 'Bank payment exceeds the supplier invoice outstanding amount';
  end if;
  outstanding_after := greatest(outstanding_before - payment_amount, 0);
  payment_state := case when outstanding_after <= 0.02 then 'paid' else 'partial' end;

  payment_id := gen_random_uuid();
  insert into public.payments (
    id, organisation_id, supplier_id, payment_date, payment_reference,
    amount, currency, payment_source, match_status, bank_statement_line_id
  ) values (
    payment_id, p_org_id, invoice.supplier_id, bank_line.line_date,
    coalesce(nullif(bank_line.reference, ''), 'BANK-' || p_bank_statement_line_id::text),
    payment_amount, invoice.currency, 'bank_reconciliation', 'matched', p_bank_statement_line_id
  );

  reconciliation_id := gen_random_uuid();
  insert into public.reconciliations (
    id, organisation_id, supplier_id, reconciliation_date,
    reconciliation_status, total_statement_amount, total_matched_amount,
    total_unmatched_amount, notes, created_by
  ) values (
    reconciliation_id, p_org_id, invoice.supplier_id, bank_line.line_date,
    'completed', payment_amount, payment_amount, 0,
    'Accepted supplier invoice match from bank reconciliation.', p_user_id
  );

  insert into public.reconciliation_lines (
    id, organisation_id, reconciliation_id, invoice_extracted_id, payment_id,
    match_status, expected_amount, matched_amount, variance_amount, notes
  ) values (
    gen_random_uuid(), p_org_id, reconciliation_id, invoice.id, payment_id,
    'matched', outstanding_before, payment_amount,
    round(payment_amount - outstanding_before, 2),
    'Matched from bank statement line ' || p_bank_statement_line_id::text || '.'
  );

  journal_id := gen_random_uuid();
  insert into public.gl_journals (
    id, organisation_id, source_type, source_id, journal_date, description,
    status, total_debit, total_credit, created_by, posted_by, posted_at
  ) values (
    journal_id, p_org_id, 'bank_transaction', p_bank_statement_line_id,
    bank_line.line_date,
    'Supplier payment ' || coalesce(invoice.invoice_number, bank_line.description, ''),
    'posted', payment_amount, payment_amount, p_user_id, p_user_id, now()
  );

  insert into public.gl_journal_lines (
    organisation_id, gl_journal_id, account_id, description,
    debit_amount, credit_amount, tracking, sort_order
  ) values
  (
    p_org_id, journal_id, payable_gl_id,
    'Payment of supplier invoice ' || coalesce(invoice.invoice_number, invoice.id::text),
    payment_amount, 0, '{}'::jsonb, 0
  ),
  (
    p_org_id, journal_id, bank_gl_id,
    coalesce(bank_line.description, bank_line.reference, 'Supplier payment'),
    0, payment_amount, '{}'::jsonb, 1
  );

  update public.bank_transaction_suggestions
  set status = 'accepted', updated_at = now()
  where id = p_suggestion_id and organisation_id = p_org_id;

  update public.bank_statement_lines
  set accepted_suggestion_id = p_suggestion_id,
      supplier_id = invoice.supplier_id,
      match_status = 'matched',
      allocation_status = 'allocated',
      review_status = 'reviewed',
      posting_status = 'posted',
      gl_journal_id = journal_id,
      reviewed_by = p_user_id,
      reviewed_at = now(),
      updated_at = now()
  where id = p_bank_statement_line_id and organisation_id = p_org_id;

  return jsonb_build_object(
    'journal_id', journal_id,
    'payment_id', payment_id,
    'reconciliation_id', reconciliation_id,
    'invoice_id', invoice.id,
    'amount', payment_amount,
    'outstanding_before', outstanding_before,
    'outstanding_after', outstanding_after,
    'payment_status', payment_state,
    'idempotent', false
  );
end;
$$;

revoke all on function public.accept_supplier_invoice_bank_match_atomic(uuid, uuid, uuid, uuid) from public, anon;
grant execute on function public.accept_supplier_invoice_bank_match_atomic(uuid, uuid, uuid, uuid) to authenticated, service_role;

create or replace function public.reverse_supplier_invoice_bank_match_atomic(
  p_org_id uuid,
  p_journal_id uuid,
  p_user_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  original_journal public.gl_journals%rowtype;
  bank_line public.bank_statement_lines%rowtype;
  payment_id uuid;
  reversal_id uuid := gen_random_uuid();
begin
  if auth.role() is distinct from 'service_role' then
    if auth.uid() is null
       or p_user_id is distinct from auth.uid()
       or not public.has_org_role(
         p_org_id,
         array['owner','admin','accountant']::public.organisation_role[]
       )
    then
      raise exception 'Not authorised to reverse supplier payments';
    end if;
  end if;

  select * into original_journal
  from public.gl_journals
  where id = p_journal_id
    and organisation_id = p_org_id
    and source_type = 'bank_transaction'
  for update;
  if not found or original_journal.status <> 'posted' then
    raise exception 'Posted supplier bank journal not found';
  end if;

  select * into bank_line
  from public.bank_statement_lines
  where id = original_journal.source_id and organisation_id = p_org_id
  for update;
  if not found then raise exception 'Source bank statement line not found'; end if;

  select id into payment_id
  from public.payments
  where organisation_id = p_org_id
    and bank_statement_line_id = bank_line.id
  for update;
  if payment_id is null then raise exception 'Supplier settlement payment not found'; end if;

  if exists (
    select 1 from public.organisation_accounting_periods p
    where p.organisation_id = p_org_id
      and original_journal.journal_date between p.period_start and p.period_end
      and (p.status in ('closed', 'locked') or (p.lock_date is not null and original_journal.journal_date <= p.lock_date))
  ) then
    raise exception 'The accounting period for this bank payment is closed or locked';
  end if;

  insert into public.gl_journals (
    id, organisation_id, source_type, source_id, reversal_of_journal_id,
    journal_date, description, status, total_debit, total_credit,
    created_by, posted_by, posted_at
  ) values (
    reversal_id, p_org_id, 'bank_transaction_reversal', bank_line.id, p_journal_id,
    original_journal.journal_date, 'Reversal: ' || original_journal.description,
    'posted', original_journal.total_credit, original_journal.total_debit,
    p_user_id, p_user_id, now()
  );

  insert into public.gl_journal_lines (
    organisation_id, gl_journal_id, account_id, description,
    debit_amount, credit_amount, tracking, sort_order
  )
  select p_org_id, reversal_id, account_id,
         'Reversal: ' || coalesce(description, original_journal.description),
         credit_amount, debit_amount, tracking, sort_order
  from public.gl_journal_lines
  where gl_journal_id = p_journal_id;

  update public.gl_journals
  set status = 'reversed', reversed_by = p_user_id, reversed_at = now()
  where id = p_journal_id and organisation_id = p_org_id;

  delete from public.reconciliations r
  where r.id in (
    select rl.reconciliation_id
    from public.reconciliation_lines rl
    where rl.organisation_id = p_org_id and rl.payment_id = payment_id
  );
  -- reconciliation_lines are removed by the reconciliation FK cascade.
  delete from public.payments
  where id = payment_id and organisation_id = p_org_id;

  update public.bank_transaction_suggestions
  set status = 'open', updated_at = now()
  where id = bank_line.accepted_suggestion_id and organisation_id = p_org_id;

  update public.bank_statement_lines
  set posting_status = 'unposted', allocation_status = 'unallocated',
      match_status = 'unmatched', review_status = 'pending',
      accepted_suggestion_id = null, supplier_id = null, gl_journal_id = null,
      reviewed_by = null, reviewed_at = null, updated_at = now()
  where id = bank_line.id and organisation_id = p_org_id;

  return jsonb_build_object(
    'reversal_id', reversal_id,
    'source_line_id', bank_line.id,
    'payment_id', payment_id
  );
end;
$$;

revoke all on function public.reverse_supplier_invoice_bank_match_atomic(uuid, uuid, uuid) from public, anon;
grant execute on function public.reverse_supplier_invoice_bank_match_atomic(uuid, uuid, uuid) to authenticated, service_role;
