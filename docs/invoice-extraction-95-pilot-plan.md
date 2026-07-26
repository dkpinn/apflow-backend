# Invoice extraction: 95% pilot plan

## Pilot promise

The pilot target is not a model confidence score. It is measured against owner-verified source truth.

The release gate requires all of the following on a locked benchmark:

1. At least 95% atomic-value accuracy, with its one-sided 95% Wilson lower confidence bound also at or above 95%.
2. At least 95% of documents require no more than two owner corrections, with the same 95% lower-confidence requirement.
3. Zero critical errors in supplier/customer direction, document type, invoice/reference number, dates, currency, subtotal, tax, total, line quantities, line prices, line tax, or line totals.
4. At least 120 locked documents, balanced across the six pilot strata: invoice/credit-note/receipt × PDF/image, with at least 20 in each stratum.
5. The locked documents are never used to tune parsers, prompts, templates, or scoring rules.

Case, whitespace, and harmless formatting differences are normalised. Spelling differences remain corrections. Amounts must agree to one cent.

## What the platform owner does

For each real document:

1. Upload it through the normal invoice workflow.
2. Review the source beside the extracted fields and lines.
3. Correct only values that are wrong.
4. In **Admin → Extraction Lab**, capture the corrected invoice as gold truth.
5. Choose its document kind, source format, and dataset split.
6. Run the benchmark. The lab reports every mismatch and the number of corrections a user would have needed.

Uploading does not automatically train on unchecked data. Only an owner-verified gold snapshot becomes benchmark truth.

## Stepped sample plan

### Step 1 — 30-document discovery set

- Five documents in each of the six strata.
- Include different suppliers, layouts, clean files, scans, and phone photos.
- Use only the `development` split.
- Outcome: identify systematic failures and remove silent high-confidence mistakes.

### Step 2 — 120-document development set

- Twenty documents in each stratum.
- Prefer at least 40 supplier/layout families overall.
- Include multi-page documents, rotated images, shadows, skew, faint print, dense tables, discounts, negative credit-note lines, VAT and non-VAT suppliers.
- Fix failures by general extraction rules first; use supplier templates only when the layout has strong identifying evidence.
- Outcome: development metrics exceed the pilot gate before locked testing begins.

### Step 3 — 120-document locked pilot set

- Twenty new, unseen documents in each stratum.
- No document or near-duplicate from development may enter this set.
- Run once after the extraction version is frozen.
- Outcome: the automated pilot gate passes all five requirements above.

### Step 4 — Failure loop

If the gate fails:

1. Move the failing locked examples to a permanent regression archive, not into the next locked set.
2. Add similar examples to development.
3. Fix the systematic cause.
4. Create a fresh locked replacement set for the affected strata.
5. Re-run the complete locked benchmark.

### Step 5 — Pilot monitoring

- Sample and verify the first 100 pilot documents.
- Report correction count, critical errors, document type, format, supplier/layout, extractor version, and confidence calibration.
- Pause claims of 95% if the rolling verified rate or lower confidence bound falls below the gate.

## Development priorities

1. Eliminate critical identity and amount errors.
2. Preserve native-PDF tables while using OCR/vision for rasterised issuer blocks.
3. Make candidate selection evidence-based rather than confidence-led.
4. Improve table structure, line order, descriptions, discounts, tax, and credit-note signs.
5. Calibrate review routing so uncertain documents are clearly flagged instead of silently accepted.
6. Minimise spelling and formatting corrections only after financial correctness is protected.

## Feature freeze

Until the locked gate passes, extraction correctness, review usability, benchmark tooling, and regression coverage take precedence over unrelated feature development.
