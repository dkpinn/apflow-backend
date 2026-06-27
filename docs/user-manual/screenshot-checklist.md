# APPayPal User Manual Screenshot Checklist

Use this checklist when capturing screenshots from the live or staging APPayPal app. Capture with demo data only.

## Capture Rules

- Use a demo organisation with safe sample data.
- Do not show real customer, supplier, bank, employee, or financial information.
- Do not include passwords, API keys, service keys, browser password prompts, or private URLs with tokens.
- Prefer full-screen captures for workflow context.
- Crop only when it improves readability and does not hide the action being explained.
- Save PNG files in `docs/user-manual/screenshots/`.
- Use the exact filenames listed below so `docs/user-manual.md` renders without edits.

## Required Screenshots

| Done | Filename | Screen | Notes |
| --- | --- | --- | --- |
| [x] | `00-sign-in.png` | Sign-in screen | Captured from `http://localhost:8080/dashboard` before authentication. |
| [ ] | `01-dashboard-command-centre.png` | Dashboard / Command Centre | Show work queues and go-live checklist if available. |
| [ ] | `02-invoice-upload.png` | Supplier invoice upload/list | Show upload area and document status list. |
| [ ] | `03-invoice-review-extracted-data.png` | Invoice review detail | Show document preview beside extracted fields. |
| [ ] | `04-invoice-line-items.png` | Invoice line items | Show editable line items and totals. |
| [ ] | `05-supplier-detail.png` | Supplier list or detail | Show safe supplier master data. |
| [ ] | `06-bank-account-detail.png` | Bank account detail | Show statement uploads or imported lines. |
| [ ] | `07-bank-transaction-review.png` | Bank transaction review | Show allocation/match/posting state with demo data. |
| [ ] | `08-customer-detail.png` | Customer list or detail | Show safe customer master data. |
| [ ] | `09-sales-invoice-detail.png` | Sales invoice detail | Show draft or issued sample invoice. |
| [ ] | `10-customer-collections.png` | Customer collections or receipts | Show overdue/due-soon queue or receipt allocation. |
| [ ] | `11-reports-trial-balance.png` | Reports | Show a report with date filters and sample rows. |
| [ ] | `12-settings-accounting-periods.png` | Settings / accounting periods | Show lock date or accounting period controls. |

## Workflow Verification While Capturing

Walk through these workflows and update screenshots if the UI labels differ from the manual:

1. Upload or open a supplier invoice, review extracted fields, and save.
2. Review one bank statement line and confirm the journal posting path.
3. Create or open a sales invoice and confirm submit/approve/issue/send labels.

## Final QA

- Open `docs/user-manual.md` in Markdown preview.
- Confirm all screenshots render.
- Confirm captions match the visible screen.
- Confirm no sensitive data appears in any image.
- Confirm each section uses current UI labels from the live app.
