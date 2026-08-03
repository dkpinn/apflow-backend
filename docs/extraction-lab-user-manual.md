# Invoice Extraction Lab User Manual

## 1. Purpose

The Invoice Extraction Lab is the controlled testing area used to measure and improve how accurately APPayPal reads supplier invoices, credit notes, and receipts.

The Lab does not train the extractor merely because documents are uploaded. A document becomes useful test evidence only after a platform owner compares it with the source, corrects every error, and captures the corrected values as **gold truth**.

## 2. The process in plain language

Think of the Lab as an exam:

- **Gold truth** is the answer sheet. It is the version you have manually checked until it matches the source document 100%.
- **Re-extract + score** is a new attempt by the machine. It rereads the original file without relying on your corrected answers, then compares its new answers with the saved answer sheet.
- **Development** is the practice set. Its mistakes may be used to improve the extractor.
- **Validation** is the mock exam. It checks that improvements also work on separate documents.
- **Locked** is the final exam. Its documents must be chosen before their score is known and must not be used to improve the extractor before the final result is recorded.

Other words shown in the Lab:

| Screen term | Plain meaning |
| --- | --- |
| Candidate | A normally uploaded document that has not yet been saved as an answer sheet |
| Gold document / gold truth | The 100%-checked answer sheet saved from the corrected invoice |
| Split | The document's test group: Development, Validation, or Locked |
| Stratum | One of the six required categories, such as Invoice - PDF or Receipt - image |
| Atomic value | One value being marked, such as invoice date, total, line quantity, or line VAT |
| Correction | One extracted value that differs from the answer sheet |
| Critical error | A wrong value that creates serious identity, financial, VAT, banking, or line-item risk |
| Fresh rerun | A new extraction performed after the answer sheet was last saved or reclassified |
| Suite | A batch run of every gold document in the chosen group |
| Stale result | An older score that no longer counts because the answer sheet or group changed afterward |

### The normal sequence for one document

1. Upload the document through the normal invoice/receipt upload screen.
2. Open it normally and correct the supplier, document type, invoice number, dates, amounts, VAT, addresses, banking details, and every line item until the saved data matches the source document 100%.
3. Open **Settings > Admin > Invoice Extraction Lab**.
4. Find the same document under **Recent documents needing owner truth**.
5. Select **Capture as gold**. This saves the corrected data as the answer sheet. New gold documents initially enter the Development split.
6. Decide what job this document will perform, based on how it was selected, not on its score:
   - Keep it in **Development** if its errors may be investigated or used to improve extraction.
   - Use **Validation** if it is part of a separate pre-release check.
   - Move it to **Locked** only if it was reserved in advance for the final test and its errors will not be used to change the extractor before that test is complete.
7. Select **Re-extract + score**, or run the suite for the selected split. The new extraction is compared with the saved gold answer sheet.

Capture the gold answer sheet **before** re-extracting. A fresh extraction may replace the corrected values currently shown on the invoice, but the separately saved gold truth remains available for the comparison.

### Can a document be moved to Locked after it scores 95%?

No. Do not use the score to decide whether a document belongs in Locked. A document that has already been run, inspected, or used to guide an extraction change is now a known example and belongs in Development or Validation.

Technically, the screen may allow its split to be changed, and changing the split makes the earlier score stale so another fresh run is required. That does not make the document an honest unseen holdout. Locked documents must include failures as well as successes; otherwise the final result is biased.

A single result of 95% or more also does not mean the document or pilot has passed. A **pilot-quality document** must meet all three per-document conditions:

1. At least 95% atomic accuracy.
2. No more than two corrections.
3. Zero critical errors.

The overall pilot passes only when the complete balanced Locked set satisfies all of the requirements in the next section.

## 3. Objective

The objective is to demonstrate, using unseen and owner-verified documents, that invoice extraction is safe enough for the pilot.

The **Pilot gate** passes only when all of the following are true:

1. At least 120 eligible documents are in the locked holdout set.
2. The locked set contains at least 20 documents in each of these six strata:
   - Invoice - PDF
   - Invoice - image
   - Credit note - PDF
   - Credit note - image
   - Receipt - PDF
   - Receipt - image
3. Every eligible locked document has been freshly re-extracted and scored after its gold truth was last updated.
4. Atomic-value accuracy is at least 95%.
5. The one-sided 95% statistical lower confidence bound for atomic accuracy is also at least 95%.
6. At least 95% of documents require no more than two corrections.
7. The one-sided 95% lower confidence bound for the two-correction measure is also at least 95%.
8. Every individual stratum achieves at least 95% atomic accuracy and at least 95% of documents within two corrections.
9. There are no critical extraction errors in the locked results.

Reaching an observed score of exactly 95% does not necessarily pass the gate. The lower-confidence requirements demand enough consistent evidence that the true result is unlikely to be below 95%.

### What is measured

Atomic accuracy compares each extracted document field and each line-item field with the corresponding owner-verified value. Text comparison ignores case and harmless whitespace. Amounts may differ by no more than one cent, and numeric quantities by no more than 0.0001.

Critical errors currently include:

- Document type, direction, document count, supplier name, invoice number, invoice date, and currency.
- Subtotal, VAT amount, total, VAT number, and whether prices include VAT.
- Bank account number, branch code, and SWIFT code.
- Missing or additional line-item rows.
- Line quantity, unit price, discounted unit price, VAT amount, total, and VAT treatment.

The Lab measures extraction accuracy. It does not measure account allocation, dimension allocation, supplier-master matching, approval, or posting to the general ledger.

## 4. Access and prerequisites

Only a **Platform Owner** can use the Invoice Extraction Lab.

Open it from:

1. Select **Settings**.
2. Open the **Admin** section.
3. Select **Invoice Extraction Lab**.

Before starting, confirm that:

- The invoice extraction worker and backend are running.
- The benchmark database migration has been applied.
- Each source file is readable and complete.
- The test collection contains real, representative PDFs and images.
- Sensitive documents are handled according to the organisation's data policy.
- The person creating gold truth understands invoices, VAT, credit notes, and receipts well enough to verify every field and line.

## 5. Dataset rules

Every gold document belongs to one dataset split.

| Split | Purpose | May influence extraction changes? | Normal use |
| --- | --- | --- | --- |
| Development | Find failures and improve extraction | Yes | Run repeatedly while fixing problems |
| Validation | Check whether changes generalise before release | No direct tuning from the same run | Run after development results are satisfactory |
| Locked holdout | Make the final pilot decision | No | Use unseen documents once the extraction version is frozen |

Never place the same document, a duplicate, or a near-duplicate in more than one split. Documents from the same template may be used across splits only when they are genuinely separate transactions and the locked version was not used to design the fix.

Gold truth in the locked split is frozen. To change its document kind or recapture its values, first move it out of the locked split. Once a locked result has revealed a failure, treat that example as known: retain it as a regression example and use a new unseen replacement for the next final holdout.

## 6. Recommended sample-building plan

### Stage 1 - Discovery baseline

Collect 30 development documents: five documents in each of the six strata.

Include different suppliers and layouts, not 30 copies of one template. Use this stage to detect basic identity, totals, VAT, table, and document-classification failures.

### Stage 2 - Development set

Grow the development set to at least 120 documents: 20 in each stratum. Aim for at least 40 supplier or layout families and include difficult but realistic sources such as:

- Native PDFs and scanned PDFs.
- Phone photographs, skew, rotation, shadows, and faint print.
- Multi-page documents and dense tables.
- VAT, non-VAT, mixed-VAT, discounts, and rounding.
- Negative credit-note values and multiple line items.
- Similar issuer and recipient blocks that could be confused.

Run development suites until systematic failures have been corrected and the result is stable.

### Stage 3 - Validation set

Use a separate set of documents to check that improvements work beyond the development examples. Do not change extraction merely to memorise a validation document. If validation exposes a general failure, add similar examples to development, implement the general correction, and validate again.

### Stage 4 - Locked pilot set

Prepare at least 120 new and unseen documents, balanced at 20 or more per stratum. Freeze the extraction version, confirm every gold snapshot, move the cases to **Locked**, and run the complete locked suite.

## 7. Detailed operating process

Follow this process for every document.

### Step 1 - Upload and extract the source

1. Upload the document through the normal supplier invoice or receipt workflow.
2. Wait until extraction completes.
3. Open the document review screen.
4. Confirm that the preview is the same file you intended to test.

Do not capture failed, unreadable, cropped, or wrong source files as gold truth.

### Step 2 - Correct the document completely

Compare the preview with every extracted value. Correct the document itself, not the supplier master, when establishing source truth.

Check at least:

- Correct document type: invoice, tax invoice, credit note, or receipt.
- Issuer/supplier and recipient direction.
- Supplier name, VAT number, registration number, contact details, and addresses.
- Invoice or receipt number, reference, invoice date, due date, and currency.
- Subtotal, VAT, total, and whether prices include VAT.
- Banking information printed on the source.
- Every line description, code, quantity, unit price, discount, VAT amount, VAT treatment, and line total.
- Missing, duplicated, combined, or incorrectly ordered lines.

Save the corrected header fields and line items. Do not approve or post merely to make a benchmark document; extraction truth should match the source regardless of accounting workflow status.

### Step 3 - Capture the corrected document as gold truth

1. Return to **Settings > Admin > Invoice Extraction Lab**.
2. Find the file under **Recent documents needing owner truth**.
3. If it still needs correction, select **Correct first** and finish the review.
4. Select **Capture as gold** only after all values are correct.

New cases are captured in the **Development** split. The Lab infers the document kind from the extracted document, so verify this classification immediately.

The Lab will reject incomplete truth if required fields are missing. A valid gold document requires, at minimum, a supported document type, document count, supplier name, invoice date, currency, total amount, and a line-item array.

### Step 4 - Classify and assign the case

In **Gold benchmark documents**:

1. Confirm the document kind: **Invoice**, **Credit note**, or **Receipt**.
2. Confirm the automatically detected source format: PDF or image.
3. Select the correct split: Development, Validation, or Locked holdout.
4. Check the coverage table to keep all six strata balanced.

Only move a document to Locked after its truth is complete, current, and independently verified.

### Step 5 - Recapture later corrections

If a gold document is later found to be wrong:

1. Select **Review**.
2. Correct and save the invoice review data.
3. Return to the Lab.
4. Select **Capture corrections**.
5. Run a fresh extraction again.

If the case is locked, first move it to Validation or Development. A result created before the latest truth update becomes stale and cannot count toward the gate.

### Step 6 - Choose the correct scoring action

The available scoring actions have different purposes:

- **Score current** compares the values currently saved on the document with gold truth. It is diagnostic only. Because those values may have been manually corrected, this score never counts toward the pilot gate.
- **Re-extract + score** runs extraction again from the original source file, then compares the new result with gold truth. A fresh locked rerun can count toward the pilot gate.
- **Run development/validation/locked suite** queues a fresh re-extraction and score for every gold document in the chosen split. This is the preferred way to establish a comparable baseline.

Use **Score current** to confirm comparisons or investigate data. Use **Re-extract + score** or a suite whenever measuring extractor performance.

### Step 7 - Run a benchmark suite

1. In **Benchmark suite runner**, choose the dataset split.
2. Confirm that the displayed split contains documents.
3. Select **Run [split] suite**.
4. Keep the extraction worker running while the suite processes.
5. Monitor the progress bar and current document.
6. Wait for a final status.

Only one suite may be queued or running at a time.

Suite statuses mean:

- **Queued**: waiting for the worker.
- **Running**: one or more documents are being re-extracted.
- **Completed**: every item finished processing.
- **Completed with errors**: scoring completed where possible, but some items had operational failures.
- **Cancelled**: the suite was stopped; unprocessed items were skipped.
- **Failed**: the suite could not complete normally.

The suite result separates:

- **Pilot-quality documents**: at least 95% accurate, no more than two corrections, and zero critical errors.
- **Extracted, needs work**: extraction ran, but the document did not meet all three per-document quality conditions.
- **Operational failures**: the file could not be processed or scored. These are technical failures, not low extraction scores.

Use **Retry failed** after fixing an operational problem. Do not use retry to conceal a genuine low-accuracy result.

## 8. Reading the dashboard

### Summary cards

- **Atomic accuracy**: correct atomic values divided by all scored atomic values in eligible fresh locked reruns.
- **At most 2 corrections**: percentage of eligible locked documents with two or fewer discrepancies.
- **Fresh locked reruns**: number of locked documents with a qualifying run compared with the minimum of 120.
- **Pilot gate**:
  - **Building sample** means coverage, freshness, or sample requirements are incomplete.
  - **Below 95%** means the sample is ready but one or more accuracy, confidence, stratum, or critical-error conditions failed.
  - **Passed** means every enforced pilot condition passed.

### Gate-integrity warnings

- **Pending fresh rerun**: locked truth exists but no qualifying fresh extraction has been scored.
- **Truth snapshots need recapture**: the stored gold schema is outdated; move the case out of Locked if necessary, review it, and capture corrections.
- **Stale**: extraction was run before the latest gold-truth update.
- **Diagnostic only**: only a score of currently stored values exists; perform a fresh rerun.

### Coverage and accuracy by document type

This table is the quickest way to find an underrepresented or failing stratum. A stratum remains **Building** until it has 20 locked documents and every one has a fresh score. **Below gate** means coverage is complete but accuracy, two-correction rate, or critical-error performance failed. Every row must show **Passed** for the overall gate to pass.

### Highest-impact extraction corrections

This table ranks mismatched fields from fresh locked reruns. Prioritise fields with critical corrections, then failures affecting the most documents. Diagnose patterns by source format, document kind, supplier, layout, and extractor version before changing the extractor.

## 9. Failure-improvement loop

When a development or validation suite finds errors:

1. Confirm that gold truth is correct.
2. Separate extraction errors from operational failures.
3. Group similar discrepancies by field, document type, source format, supplier, and layout.
4. Fix the general extraction cause rather than hard-coding a single document whenever possible.
5. Add tests and similar development examples for the failure.
6. Rerun the affected development documents.
7. Run the full development suite to check for regressions.
8. Run validation after development is stable.

When a locked suite finds errors:

1. Record and retain the failed example as a permanent regression case.
2. Do not edit the locked truth merely to make the score pass.
3. Move the known example out of the final holdout before using it to guide a fix.
4. Add similar examples to Development.
5. Implement and verify the general fix.
6. Replace affected locked cases with new unseen documents in the same strata.
7. Freeze the new extractor version and rerun the complete locked suite.

## 10. Rules that protect the result

- Always correct the source review before capturing gold truth.
- Never treat model confidence as accuracy.
- Never use **Score current** as pilot evidence.
- Never tune against locked documents and then continue to call them unseen.
- Never change gold truth simply because the extractor disagrees with it.
- Never mix account allocation or supplier-master preferences into document source truth.
- Never count unreadable or incomplete files as evidence of normal document accuracy; track them separately as input-quality or operational failures.
- Keep the worker running for suites and record the extractor version used.
- Preserve a repeatable regression set even after a failing case leaves the locked holdout.

## 11. Troubleshooting

### The Lab is not visible

The signed-in user must be a Platform Owner. Organisation-admin permission alone is not sufficient.

### “Suite storage is not available yet”

Apply the invoice benchmark-suite SQL migration, then restart the backend.

### Run suite is disabled

The selected split contains no gold documents, another suite is active, or a request is still being processed.

### A suite remains queued

Confirm that the backend extraction worker is running and connected to the same database as the web application.

### A document says “Needs recapture”

Its gold snapshot uses an older benchmark schema. Move it out of Locked if necessary, review it, select **Capture corrections**, and perform a fresh rerun.

### Capture as gold is rejected

Return to the document and complete the required source fields and line-item data. Save the document before trying again.

### The score is unexpectedly high after manual corrections

Check the result badge. **Diagnostic** means current saved values were scored and the result does not count. Use **Re-extract + score** to test the extractor from the original file.

### The pilot gate remains “Building sample”

Check all six coverage rows, pending fresh reruns, stale results, and outdated gold truth. The locked set must contain at least 20 freshly scored cases in each stratum.

### The pilot gate says “Below 95%” even when the headline is 95%

Review critical errors, the two-correction measure, each stratum, and the statistical lower confidence bounds. All conditions must pass; the headline average alone is insufficient.

## 12. Pilot completion and ongoing monitoring

When the Pilot gate shows **Passed**:

1. Record the extractor version, completion date, locked sample composition, and result.
2. Preserve the locked evidence and suite results.
3. Begin the pilot without changing the extraction version that passed.
4. Manually verify the first 100 live pilot documents.
5. Track correction count, critical errors, document type, format, supplier/layout family, and extractor version.
6. Pause the 95% claim and investigate if verified live performance or its lower confidence bound falls below the gate.

Passing the Lab is a release gate, not a guarantee that every future document will be correct. Users must continue reviewing extracted documents, particularly banking information, VAT, totals, and low-quality sources.
