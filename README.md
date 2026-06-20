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
3. Follow the Mailgun testing guide in `MAILGUN_WEBHOOK_TESTING.md`.
