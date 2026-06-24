-- Add "deferred" to bank_statement_lines.review_status allowed values.
-- The existing constraint must be dropped and re-created because Postgres
-- does not support ALTER CHECK CONSTRAINT directly.
alter table public.bank_statement_lines
    drop constraint if exists bank_statement_lines_review_status_check;

alter table public.bank_statement_lines
    add constraint bank_statement_lines_review_status_check
    check (review_status in ('pending', 'reviewed', 'approved', 'ignored', 'deferred'));
