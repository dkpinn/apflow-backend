-- Multi-channel delivery for customer (sales) invoices.
-- The existing delivery-event log assumed email/Mailgun only. Add a `channel`
-- discriminator and a `recipient_phone` column so the same log can track
-- WhatsApp (or other future channels) alongside email. The email path is
-- unchanged: existing rows default to channel = 'email'.

alter table public.sales_invoice_delivery_events
  add column if not exists channel text not null default 'email'
    check (channel in ('email', 'whatsapp')),
  add column if not exists recipient_phone text;

create index if not exists sales_invoice_delivery_events_channel_idx
  on public.sales_invoice_delivery_events(sales_invoice_id, channel, created_at desc);
