import pytest
from fastapi import HTTPException

from app.routers import customer_collections
from app.services.customer_collections import build_customer_collections


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self.in_filters = []
        self.order_fields = []
        self._limit = None

    def select(self, *_args, **_kwargs):
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
        rows = self.rows
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
        return _Query(self.tables.get(name, []))


def _invoice(
    invoice_id,
    *,
    customer_id="customer-1",
    number="INV",
    issue_date="2026-06-01",
    due_date="2026-07-01",
    total=100,
    paid=0,
    credited=0,
    outstanding=100,
    status="issued",
    payment_status="unpaid",
    document_type="invoice",
):
    return {
        "id": invoice_id,
        "organisation_id": "org-1",
        "customer_id": customer_id,
        "invoice_number": number,
        "issue_date": issue_date,
        "due_date": due_date,
        "currency": "ZAR",
        "total_amount": total,
        "amount_paid": paid,
        "amount_credited": credited,
        "amount_outstanding": outstanding,
        "status": status,
        "payment_status": payment_status,
        "document_type": document_type,
        "customer_reference": None,
    }


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
        "customers": [
            {
                "id": "customer-1",
                "organisation_id": "org-1",
                "legal_name": "Alpha Customer",
                "trading_name": None,
                "customer_code": "ALPHA",
                "email": "hello@alpha.test",
                "billing_email": None,
                "accounts_email": "accounts@alpha.test",
                "phone": "011 000 0000",
            },
            {
                "id": "customer-2",
                "organisation_id": "org-1",
                "legal_name": "Beta Customer",
                "trading_name": None,
                "customer_code": "BETA",
                "email": "hello@beta.test",
                "billing_email": None,
                "accounts_email": None,
                "phone": None,
            },
        ],
        "sales_invoices": [
            _invoice("current", number="CUR-1", issue_date="2026-06-20", due_date="2026-07-20", total=115, outstanding=115),
            _invoice("due-soon", number="SOON-1", issue_date="2026-06-01", due_date="2026-06-28", total=500, outstanding=500),
            _invoice(
                "old-45",
                number="OLD-45",
                issue_date="2026-04-01",
                due_date="2026-05-11",
                total=200,
                paid=50,
                outstanding=150,
                payment_status="partial",
            ),
            _invoice(
                "old-100",
                customer_id="customer-2",
                number="OLD-100",
                issue_date="2026-02-01",
                due_date="2026-03-17",
                total=300,
                outstanding=300,
                payment_status="overdue",
            ),
            _invoice("paid", number="PAID", due_date="2026-05-31", outstanding=0, payment_status="paid"),
            _invoice("draft", number="DRAFT", status="draft", due_date="2026-05-31", outstanding=999),
            _invoice("future", number="FUT", issue_date="2026-07-01", due_date="2026-07-31", outstanding=500),
            _invoice("credit", number="CN-1", due_date="2026-05-31", outstanding=100, document_type="credit_note"),
        ],
    }


def test_customer_collections_builds_prioritised_follow_up_queue():
    report = build_customer_collections(
        _DB(_base_tables()),
        organisation_id="org-1",
        as_at_date="2026-06-25",
        due_soon_days=7,
    )

    assert report["summary"]["open_invoice_count"] == 4
    assert report["summary"]["follow_up_count"] == 3
    assert report["summary"]["overdue_invoice_count"] == 2
    assert report["summary"]["due_soon_invoice_count"] == 1
    assert report["summary"]["total_outstanding"] == 1065.0
    assert report["summary"]["overdue_outstanding"] == 450.0

    assert [row["invoice_number"] for row in report["follow_up_queue"]] == [
        "OLD-100",
        "OLD-45",
        "SOON-1",
    ]
    assert report["follow_up_queue"][0]["priority"] == "urgent"
    assert report["follow_up_queue"][0]["next_action"] == "Call customer and send final reminder"

    alpha = next(row for row in report["customers"] if row["customer_code"] == "ALPHA")
    assert alpha["email"] == "accounts@alpha.test"
    assert alpha["open_invoice_count"] == 3
    assert alpha["follow_up_count"] == 2
    assert alpha["total_outstanding"] == 765.0

    assert any(warning["code"] == "zero_balance_excluded" for warning in report["warnings"])
    assert any(warning["code"] == "future_invoices_excluded" for warning in report["warnings"])


def test_customer_collections_rejects_invalid_date_and_due_window():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        build_customer_collections(
            _DB(_base_tables()),
            organisation_id="org-1",
            as_at_date="25/06/2026",
        )

    with pytest.raises(ValueError, match="between 0 and 90"):
        build_customer_collections(
            _DB(_base_tables()),
            organisation_id="org-1",
            as_at_date="2026-06-25",
            due_soon_days=91,
        )


def test_customer_collections_route_uses_org_read_permission(monkeypatch):
    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("collections service should not run without organisation access")

    def _deny(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="No access")

    monkeypatch.setattr(customer_collections, "ensure_org_read", _deny)
    monkeypatch.setattr(customer_collections, "build_customer_collections", _fail_if_called)

    with pytest.raises(HTTPException) as exc:
        customer_collections.customer_collections_dashboard(
            auth=("missing-user", _DB(_base_tables())),
            organisation_id="org-1",
            as_at_date="2026-06-25",
            due_soon_days=7,
        )

    assert exc.value.status_code == 403
