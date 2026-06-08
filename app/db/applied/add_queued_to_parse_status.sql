-- Add 'queued' value to the parse_status enum on invoices_raw.
-- Extraction worker sets parse_status = 'queued' in queue_invoice_job(), but this
-- value was never added to the DB enum, causing Postgres error 22P02 and silent
-- extraction failures (documents stay pending forever, nothing extracts).
ALTER TYPE parse_status ADD VALUE IF NOT EXISTS 'queued';
