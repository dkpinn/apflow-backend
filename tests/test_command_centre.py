import pytest
from fastapi import HTTPException

from app.routers import command_centre
from app.services.command_centre import generate_command_centre


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, value):
        self._limit = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        rows = self.rows
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _tables():
    return {
        "bank_statement_lines": [
            {
                "id": "bank-old",
                "organisation_id": "org-1",
                "line_date": "2026-06-01",
                "posting_status": "unposted",
                "allocation_status": "unallocated",
                "review_status": "pending",
            },
            {
                "id": "bank-done",
                "organisation_id": "org-1",
                "line_date": "2026-06-20",
                "posting_status": "posted",
                "allocation_status": "allocated",
                "review_status": "reviewed",
            },
        ],
        "bank_statement_uploads": [
            {
                "id": "upload-1",
                "organisation_id": "org-1",
                "extraction_status": "failed",
                "original_filename": "june.pdf",
            },
            {
                "id": "upload-bad-benchmark",
                "organisation_id": "org-1",
                "extraction_status": "extracted",
                "original_filename": "corrected.pdf",
            }
        ],
        "bank_statement_gold_files": [
            {
                "id": "gold-1",
                "organisation_id": "org-1",
                "document_id": "corrected-fixture",
                "gold_json": {
                    "_apflow_source_upload_id": "upload-bad-benchmark",
                    "transactions": [{"transaction_index": 1}],
                },
            }
        ],
        "bank_statement_extraction_runs": [
            {
                "organisation_id": "org-1",
                "bank_statement_upload_id": "upload-bad-benchmark",
                "document_id": "corrected-fixture",
                "can_allocate": False,
                "created_at": "2026-06-24T10:00:00",
            }
        ],
        "bank_audit_events": [],
        "invoices_extracted": [
            {
                "id": "supplier-review",
                "organisation_id": "org-1",
                "due_date": "2026-06-15",
                "total_amount": "115.00",
                "review_status": "needs_info",
                "approval_status": "needs_info",
                "validation_status": "needs_review",
                "posting_status": "unposted",
                "document_type": "tax_invoice",
                "document_direction": "inbound",
            },
            {
                "id": "supplier-posted",
                "organisation_id": "org-1",
                "due_date": "2026-06-15",
                "total_amount": "80.00",
                "review_status": "approved",
                "approval_status": "approved",
                "validation_status": "passed",
                "posting_status": "posted",
                "document_type": "tax_invoice",
                "document_direction": "inbound",
            },
        ],
        "sales_invoices": [
            {
                "id": "customer-overdue",
                "organisation_id": "org-1",
                "due_date": "2026-06-10",
                "amount_outstanding": "230.00",
                "status": "issued",
                "payment_status": "overdue",
                "document_type": "invoice",
            },
            {
                "id": "customer-draft",
                "organisation_id": "org-1",
                "due_date": "2026-06-10",
                "amount_outstanding": "999.00",
                "status": "draft",
                "payment_status": "unpaid",
                "document_type": "invoice",
            },
        ],
        "approval_requests": [
            {"id": "approval-1", "organisation_id": "org-1", "status": "pending"},
            {"id": "approval-2", "organisation_id": "org-1", "status": "approved"},
        ],
        "gl_journals": [
            {"id": "draft-1", "organisation_id": "org-1", "status": "draft"},
            {"id": "posted-1", "organisation_id": "org-1", "status": "posted"},
        ],
        "bank_accounts": [
            {"id": "bank-1", "organisation_id": "org-1", "active": True, "gl_account_id": None},
            {"id": "bank-2", "organisation_id": "org-1", "active": True, "gl_account_id": "account-1"},
        ],
    }


def test_command_centre_counts_operational_queues_and_health_score():
    report = generate_command_centre(_DB(_tables()), organisation_id="org-1", as_at_date="2026-06-25")

    assert report["health"]["score"] < 100
    assert report["health"]["oldest_unreconciled_bank_age_days"] == 24
    assert report["summary"]["unreconciled_bank_transactions"] == 1
    assert report["summary"]["supplier_invoices_needing_review"] == 1
    assert report["summary"]["unposted_supplier_bills"] == 1
    assert report["summary"]["overdue_supplier_bills"] == 1
    assert report["summary"]["overdue_customer_invoices"] == 1
    assert report["summary"]["pending_approvals"] == 1
    assert report["summary"]["draft_journals"] == 1
    assert report["summary"]["failed_extractions"] == 1
    assert report["summary"]["bank_extraction_benchmark_exceptions"] == 1
    assert report["summary"]["missing_bank_mappings"] == 1
    assert report["summary"]["overdue_receivables_amount"] == 230.0
    assert report["summary"]["overdue_supplier_bill_amount"] == 115.0

    queue_counts = {queue["id"]: queue["count"] for queue in report["queues"]}
    assert queue_counts["unreconciled_bank"] == 1
    assert queue_counts["failed_extractions"] == 1
    assert queue_counts["bank_extraction_benchmarks"] == 1


def test_command_centre_route_uses_org_read_permission(monkeypatch):
    calls = []

    def _ensure(user_id, organisation_id):
        calls.append((user_id, organisation_id))
        raise HTTPException(status_code=403, detail="Nope")

    monkeypatch.setattr(command_centre, "ensure_org_read", _ensure)

    with pytest.raises(HTTPException) as exc:
        command_centre.command_centre(
            auth=("viewer-1", _DB(_tables())),
            organisation_id="org-1",
            as_at_date="2026-06-25",
        )

    assert exc.value.status_code == 403
    assert calls == [("viewer-1", "org-1")]
