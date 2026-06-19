-- Prevent duplicate Mailgun delivery/status events when the webhook retries
-- delivery of the same event (Mailgun retries on any non-2xx response, and
-- can also send the same event more than once even on success).
--
-- A row is uniquely identified by (provider_message_id, event_type) once the
-- message has actually been sent (provider_message_id is set). "queued" and
-- pre-send "failed" rows have no provider_message_id and are unaffected.

drop index if exists public.sales_invoice_delivery_events_provider_idx;

create unique index if not exists sales_invoice_delivery_events_provider_event_uidx
  on public.sales_invoice_delivery_events(provider_message_id, event_type)
  where provider_message_id is not null;
