# Mailgun Webhook Testing

This repository includes a helper script for testing Mailgun inbound webhook handling locally.

## What is included

- `scripts/send_signed_mailgun_test.py`
  - Generates a Mailgun-style HMAC-SHA256 signature using `MAILGUN_WEBHOOK_SIGNING_KEY`
  - Sends a multipart/form-data webhook payload to a target endpoint
  - Supports `--target-url`, `--recipient`, `--sender`, and optional `--attach-file`

## Setup

1. Start the backend on port `8000`.
   - Example: `uvicorn app.main:app --reload --port 8000`

2. Ensure `.env` contains the Mailgun signing key:
   - `MAILGUN_WEBHOOK_SIGNING_KEY=...`

3. Start ngrok (optional, for public webhook testing):
   - `ngrok http 8000`
   - Note the public URL from ngrok, e.g. `https://<your-tunnel>.ngrok-free.dev`

## Local test

Run the signed test against the local endpoint:

```powershell
cd apflow-backend
python .\scripts\send_signed_mailgun_test.py --recipient inv-222222222222@mail.apflow.com
```

## Ngrok/public test

Use the ngrok public URL to simulate Mailgun delivering to a public webhook endpoint:

```powershell
cd apflow-backend
python .\scripts\send_signed_mailgun_test.py --target-url https://rebirth-sporting-tying.ngrok-free.dev/api/webhooks/email-inbound --recipient inv-222222222222@mail.apflow.com
```

## Attachment test

Include a PDF attachment to verify the attachment processing path:

```powershell
python .\scripts\send_signed_mailgun_test.py --recipient inv-222222222222@mail.apflow.com --attach-file .\scripts\sample_invoice.pdf
```

## Mailgun inbound route configuration

In Mailgun, configure an inbound route that forwards matching messages to:

```text
https://<your-ngrok-url>/api/webhooks/email-inbound
```

- Use a `recipient` match pattern such as `inv-*` if your inbound addresses are dynamic.
- Mailgun will send `recipient`, `sender`, `timestamp`, `token`, `signature`, plus any attachments.

## Notes

- The backend expects `organisations.inbound_email` to contain the recipient address.
- The test script uses the same signing logic as the backend's `verify_mailgun_signature`.
- If the recipient does not match an organisation, the handler returns `{"status":"ignored","reason":"recipient not linked to any organisation"}`.
