# Invoice Detail Page

**Route:** `/_app/invoices/$invoiceId`  
**Frontend file:** `src/routes/_app.invoices.$invoiceId.tsx`  
**Backend router:** `app/routers/invoices.py`

---

## A. Objectives

1. **Display the full review state of a single invoice** — extracted fields, document preview images, line items, banking details, and supplier identity in one place.
2. **Let a reviewer correct extraction errors** — every extracted field is editable; changes are saved back to `invoices_extracted` and feed the training pipeline via `invoice_extraction_feedback`.
3. **Verify banking details** — compare the account number, bank name, and sort code on the invoice against the linked supplier's master record and surface mismatches before payment.
4. **Validate financial totals** — line items are the source of truth; subtotal, VAT, and total are computed from them and must agree with the document total within acceptable rounding tolerances.
5. **Save and approve the invoice** — a reviewer can save draft corrections or fully approve the invoice, locking it from further edits.
6. **Trigger re-extraction** — if the initial OCR result is wrong, the reviewer can kick off a fresh VLM extraction and watch real-time progress.
7. **Manage the supplier relationship** — link or create a supplier master record from the extracted supplier identity, or push the invoice's banking details back to the supplier profile.
8. **Show the source document** — preview images rendered from the original file are displayed alongside the extracted data so reviewers never lose sight of what was actually on the page.
9. **Generate missing previews on demand** — if preview images were never rendered (old invoices, selectable-text PDFs), a single button triggers lightweight on-demand generation (~1 s) without a full re-extraction.
10. **Maintain an audit trail** — every status change, approval, and reviewer correction is recorded and visible in the audit sheet.

---

## B. How the Frontend Accomplishes the Objectives

### Component tree

```
InvoiceDetail                     main page component
├── DocumentPreview               collapsible left panel — preview images + page navigation
├── (four tabs)
│   ├── EditableExtractedCard     "Extracted data" tab — invoice fields, totals, status
│   ├── LineItemsCard             "Line items" tab — editable line-by-line breakdown
│   ├── BankingReviewCard         "Banking" tab — extracted vs master comparison
│   └── SupplierReviewCard        "Supplier" tab — extracted identity vs master record
├── ReextractProgressPanel        floating progress bar during re-extraction
├── AuditTrail / InvoiceAuditTrail  audit event sheet
├── EditBankingDialog             override banking details
└── LinkSupplierDialog            search and link a supplier master record
```

### Data loading

The main `useQuery` (key `["invoice_review_data", invoiceId, currentOrgId]`) calls `fetchReviewDataFromApi()` which hits `GET /api/invoices/{invoiceId}/review-data`. If FastAPI is unavailable it falls back to `fetchSupabaseReviewData()`, which queries Supabase directly for `invoices_extracted`, `invoices_raw`, `document_pages`, `invoice_line_items`, `invoice_audit_events`, and the linked supplier.

A second `useQuery` fetches the linked supplier master record from the `suppliers` table whenever `supplier_id` is present.

A third `useQuery` (`supplier_match_suggest`) calls `GET /api/suppliers/match-suggest` when no supplier is linked, to surface a suggestion automatically.

### Realtime updates

Two Supabase Realtime channels keep the UI live without polling:
- `invoice-detail-{extractedId}` — patches `invoices_extracted` rows directly into the query cache as they change.
- `line-items-{invoiceId}` — handles `INSERT`, `UPDATE`, and `DELETE` events on `invoice_line_items`, keeping the line items list in sync.

### Editable fields and form state

`reviewForm` (`EditableInvoiceForm`) is a flat `Record<AttemptMappedField, string>` that mirrors all editable columns in `invoices_extracted`. It is initialised from `buildEditableInvoiceForm()` and reset whenever the server data changes. `setReviewField()` updates individual keys. `buildReviewPayload()` strips banking fields when the user has already committed banking overrides, preventing accidental overwrites.

### Saving changes

`handleSaveReview()` — calls `supabase.from("invoices_extracted").update(payload)` directly, then calls `recordReviewFeedback()` to insert diff rows into `invoice_extraction_feedback` for training signal. The Sonner toast library gives inline success/error feedback.

`handleApproveReview()` — saves the form payload first, then sets `approval_status = "approved"`, `review_status = "approved"`, and `approved_at` on `invoices_extracted`. Also patches the React Query cache optimistically so the status badge updates instantly without a refetch.

`handleMarkReviewed()` / `handleUnlock()` — set `review_status` to `"reviewed"` or `"in_review"` respectively via direct Supabase calls.

### Line items and derived totals

`LineItemsCard` holds local editable state (`items`) and derives three memo values:
- `vatRate` — 0 if the linked supplier has no `vat_number`; otherwise `default_vat_rate / 100`, defaulting to 0.15.
- `liveTotals` — `{ subtotal, vat, total }` computed from the current row values and `vatRate`.
- Calls `onTotalsChange(liveTotals)` via `useEffect` so `InvoiceDetail` can pass `computedTotals` into `EditableExtractedCard`.

`handleSaveAll()` in `LineItemsCard` posts to `POST /api/invoices/save-line-items`. On success it calls `onSaved()` which invalidates both `invoice_review_data` and `invoice_line_items` query caches, triggering a server-side refetch of the corrected totals.

`EditableExtractedCard` renders `subtotal`, `tax_amount`, and `total_amount` as read-only dashed boxes ("from line items") when `computedTotals` is present, preventing them from being edited independently.

`documentTotalRef` captures the original VLM-extracted total once at mount so the backend can use it as a rounding reference on every subsequent save, not a moving target.

### Banking validation

`bankingChecks` (computed inline in `InvoiceDetail`) compares the invoice's effective bank fields (extracted or overridden) against the linked supplier master, producing one of: `match | mismatch | missing-invoice | missing-master | missing-both` per field. `BankingReviewCard` renders coloured rows and a warning banner. `handlePopulateSupplierBanking()` copies the invoice's banking details back to the supplier master with a confirmation dialog. `handleSaveOverrides()` writes to `override_bank_account_number`, `override_sort_code`, `override_bank_name` on `invoices_extracted`.

### Document preview

`DocumentPreview` receives the `document_pages` array (from the review-data response). Each page has `original_preview_path` and `processed_preview_path` — storage paths in the `invoices` Supabase Storage bucket. `resolveStorageAssetUrl()` calls `supabase.storage.from("invoices").createSignedUrl()` to get a time-limited URL for each image. Signed URLs go directly to Supabase Storage — they do not pass through ngrok, so preview images work during local development.

When no preview paths exist, the panel shows a "Generate preview" button. Clicking it calls `handleGeneratePreview()`, which posts `{ invoice_raw_id, organisation_id }` to `POST /api/invoices/generate-preview`. On success the `invoice_review_data` query is invalidated and the new preview images appear without a page reload.

The panel is collapsible (state persisted in `localStorage` under `"apflow-doc-collapsed"`). It auto-collapses when the invoice is approved.

### Re-extraction

`handleReextract()` posts to `POST /api/invoices/re-extract` (async, job-based). The returned `job_id` is polled via `GET /api/invoices/re-extract/{job_id}/status` every 1.5 s. A local timer drives a synthetic progress bar (`REEXTRACT_PROGRESS_STAGES`) so the UI feels responsive even before the first poll. On completion, both `invoice_review_data` and `invoice_line_items` caches are invalidated and the line-items diagnostic is stored in `localStorage` (key `line-items-diagnostic:{invoiceId}`).

### Supplier management

`handleCreateSupplier()` posts to `POST /api/suppliers/from-invoice` with the `supplier_create_payload` built by the backend in the review-data response. `handleLinkSupplier()` calls `POST /api/suppliers/link` to associate an existing supplier. `handleUnlinkSupplier()` clears `supplier_id` on `invoices_extracted` directly.

---

## C. How the Backend Accomplishes the Objectives

### `GET /api/invoices/{invoice_id}/review-data`

The central endpoint. It resolves `invoice_id` as either `invoices_extracted.id` or `invoices_extracted.invoice_raw_id` (tries both). Then in a single request it assembles:

| Data | Source table |
|---|---|
| `invoice` | `invoices_extracted` (full row + joined supplier) |
| `raw` | `invoices_raw` (file metadata, status, preview paths) |
| `document_pages` | `document_pages` (ordered by page_number) |
| `line_items` | `invoice_line_items` (ordered by created_at, id) |
| `parse_attempts` | `invoice_parse_attempts` (via `fetch_parse_attempts()`) |
| `audit_events` | `invoice_audit_events` (ordered by created_at asc) |
| `extracted_supplier_profile` | Derived by `build_extracted_supplier_profile()` |
| `extracted_document_profile` | Derived by `build_extracted_document_profile()` |
| `supplier_create_payload` | Derived by `build_supplier_create_payload()` |

Child reads that fail return empty data and populate `fetch_errors` rather than making the whole response fail, so partial data still reaches the reviewer.

### `POST /api/invoices/save-line-items`

Request: `{ invoice_extracted_id, organisation_id, supplier_id, line_items, document_total }`.

1. Fetches the supplier's `vat_number` and `default_vat_rate` via `fetch_supplier_processing_settings()`. VAT rate is 0 unless the supplier is a VAT vendor.
2. Computes `subtotal = SUM(line_total)`, `computed_vat = subtotal * vat_rate`, `computed_total = subtotal + computed_vat`.
3. Applies hybrid rounding against `document_total`:
   - `|diff| ≤ 0.02` → absorbs into VAT silently (`rounding_applied = "vat_adjusted"`)
   - `0.02 < |diff| ≤ 0.50` → appends a "Rounding adjustment" line item (`rounding_applied = "line_item_added"`)
   - `|diff| > 0.50` → sets `validation_status = "needs_review"` (`rounding_applied = "needs_review"`)
4. Calls `replace_invoice_line_items()` to delete existing rows and insert the new set.
5. Updates `invoices_extracted` with the derived `subtotal`, `tax_amount`, `total_amount` (and optionally `validation_status`).

### `POST /api/invoices/generate-preview`

Request: `{ invoice_raw_id, organisation_id }`.

1. Downloads the original file from Supabase Storage (`invoices` bucket) using `invoices_raw.file_path`.
2. Renders pages to PIL Images: PDFs via `pdf_to_images()` (PyMuPDF), images via PIL directly.
3. For each page, calls `generate_preview_images(img, img)` to produce resized JPEG thumbnails.
4. Uploads originals and processed copies to `{org_id}/invoices/previews/{invoice_raw_id}/page-{n}-original.jpg` and `page-{n}-processed.jpg` via `upload_invoice_preview_image()`.
5. Upserts rows in `document_pages` (insert if page not yet recorded, update if it exists).
6. Updates `invoices_raw.preview_path` and `processed_preview_path` with page-1 paths.

### `POST /api/invoices/re-extract`

Creates an in-memory job record (`REEXTRACT_JOBS` dict) and spawns `run_reextract_job_background()` as a FastAPI `BackgroundTask`. The background function calls `run_invoice_re_extraction()`, which:
1. Downloads the file from storage.
2. Calls `extract_text_with_fallback()` — three OCR paths: selectable PDF text, Tesseract OCR, or Gemini VLM image analysis.
3. Calls `extract_with_gemini()` for VLM field extraction with the best available image.
4. Applies `apply_supplier_processing_rules()` for supplier-specific field normalisation.
5. Updates `invoices_extracted` with the new parsed fields.
6. Calls `replace_invoice_line_items()` to persist extracted line items.
7. Calls `persist_preview_artifacts()` to upload rendered page images to Supabase Storage.
8. Advances the job status through defined stages (`reading_document → ocr → parsing_invoice_fields → extracting_line_items → saving_extracted_data → completed`).

The frontend polls `GET /api/invoices/re-extract/{job_id}/status` to read stage and progress.

### OCR pipeline — preview fix

In `extract_text_with_fallback()` (`app/services/invoice_ocr_pipeline.py`), the `pdf_text` path (used when a PDF has ≥80 characters of embedded selectable text) now renders preview images before returning, so `persist_preview_artifacts()` always receives populated `original_preview_image` and `processed_preview_image` keys regardless of which OCR path was taken.

---

## D. Code Called from the Page

### Frontend utility functions

| Function | Line | Purpose |
|---|---|---|
| `fetchReviewDataFromApi()` | 4117 | Fetches `GET /api/invoices/{id}/review-data`, normalises the response |
| `fetchSupabaseReviewData()` | 4141 | Supabase fallback — queries 5 tables and assembles the same shape as the API response |
| `normalizeReviewData()` | 4299 | Coerces the raw API JSON to the typed `ReviewDataResponse` shape |
| `buildEditableInvoiceForm()` | 4350 | Builds the initial `reviewForm` from `invoices_extracted` + `parsed` data |
| `normalizeSupplierReceiptForm()` | 4441 | Suppresses generic/garbled OCR values and fills missing fields from the supplier master |
| `buildPayloadFrom()` | 4537 | Converts `reviewForm` to the update payload sent to Supabase |
| `resolveStorageAssetUrl()` | 3356 | Creates a signed URL from a Supabase Storage path for image display |
| `invoiceTotalMismatch()` | 4690 | Returns `true` when subtotal + tax ≠ total (used for warning badge) |
| `bankingMasterMismatch()` | 4772 | Returns `true` when any banking field differs between invoice and supplier master |
| `supplierMasterComparisonRows()` | 4776 | Builds comparison rows for the Supplier tab diff display |
| `buildSupplierCreatePayload()` | 5082 | Assembles the payload for `POST /api/suppliers/from-invoice` |
| `toNum()` | 3429 | Safely coerces any value to `number \| null` for financial arithmetic |

### Backend services

| Module | Function | Purpose |
|---|---|---|
| `app/services/invoice_ocr_pipeline.py` | `extract_text_with_fallback()` | Three-path OCR: selectable PDF text → Tesseract → Gemini VLM |
| `app/services/invoice_ocr_pipeline.py` | `pdf_to_images()` | Renders PDF pages to PIL Images using PyMuPDF |
| `app/services/invoice_ocr_pipeline.py` | `parse_invoice_fields()` | Regex + heuristic field extraction from raw OCR text |
| `app/services/invoice_extraction/vlm_parser.py` | `extract_with_gemini()` | Sends rendered page images to Gemini for structured field extraction |
| `app/services/invoice_extraction/receipt_preprocessing.py` | `generate_preview_images()` | Resizes PIL images to JPEG thumbnails; returns `PreviewImages(original, processed)` |
| `app/services/invoice_previews.py` | `persist_preview_artifacts()` | Uploads `original_preview_image` / `processed_preview_image` from page dicts to Supabase Storage |
| `app/services/invoice_previews.py` | `upload_invoice_preview_image()` | Single-image upsert upload to the `invoices` bucket with fallback retry |
| `app/services/invoice_line_items.py` | `replace_invoice_line_items()` | Delete-and-reinsert line items; returns diagnostics (`found`, `inserted`, `totals_match`) |
| `app/services/invoice_line_items.py` | `build_line_item_diagnostics()` | Sums `line_total` values and compares against `invoice_total` |
| `app/services/invoice_parse_attempts.py` | `fetch_parse_attempts()` | Reads `invoice_parse_attempts` rows and identifies the `selected=true` attempt |
| `app/services/invoice_supplier_rules.py` | `fetch_supplier_processing_settings()` | Reads `vat_number`, `default_vat_rate`, `parse_line_items` from the `suppliers` table |
| `app/services/invoice_supplier_rules.py` | `apply_supplier_processing_rules()` | Applies supplier-specific overrides to extracted field values |
| `app/services/audit_log.py` | `log_invoice_event()` | Writes a row to `invoice_audit_events` for every significant state change |
| `app/services/document_jobs.py` | `create_processing_job()` / `mark_job_*()` | Manages the `document_processing_jobs` queue used by the extraction worker |

### Supabase tables read/written by this page

| Table | Operations |
|---|---|
| `invoices_extracted` | SELECT (review data), UPDATE (save review, approve, banking overrides, derived totals) |
| `invoices_raw` | SELECT (file metadata, preview paths), UPDATE (preview_path after generate-preview) |
| `invoice_line_items` | SELECT (review data), DELETE + INSERT (save-line-items) |
| `document_pages` | SELECT (review data), UPDATE / INSERT (generate-preview) |
| `invoice_audit_events` | SELECT (audit trail), INSERT (via `log_invoice_event`) |
| `invoice_parse_attempts` | SELECT (selected attempt for field diffing) |
| `invoice_extraction_feedback` | INSERT (field correction records for ML training) |
| `suppliers` | SELECT (master banking + VAT rate), UPDATE (populate-banking) |

### Supabase Storage paths

| Path | Purpose |
|---|---|
| `invoices/{file_path}` | Original uploaded invoice file (PDF or image) |
| `{org_id}/invoices/previews/{invoice_raw_id}/page-{n}-original.jpg` | Best-parsing-snapshot preview image |
| `{org_id}/invoices/previews/{invoice_raw_id}/page-{n}-processed.jpg` | Processed (contrast-enhanced) preview image |
