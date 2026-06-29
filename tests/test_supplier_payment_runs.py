import pytest
from fastapi import HTTPException

from app.routers import supplier_payment_runs
from app.services.supplier_payment_runs import (
    build_supplier_payment_run,
    create_supplier_payment_run_draft,
    generate_supplier_payment_run_remittances,
)


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.in_filters = []
        self.order_fields = []
        self._limit = None
        self.insert_payload = None

    def select(self, *_args, **_kwargs):
        return self

    def insert(self, payload):
        self.insert_payload = payload
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def in_(self, field, values):
        self.in_filters.append((field, {str(value) for value in values}))
        return self

    def order(self, field, **_kwargs):
        self.order_fields.append(field)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        if self.insert_payload is not None:
            table = self.db.tables.setdefault(self.table_name, [])
            payloads = self.insert_payload if isinstance(self.insert_payload, list) else [self.insert_payload]
            inserted = []
            for payload in payloads:
                row = dict(payload)
                row.setdefault("id", f"{self.table_name}-{len(table) + 1}")
                table.append(row)
                inserted.append(row)
            return _Response(inserted)

        rows = list(self.db.tables.get(self.table_name, []))
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self.in_filters:
            rows = [row for row in rows if str(row.get(field)) in values]
        for field in reversed(self.order_fields):
            rows = sorted(rows, key=lambda row: (row.get(field) is None, row.get(field)))
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self, name)


def _base_tables():
    return {
        "organisation_users": [
            {
                "organisation_id": "org-1",
                "user_id": "viewer-1",
                "status": "active",
                "role": "viewer",
                "permissions": {},
            }
        ],
        "suppliers": [
            {
                "id": "supplier-1",
                "organisation_id": "org-1",
                "supplier_name": "Acme Supplies",
                "trading_name": None,
                "supplier_code": "ACME",
                "default_email": "hello@acme.test",
                "accounting_email": "accounts@acme.test",
                "bank_name": "Example Bank",
                "bank_account_name": "Acme Supplies",
                "bank_account_number": "123456789",
                "bank_branch_code": "250655",
            },
            {
                "id": "supplier-2",
                "organisation_id": "org-1",
                "supplier_name": "Beta Services",
                "trading_name": None,
                "supplier_code": "BETA",
                "default_email": "hello@beta.test",
                "accounting_email": None,
                "bank_name": None,
                "bank_account_name": None,
                "bank_account_number": None,
                "bank_branch_code": None,
            },
        ],
        "invoices_extracted": [
            {
                "id": "inv-current",
                "organisation_id": "org-1",
                "supplier_id": "supplier-1",
                "supplier_name_extracted": "Acme OCR",
                "invoice_number": "CUR-1",
                "invoice_date": "2026-06-20",
                "due_date": "2026-07-20",
                "total_amount": 115,
                "currency": "ZAR",
                "posting_status": "posted",
                "document_type": "tax_invoice",
            },
            {
                "id": "inv-soon",
                "organisation_id": "org-1",
                "supplier_id": "supplier-1",
                "supplier_name_extracted": "Acme OCR",
                "invoice_number": "SOON-1",
                "invoice_date": "2026-06-01",
                "due_date": "2026-06-28",
                "total_amount": 500,
                "currency": "ZAR",
                "posting_status": "posted",
                "document_type": "tax_invoice",
            },
            {
                "id": "inv-45",
                "organisation_id": "org-1",
                "supplier_id": "supplier-1",
                "supplier_name_extracted": "Acme OCR",
                "invoice_number": "OLD-45",
                "invoice_date": "2026-04-01",
                "due_date": "2026-05-11",
                "total_amount": 200,
                "currency": "ZAR",
                "posting_status": "posted",
                "document_type": "tax_invoice",
            },
            {
                "id": "inv-100",
                "organisation_id": "org-1",
                "supplier_id": "supplier-2",
                "supplier_name_extracted": "Beta OCR",
                "invoice_number": "OLD-100",
                "invoice_date": "2026-02-01",
                "due_date": "2026-03-17",
                "total_amount": 300,
                "currency": "ZAR",
                "posting_status": "posted",
                "document_type": "tax_invoice",
            },
            {
                "id": "receipt",
                "organisation_id": "org-1",
                "supplier_id": "supplier-1",
                "invoice_number": "REC",
                "invoice_date": "2026-05-01",
                "due_date": "2026-05-01",
                "total_amount": 50,
                "posting_status": "posted",
                "document_type": "card_receipt",
            },
        ],
        "reconciliation_lines": [
            {
                "id": "pay-1",
                "organisation_id": "org-1",
                "invoice_extracted_id": "inv-45",
                "statement_line_id": "stmt-1",
                "payment_id": None,
                "match_status": "matched",
                "matched_amount": 50,
                "expected_amount": 200,
            },
            {
                "id": "pay-future",
                "organisation_id": "org-1",
                "invoice_extracted_id": "inv-100",
                "statement_line_id": "stmt-future",
                "payment_id": None,
                "match_status": "matched",
                "matched_amount": 300,
                "expected_amount": 300,
            },
        ],
        "statement_lines": [
            {"id": "stmt-1", "organisation_id": "org-1", "line_date": "2026-06-20"},
            {"id": "stmt-future", "organisation_id": "org-1", "line_date": "2026-07-01"},
        ],
        "payments": [],
    }


def test_supplier_payment_run_prioritises_due_and_bank_blocked_invoices():
    report = build_supplier_payment_run(
        _DB(_base_tables()),
        organisation_id="org-1",
        pay_on_date="2026-06-25",
        due_within_days=7,
    )

    assert report["summary"]["open_invoice_count"] == 4
    assert report["summary"]["selected_invoice_count"] == 3
    assert report["summary"]["blocked_invoice_count"] == 1
    assert report["summary"]["total_payable"] == 1065.0
    assert report["summary"]["selected_total"] == 650.0
    assert report["summary"]["blocked_total"] == 300.0
    assert report["summary"]["overdue_total"] == 450.0

    assert [row["invoice_number"] for row in report["selected_queue"]] == [
        "OLD-100",
        "OLD-45",
        "SOON-1",
    ]
    assert report["selected_queue"][0]["priority"] == "blocked"
    assert report["selected_queue"][0]["next_action"] == "Add or verify supplier banking details"

    acme = next(row for row in report["suppliers"] if row["supplier_code"] == "ACME")
    assert acme["email"] == "accounts@acme.test"
    assert acme["bank_ready"] is True
    assert acme["selected_invoice_count"] == 2
    assert acme["selected_total"] == 650.0

    assert any(warning["code"] == "receipts_excluded" for warning in report["warnings"])
    assert any(warning["code"] == "future_payments_excluded" for warning in report["warnings"])


def test_supplier_payment_run_rejects_invalid_date_and_window():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        build_supplier_payment_run(
            _DB(_base_tables()),
            organisation_id="org-1",
            pay_on_date="25/06/2026",
        )

    with pytest.raises(ValueError, match="between 0 and 90"):
        build_supplier_payment_run(
            _DB(_base_tables()),
            organisation_id="org-1",
            pay_on_date="2026-06-25",
            due_within_days=91,
        )


def test_supplier_payment_run_route_uses_org_read_permission(monkeypatch):
    def _deny(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="No access")

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("payment run service should not run without organisation access")

    monkeypatch.setattr(supplier_payment_runs, "ensure_org_read", _deny)
    monkeypatch.setattr(supplier_payment_runs, "build_supplier_payment_run", _fail_if_called)

    with pytest.raises(HTTPException) as exc:
        supplier_payment_runs.supplier_payment_run_preview(
            auth=("missing-user", _DB(_base_tables())),
            organisation_id="org-1",
            pay_on_date="2026-06-25",
            due_within_days=7,
        )

    assert exc.value.status_code == 403


def test_create_supplier_payment_run_draft_persists_bank_ready_selection():
    db = _DB(_base_tables())

    result = create_supplier_payment_run_draft(
        db,
        organisation_id="org-1",
        pay_on_date="2026-06-25",
        due_within_days=7,
        selected_invoice_ids=["inv-45", "inv-soon", "inv-soon"],
        created_by="viewer-1",
        notes="June supplier run",
    )

    run = result["payment_run"]
    assert run["status"] == "draft"
    assert run["organisation_id"] == "org-1"
    assert run["invoice_count"] == 2
    assert run["selected_total"] == 650.0
    assert run["notes"] == "June supplier run"
    assert run["summary"] == {
        "invoice_count": 2,
        "selected_total": 650.0,
        "currency": "ZAR",
    }

    assert db.tables["supplier_payment_runs"][0]["created_by"] == "viewer-1"
    assert {row["invoice_extracted_id"] for row in db.tables["supplier_payment_run_items"]} == {
        "inv-45",
        "inv-soon",
    }
    assert len(run["items"]) == 2


def test_create_supplier_payment_run_draft_rejects_blocked_and_missing_selection():
    with pytest.raises(ValueError, match="banking details"):
        create_supplier_payment_run_draft(
            _DB(_base_tables()),
            organisation_id="org-1",
            pay_on_date="2026-06-25",
            due_within_days=7,
            selected_invoice_ids=["inv-100"],
            created_by="viewer-1",
        )

    with pytest.raises(ValueError, match="no longer available"):
        create_supplier_payment_run_draft(
            _DB(_base_tables()),
            organisation_id="org-1",
            pay_on_date="2026-06-25",
            due_within_days=7,
            selected_invoice_ids=["missing-invoice"],
            created_by="viewer-1",
        )


def test_supplier_payment_run_draft_route_uses_org_write_permission(monkeypatch):
    def _deny(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="No write access")

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("draft service should not run without organisation write access")

    monkeypatch.setattr(supplier_payment_runs, "ensure_org_write", _deny)
    monkeypatch.setattr(supplier_payment_runs, "create_supplier_payment_run_draft", _fail_if_called)

    payload = supplier_payment_runs.SupplierPaymentRunDraftRequest(
        organisation_id="org-1",
        pay_on_date="2026-06-25",
        due_within_days=7,
        selected_invoice_ids=["inv-soon"],
    )
    with pytest.raises(HTTPException) as exc:
        supplier_payment_runs.create_supplier_payment_run_draft_route(
            payload=payload,
            auth=("viewer-1", _DB(_base_tables())),
        )

    assert exc.value.status_code == 403


def test_generate_supplier_payment_run_remittances_groups_by_supplier_and_is_idempotent():
    db = _DB(_base_tables())
    draft_result = create_supplier_payment_run_draft(
        db,
        organisation_id="org-1",
        pay_on_date="2026-06-25",
        due_within_days=7,
        selected_invoice_ids=["inv-45", "inv-soon"],
        created_by="viewer-1",
    )
    draft_id = draft_result["payment_run"]["id"]
    db.tables["supplier_payment_runs"][0]["status"] = "approved"

    first = generate_supplier_payment_run_remittances(
        db,
        draft_id,
        "org-1",
        generated_by="approver-1",
    )

    assert first["created_count"] == 1
    assert first["reused_count"] == 0
    remittance = first["remittances"][0]
    assert remittance["payment_run_id"] == draft_id
    assert remittance["supplier_id"] == "supplier-1"
    assert remittance["supplier_name"] == "Acme Supplies"
    assert remittance["supplier_email"] == "accounts@acme.test"
    assert remittance["remittance_status"] == "draft"
    assert remittance["total_amount"] == 650.0
    assert remittance["invoice_count"] == 2
    assert {ref["invoice_number"] for ref in remittance["invoice_references"]} == {
        "OLD-45",
        "SOON-1",
    }

    second = generate_supplier_payment_run_remittances(
        db,
        draft_id,
        "org-1",
        generated_by="approver-1",
    )

    assert second["created_count"] == 0
    assert second["reused_count"] == 1
    assert len(db.tables["remittances"]) == 1


def test_generate_supplier_payment_run_remittances_requires_approved_run():
    db = _DB(_base_tables())
    draft_result = create_supplier_payment_run_draft(
        db,
        organisation_id="org-1",
        pay_on_date="2026-06-25",
        due_within_days=7,
        selected_invoice_ids=["inv-45"],
        created_by="viewer-1",
    )

    with pytest.raises(ValueError, match="approved"):
        generate_supplier_payment_run_remittances(
            db,
            draft_result["payment_run"]["id"],
            "org-1",
            generated_by="approver-1",
        )


def test_supplier_payment_run_remittance_route_uses_org_write_permission(monkeypatch):
    def _deny(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="No write access")

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("remittance service should not run without organisation write access")

    monkeypatch.setattr(supplier_payment_runs, "ensure_org_write", _deny)
    monkeypatch.setattr(
        supplier_payment_runs,
        "generate_supplier_payment_run_remittances",
        _fail_if_called,
    )

    payload = supplier_payment_runs.DraftActionRequest(organisation_id="org-1")
    with pytest.raises(HTTPException) as exc:
        supplier_payment_runs.generate_remittances(
            payload=payload,
            auth=("viewer-1", _DB(_base_tables())),
            draft_id="run-1",
        )

    assert exc.value.status_code == 403
