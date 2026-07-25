# apflow-backend

This backend repository provides the API and webhook handling for APFlow.

## Mailgun webhook testing

For Mailgun inbound webhook testing, see `MAILGUN_WEBHOOK_TESTING.md`.

## Getting started

1. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. Start the backend:
   ```powershell
   uvicorn app.main:app --reload --port 8000
   ```
3. In a separate process, start the invoice worker:
   ```powershell
   python -m app.workers.invoice_worker
   ```
4. Follow the Mailgun testing guide in `MAILGUN_WEBHOOK_TESTING.md`.

## Invoice worker deployment

Run `python -m app.workers.invoice_worker` as a dedicated, continuously running
worker service. It uses the same Supabase environment variables as the API.
Multiple replicas can run safely because jobs are claimed atomically in
PostgreSQL. The API only queues work; it does not process the queue itself.

Optional settings:

- `INVOICE_WORKER_ID`: stable instance name (otherwise generated).
- `INVOICE_WORKER_POLL_SECONDS`: empty-queue polling delay; default `2`.
- `INVOICE_WORKER_MAINTENANCE_SECONDS`: recovery interval; default `60`.
