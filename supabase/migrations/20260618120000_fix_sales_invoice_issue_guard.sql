-- Allow the controlled sales invoice issue RPC to stamp immutable fields while
-- keeping submitted/approved/issued invoices protected from ordinary edits.

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

  if old.status <> 'draft'
     and current_setting('app.sales_invoice_issue', true) is distinct from 'on'
     and (
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
