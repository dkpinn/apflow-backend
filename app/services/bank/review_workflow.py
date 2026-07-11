"""Bank statement extraction review-workflow logic.

Pure(ish) helpers that decide whether a bank statement upload can be approved:
the hard/soft blocker rules, reviewer-identity and attestation checks, the
gold-fixture/benchmark status, the row-level review snapshot, and the gold-file
draft. Extracted from the bank_uploads router so this logic can be unit-tested
without the HTTP layer. The only database access here is read-only lookups plus
the single-user-org count.
"""
from __future__ import annotations

from pathlib import Path

from app.schemas.bank import ApproveExtractionRequest
from app.services.bank.extraction_gate import (
    corrected_fixture_benchmark_blockers,
    corrected_fixture_rows_for_upload,
    upload_requires_corrected_fixture,
)


# --- generic line accessors -------------------------------------------------

def line_signed_amount(line) -> float:
    if isinstance(line, dict):
        return float(line.get("signed_amount") or 0)
    return float(getattr(line, "signed_amount", 0) or 0)


def line_value(line, key: str, default=None):
    if isinstance(line, dict):
        return line.get(key, default)
    return getattr(line, key, default)


def numeric_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- review snapshot --------------------------------------------------------

def build_review_snapshot_line(wrapper: dict, row_number: int) -> dict:
    line = wrapper["line"]
    duplicate_status = wrapper.get("duplicate_status") or "clear"
    signed_amount = line_signed_amount(line)
    if duplicate_status != "clear":
        import_status = "duplicate_filtered"
    elif signed_amount == 0:
        import_status = "dropped_zero_or_opening"
    else:
        import_status = "stored"
    return {
        "row_number": row_number,
        "import_status": import_status,
        "duplicate_status": duplicate_status,
        "line_date": line_value(line, "line_date"),
        "value_date": line_value(line, "value_date"),
        "description": line_value(line, "description"),
        "reference": line_value(line, "reference"),
        "counterparty": line_value(line, "counterparty"),
        "debit_amount": numeric_or_none(line_value(line, "debit_amount")),
        "credit_amount": numeric_or_none(line_value(line, "credit_amount")),
        "signed_amount": signed_amount,
        "balance_amount": numeric_or_none(line_value(line, "balance_amount")),
        "currency": line_value(line, "currency"),
        "source_page": line_value(line, "source_page"),
        "source_row_index": line_value(line, "source_row_index"),
        "extraction_confidence": numeric_or_none(line_value(line, "extraction_confidence")),
        "extraction_warnings": line_value(line, "extraction_warnings", []) or [],
    }


# --- gold-file draft --------------------------------------------------------

def gold_document_id(upload: dict) -> str:
    filename = str(upload.get("original_filename") or upload.get("id") or "bank-statement")
    stem = Path(filename).stem or "bank-statement"
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in stem).strip("-")
    return clean or str(upload.get("id") or "bank-statement")


def gold_transaction_from_review_line(line: dict, transaction_index: int) -> dict:
    return {
        "transaction_index": transaction_index,
        "date": line.get("line_date"),
        "description": line.get("description") or "",
        "amount": line.get("signed_amount"),
        "debit": line.get("debit_amount"),
        "credit": line.get("credit_amount"),
        "running_balance": line.get("balance_amount"),
        "page_number": line.get("source_page"),
        "source_reference": (
            f"row-{line.get('source_row_index')}"
            if line.get("source_row_index") not in (None, "")
            else f"extracted-row-{line.get('row_number') or transaction_index}"
        ),
    }


def gold_draft_from_upload(upload: dict, account: dict) -> dict:
    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    review_snapshot = evidence.get("review_snapshot") if isinstance(evidence.get("review_snapshot"), dict) else {}
    snapshot_lines = review_snapshot.get("lines") if isinstance(review_snapshot.get("lines"), list) else []
    transactions = []
    for line in snapshot_lines:
        if not isinstance(line, dict):
            continue
        if line.get("import_status") == "dropped_zero_or_opening":
            continue
        if numeric_or_none(line.get("signed_amount")) == 0:
            continue
        transactions.append(gold_transaction_from_review_line(line, len(transactions) + 1))
    return {
        "document_id": gold_document_id(upload),
        "bank": account.get("institution_name"),
        "account_type": account.get("account_type"),
        "document_variant": upload.get("source_format") or "bank_upload",
        "statement_start_date": upload.get("statement_period_from"),
        "statement_end_date": upload.get("statement_period_to"),
        "opening_balance": upload.get("opening_balance"),
        "closing_balance": upload.get("closing_balance"),
        "transactions": transactions,
        "source_upload_id": upload.get("id"),
        "source_filename": upload.get("original_filename"),
        "needs_manual_correction": True,
    }


def gold_draft_or_none(upload: dict, account: dict) -> dict | None:
    draft = gold_draft_from_upload(upload, account)
    return draft if draft["transactions"] else None


# --- approval blockers ------------------------------------------------------

def soft_blockers(upload: dict) -> list[str]:
    """Balance signals the reviewer's attestation can override.

    For a statement in manual review, the human comparing against the source
    document is the authority — these automatic checks are shown as warnings, not
    hard blocks, so a poorly-read scan doesn't trap the reviewer forever. They are
    covered by the reviewer's `balances_checked` attestation at approval time.
    """
    warnings: list[str] = []
    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    validation = evidence.get("validation") if isinstance(evidence.get("validation"), dict) else {}
    running_balance = evidence.get("running_balance") if isinstance(evidence.get("running_balance"), dict) else {}

    if upload.get("balance_status") != "balanced":
        warnings.append("Statement opening/closing balances are not reconciled")
    if validation.get("closing_balance_passed") is not True:
        warnings.append("Closing balance validation has not passed")
    if validation.get("running_balance_passed") is not True:
        warnings.append("Running balance validation has not passed")
    if running_balance.get("balance_walk_status") not in {None, "balanced"}:
        warnings.append("Running balance walk has not passed")
    return warnings


def structural_blockers(upload: dict, db=None, reviewer_id: str | None = None, is_single_user: bool = False) -> list[str]:
    """Hard blockers that no attestation can wave away.

    These are structural integrity problems (nothing to import, an incomplete
    review snapshot, unresolved duplicates) plus — only under strict gold-fixture
    mode — the fixture/benchmark/independent-verifier controls.
    """
    blockers: list[str] = []
    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    duplicate_summary = upload.get("duplicate_summary") if isinstance(upload.get("duplicate_summary"), dict) else {}

    if int(upload.get("extracted_line_count") or 0) <= 0:
        blockers.append("No importable transaction rows were stored")
    review_snapshot = evidence.get("review_snapshot") if isinstance(evidence.get("review_snapshot"), dict) else {}
    review_lines = review_snapshot.get("lines") if isinstance(review_snapshot.get("lines"), list) else []
    raw_count = int(evidence.get("raw_extracted_transaction_count") or 0)
    if raw_count and len(review_lines) != raw_count:
        blockers.append("Row-level extraction review snapshot is incomplete")
    duplicate_count = int(
        upload.get("duplicate_line_count")
        or duplicate_summary.get("duplicate_line_count")
        or 0
    )
    if duplicate_count:
        blockers.append("Duplicate transaction rows are still present")
    if db is not None:
        fixture_rows = corrected_fixture_rows_for_upload(
            db,
            organisation_id=str(upload.get("organisation_id")),
            upload_id=str(upload.get("id")),
        )
        if upload_requires_corrected_fixture(upload) and not fixture_rows:
            blockers.append(
                "PDF/image/VLM bank statement extraction requires a corrected gold fixture and passing benchmark"
            )
        if upload_requires_corrected_fixture(upload) and fixture_rows and reviewer_id and not is_single_user:
            has_independent_verifier = any(
                row.get("verified_by") and str(row.get("verified_by")) != str(reviewer_id)
                for row in fixture_rows
            )
            if not has_independent_verifier:
                blockers.append(
                    "PDF/image/VLM bank statement extraction approval must be performed by a reviewer different from the corrected gold fixture verifier"
                )
        # Benchmark freshness only gates approval when gold fixtures are a hard
        # requirement. In the default optional/internal mode a saved-but-unbenchmarked
        # gold file must not block approval of an eyeballed extraction.
        if upload_requires_corrected_fixture(upload):
            blockers.extend(
                corrected_fixture_benchmark_blockers(
                    db,
                    organisation_id=str(upload.get("organisation_id")),
                    upload_id=str(upload.get("id")),
                )
            )
    return blockers


def is_single_user_org(db, organisation_id: str) -> bool:
    result = (
        db.table("organisation_users")
        .select("id", count="exact")
        .eq("organisation_id", organisation_id)
        .execute()
    )
    # Prefer the exact server count, but fall back to the returned rows when the
    # driver does not populate `count` (e.g. some stubs / older postgrest).
    member_count = result.count
    if member_count is None:
        member_count = len(result.data or [])
    return member_count <= 1


def reviewer_identity_blockers(upload: dict, reviewer_id: str, is_single_user: bool = False) -> list[str]:
    if is_single_user:
        return []
    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    extracted_by = evidence.get("extracted_by")
    if reviewer_id in {upload.get("uploaded_by"), extracted_by}:
        return ["Bank statement extraction must be approved by a different reviewer"]
    return []


def attestation_blockers(payload: ApproveExtractionRequest) -> list[str]:
    required_checks = [
        (payload.source_document_checked, "Reviewer must confirm the source document was opened and checked"),
        (payload.transaction_count_checked, "Reviewer must confirm the extracted transaction count matches the source"),
        (payload.amounts_and_dates_checked, "Reviewer must confirm extracted dates, descriptions, and amounts match the source"),
        (payload.balances_checked, "Reviewer must confirm opening, closing, and running balances reconcile"),
    ]
    return [message for passed, message in required_checks if not passed]


# --- gold-file / benchmark freshness ---------------------------------------

def timestamp_key(row: dict, *fields: str) -> str:
    for field in fields:
        value = row.get(field)
        if value:
            return str(value)
    return ""


def latest_gold_file(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return max(rows, key=lambda row: (timestamp_key(row, "verified_at", "created_at"), str(row.get("id") or "")))


def latest_benchmark_run_for_gold_file(
    db,
    *,
    organisation_id: str,
    upload_id: str,
    gold_file: dict | None,
) -> dict | None:
    if not gold_file or not gold_file.get("document_id"):
        return None
    runs = (
        db.table("bank_statement_extraction_runs")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_upload_id", upload_id)
        .eq("document_id", gold_file["document_id"])
        .order("created_at")
        .execute()
        .data
        or []
    )
    if not runs:
        return None
    return max(runs, key=lambda row: (timestamp_key(row, "created_at"), str(row.get("id") or "")))


def benchmark_status(
    *,
    upload: dict,
    requires_fixture: bool,
    latest_gold_file: dict | None,
    latest_benchmark_run: dict | None,
) -> str:
    if not requires_fixture and not latest_gold_file:
        return "not_required"
    if requires_fixture and not latest_gold_file:
        return "missing_gold_file"
    if latest_gold_file and not latest_benchmark_run:
        return "not_run"

    run_created_at = timestamp_key(latest_benchmark_run or {}, "created_at")
    gold_verified_at = timestamp_key(latest_gold_file or {}, "verified_at", "created_at")
    upload_extracted_at = timestamp_key(upload, "extracted_at")
    if gold_verified_at and (not run_created_at or run_created_at < gold_verified_at):
        return "stale_after_gold_correction"
    if upload_extracted_at and (not run_created_at or run_created_at < upload_extracted_at):
        return "stale_after_latest_extraction"
    return "passed" if latest_benchmark_run and latest_benchmark_run.get("can_allocate") is True else "failed"


# --- top-level workflow state ----------------------------------------------

def review_workflow_state(
    db,
    *,
    organisation_id: str,
    upload: dict,
    account: dict,
    reviewer_id: str,
    is_single_user: bool = False,
) -> dict:
    fixture_rows = corrected_fixture_rows_for_upload(
        db,
        organisation_id=organisation_id,
        upload_id=str(upload.get("id")),
    )
    gold_file = latest_gold_file(fixture_rows)
    benchmark_run = latest_benchmark_run_for_gold_file(
        db,
        organisation_id=organisation_id,
        upload_id=str(upload.get("id")),
        gold_file=gold_file,
    )
    approval_blockers = (
        reviewer_identity_blockers(upload, reviewer_id, is_single_user=is_single_user)
        if upload.get("extraction_status") == "needs_review"
        else []
    ) + structural_blockers(upload, db, reviewer_id=reviewer_id, is_single_user=is_single_user)
    approval_warnings = soft_blockers(upload)
    requires_fixture = upload_requires_corrected_fixture(upload)
    status = benchmark_status(
        upload=upload,
        requires_fixture=requires_fixture,
        latest_gold_file=gold_file,
        latest_benchmark_run=benchmark_run,
    )
    gold_draft = gold_draft_or_none(upload, account)
    has_independent_gold_verifier = any(
        row.get("verified_by") and str(row.get("verified_by")) != str(reviewer_id)
        for row in fixture_rows
    )
    return {
        "route_hint": {
            "bank_cash_review_path": (
                f"/bank-cash/accounts/{upload.get('bank_account_id')}/"
                f"uploads/{upload.get('id')}/review"
            ),
            "action_label": "Review Extraction",
            "show_review_action": (
                str(upload.get("extraction_status") or "").lower() in {"needs_review", "failed"}
                or bool(approval_blockers)
            ),
        },
        "gold_draft": gold_draft,
        "latest_gold_file": gold_file,
        "latest_benchmark_run": benchmark_run,
        "requires_corrected_fixture": requires_fixture,
        "has_corrected_fixture": bool(fixture_rows),
        "has_independent_gold_verifier": has_independent_gold_verifier,
        "benchmark_status": status,
        "approval_blockers": approval_blockers,
        "approval_warnings": approval_warnings,
        "actions": {
            "can_save_gold_file": gold_draft is not None,
            "can_run_benchmark": gold_file is not None,
            "can_approve": (
                upload.get("extraction_status") == "needs_review"
                and not approval_blockers
                and (not requires_fixture or status in {"passed", "not_required"})
            ),
        },
    }
