from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.bank.extraction_gate import corrected_fixture_benchmark_blockers


def _rows(db, table: str, organisation_id: str, select: str = "*") -> list[dict[str, Any]]:
    return (
        db.table(table)
        .select(select)
        .eq("organisation_id", organisation_id)
        .limit(1000)
        .execute()
    ).data or []


def _status(value: Any, default: str = "") -> str:
    return str(value or default).strip().lower()


def _money(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_unreconciled_bank_line(row: dict[str, Any]) -> bool:
    review_status = _status(row.get("review_status"), "pending")
    if review_status in {"ignored", "deferred"}:
        return False
    return (
        _status(row.get("posting_status"), "unposted") != "posted"
        or _status(row.get("allocation_status"), "unallocated") not in {"allocated", "split"}
        or review_status != "reviewed"
    )


def _is_supplier_invoice_needing_review(row: dict[str, Any]) -> bool:
    review_status = _status(row.get("review_status"), "pending")
    approval_status = _status(row.get("approval_status"), "pending")
    validation_status = _status(row.get("validation_status"))
    posting_status = _status(row.get("posting_status"), "unposted")
    if posting_status == "posted":
        return False
    return (
        review_status in {"pending", "needs_info", "in_review"}
        or approval_status in {"pending", "needs_info"}
        or validation_status == "needs_review"
    )


def _is_supplier_bill(row: dict[str, Any]) -> bool:
    document_type = _status(row.get("document_type"), "tax_invoice")
    direction = _status(row.get("document_direction"))
    return document_type not in {"receipt", "credit_note"} and direction != "outbound"


def _queue(
    queue_id: str,
    label: str,
    count: int,
    severity: str,
    href: str,
    description: str,
) -> dict[str, Any]:
    return {
        "id": queue_id,
        "label": label,
        "count": count,
        "severity": severity,
        "href": href,
        "description": description,
    }


def _deduct(count: int, per_item: int, cap: int) -> int:
    return min(max(count, 0) * per_item, cap)


def _health_label(score: int) -> str:
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 50:
        return "Needs attention"
    return "Critical"


def _bank_uploads_with_benchmark_issues(
    db,
    *,
    organisation_id: str,
    bank_uploads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for upload in bank_uploads:
        upload_id = str(upload.get("id") or "")
        if not upload_id:
            continue
        blockers = corrected_fixture_benchmark_blockers(
            db,
            organisation_id=organisation_id,
            upload_id=upload_id,
        )
        if blockers:
            issues.append({**upload, "benchmark_blockers": blockers})
    return issues


def generate_command_centre(
    db,
    *,
    organisation_id: str,
    as_at_date: str | None = None,
) -> dict[str, Any]:
    today = _parse_date(as_at_date) or date.today()

    bank_lines = _rows(
        db,
        "bank_statement_lines",
        organisation_id,
        "id, line_date, posting_status, allocation_status, review_status",
    )
    bank_uploads = _rows(
        db,
        "bank_statement_uploads",
        organisation_id,
        "id, extraction_status, original_filename, uploaded_at",
    )
    supplier_invoices = _rows(
        db,
        "invoices_extracted",
        organisation_id,
        "id, due_date, total_amount, review_status, approval_status, validation_status, posting_status, document_type, document_direction",
    )
    sales_invoices = _rows(
        db,
        "sales_invoices",
        organisation_id,
        "id, due_date, amount_outstanding, status, payment_status, document_type",
    )
    approval_requests = _rows(
        db,
        "approval_requests",
        organisation_id,
        "id, status, requested_at",
    )
    draft_journals = _rows(
        db,
        "gl_journals",
        organisation_id,
        "id, status, source_type, created_at",
    )
    bank_accounts = _rows(
        db,
        "bank_accounts",
        organisation_id,
        "id, active, gl_account_id, name",
    )

    unreconciled_bank = [row for row in bank_lines if _is_unreconciled_bank_line(row)]
    bank_line_dates = [_parse_date(row.get("line_date")) for row in unreconciled_bank]
    oldest_bank_age_days = max(
        ((today - line_date).days for line_date in bank_line_dates if line_date and line_date <= today),
        default=0,
    )

    supplier_bills = [row for row in supplier_invoices if _is_supplier_bill(row)]
    invoices_needing_review = [row for row in supplier_bills if _is_supplier_invoice_needing_review(row)]
    unposted_supplier_bills = [
        row for row in supplier_bills if _status(row.get("posting_status"), "unposted") != "posted"
    ]
    overdue_supplier_bills = [
        row
        for row in unposted_supplier_bills
        if (due_date := _parse_date(row.get("due_date"))) is not None and due_date < today
    ]

    overdue_customers = [
        row
        for row in sales_invoices
        if _status(row.get("document_type"), "invoice") == "invoice"
        and _status(row.get("status")) == "issued"
        and _money(row.get("amount_outstanding")) > 0
        and (due_date := _parse_date(row.get("due_date"))) is not None
        and due_date < today
    ]

    pending_approvals = [row for row in approval_requests if _status(row.get("status")) == "pending"]
    draft_journal_rows = [row for row in draft_journals if _status(row.get("status")) == "draft"]
    failed_extractions = [
        row for row in bank_uploads if _status(row.get("extraction_status")) in {"failed", "needs_review"}
    ]
    benchmark_exceptions = _bank_uploads_with_benchmark_issues(
        db,
        organisation_id=organisation_id,
        bank_uploads=bank_uploads,
    )
    missing_bank_mappings = [
        row for row in bank_accounts if row.get("active") is not False and not row.get("gl_account_id")
    ]

    queues = [
        _queue(
            "unreconciled_bank",
            "Unreconciled bank transactions",
            len(unreconciled_bank),
            "critical" if oldest_bank_age_days > 14 else "warning",
            "/bank",
            "Bank lines that still need allocation, review, or posting.",
        ),
        _queue(
            "supplier_review",
            "Supplier invoices needing review",
            len(invoices_needing_review),
            "warning",
            "/invoices",
            "Captured supplier documents waiting for review, approval, or validation cleanup.",
        ),
        _queue(
            "unposted_supplier_bills",
            "Unposted supplier bills",
            len(unposted_supplier_bills),
            "warning",
            "/invoices",
            "Supplier bills not yet posted to the general ledger.",
        ),
        _queue(
            "overdue_customer_invoices",
            "Overdue customer invoices",
            len(overdue_customers),
            "critical" if overdue_customers else "info",
            "/customers",
            "Issued customer invoices past due with an outstanding balance.",
        ),
        _queue(
            "pending_approvals",
            "Pending approvals",
            len(pending_approvals),
            "warning",
            "/approvals",
            "Approval requests waiting for action.",
        ),
        _queue(
            "draft_journals",
            "Draft journals",
            len(draft_journal_rows),
            "warning",
            "/reports/transactions",
            "Journals created but not posted.",
        ),
        _queue(
            "failed_extractions",
            "Failed bank extractions",
            len(failed_extractions),
            "critical" if failed_extractions else "info",
            "/bank",
            "Bank uploads that failed extraction or need extraction review.",
        ),
        _queue(
            "bank_extraction_benchmarks",
            "Bank extraction benchmark exceptions",
            len(benchmark_exceptions),
            "critical" if benchmark_exceptions else "info",
            "/bank",
            "Corrected bank statement fixtures that are missing or failing parser benchmarks.",
        ),
        _queue(
            "missing_bank_mappings",
            "Missing bank GL mappings",
            len(missing_bank_mappings),
            "warning",
            "/bank",
            "Active bank/cash accounts without a linked general-ledger account.",
        ),
    ]

    score = 100
    score -= _deduct(len(unreconciled_bank), 4, 32)
    score -= _deduct(len(invoices_needing_review), 3, 24)
    score -= _deduct(len(unposted_supplier_bills), 2, 20)
    score -= _deduct(len(overdue_customers), 3, 18)
    score -= _deduct(len(pending_approvals), 2, 12)
    score -= _deduct(len(draft_journal_rows), 2, 12)
    score -= _deduct(len(failed_extractions), 6, 18)
    score -= _deduct(len(benchmark_exceptions), 8, 24)
    score -= _deduct(len(missing_bank_mappings), 4, 16)
    if oldest_bank_age_days > 30:
        score -= 10
    elif oldest_bank_age_days > 14:
        score -= 5
    score = max(0, min(100, score))

    overdue_receivables_amount = sum((_money(row.get("amount_outstanding")) for row in overdue_customers), Decimal("0"))
    overdue_supplier_bill_amount = sum((_money(row.get("total_amount")) for row in overdue_supplier_bills), Decimal("0"))

    return {
        "organisation_id": organisation_id,
        "as_at_date": today.isoformat(),
        "health": {
            "score": score,
            "label": _health_label(score),
            "oldest_unreconciled_bank_age_days": oldest_bank_age_days,
        },
        "summary": {
            "unreconciled_bank_transactions": len(unreconciled_bank),
            "supplier_invoices_needing_review": len(invoices_needing_review),
            "unposted_supplier_bills": len(unposted_supplier_bills),
            "overdue_supplier_bills": len(overdue_supplier_bills),
            "overdue_customer_invoices": len(overdue_customers),
            "pending_approvals": len(pending_approvals),
            "draft_journals": len(draft_journal_rows),
            "failed_extractions": len(failed_extractions),
            "bank_extraction_benchmark_exceptions": len(benchmark_exceptions),
            "missing_bank_mappings": len(missing_bank_mappings),
            "overdue_receivables_amount": float(overdue_receivables_amount),
            "overdue_supplier_bill_amount": float(overdue_supplier_bill_amount),
        },
        "queues": queues,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "disclaimer": (
            "Command Centre queues use existing operational records. Supplier payment status is "
            "approximated from review/posting due dates until a dedicated AP settlement state is available."
        ),
    }
