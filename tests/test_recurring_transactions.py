import pytest
from fastapi import HTTPException

from app.routers import recurring_transactions as recurring_router
from app.services import recurring_transactions as recurring_service


ORG_ID = "00000000-0000-0000-0000-000000000001"
USER_ID = "00000000-0000-0000-0000-000000000002"
TEMPLATE_ID = "00000000-0000-0000-0000-000000000003"
DRAFT_ID = "00000000-0000-0000-0000-000000000004"
SUPPLIER_ID = "00000000-0000-0000-0000-000000000005"


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.neq_filters = []
        self.lte_filters = []
        self.order_fields = []
        self._limit = None
        self.insert_payload = None
        self.update_payload = None
        self.single = False

    def select(self, *_args, **_kwargs):
        return self

    def insert(self, payload):
        self.insert_payload = payload
        return self

    def update(self, payload):
        self.update_payload = payload
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def neq(self, field, value):
        self.neq_filters.append((field, value))
        return self

    def lte(self, field, value):
        self.lte_filters.append((field, value))
        return self

    def order(self, field, **_kwargs):
        self.order_fields.append((field, bool(_kwargs.get("desc"))))
        return self

    def limit(self, value):
        self._limit = value
        return self

    def maybe_single(self):
        self.single = True
        return self

    def _matching_rows(self):
        rows = list(self.db.tables.get(self.table_name, []))
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, value in self.neq_filters:
            rows = [row for row in rows if row.get(field) != value]
        for field, value in self.lte_filters:
            rows = [row for row in rows if str(row.get(field) or "") <= value]
        for field, desc in reversed(self.order_fields):
            rows = sorted(rows, key=lambda row: (row.get(field) is None, row.get(field)), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows

    def execute(self):
        if self.insert_payload is not None:
            table = self.db.tables.setdefault(self.table_name, [])
            payloads = self.insert_payload if isinstance(self.insert_payload, list) else [self.insert_payload]
            inserted = []
            for payload in payloads:
                row = dict(payload)
                row.setdefault("id", f"{self.table_name}-{len(table) + len(inserted) + 1}")
                table.append(row)
                inserted.append(dict(row))
            return _Response(inserted)

        rows = self._matching_rows()
        if self.update_payload is not None:
            updated = []
            for row in self.db.tables.get(self.table_name, []):
                if row in rows:
                    row.update(self.update_payload)
                    updated.append(dict(row))
            return _Response(updated[0] if self.single and updated else updated)

        if self.single:
            return _Response(dict(rows[0]) if rows else None)
        return _Response([dict(row) for row in rows])


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self, name)


def _template(**overrides):
    return {
        "id": TEMPLATE_ID,
        "organisation_id": ORG_ID,
        "name": "Monthly rent",
        "transaction_type": "supplier_invoice",
        "schedule_type": "monthly",
        "schedule_day": 1,
        "amount": 500,
        "currency": "ZAR",
        "supplier_id": SUPPLIER_ID,
        "customer_id": None,
        "description": "Office rent",
        "reference": "RENT",
        "template_data": {"supplier_name": "Landlord"},
        "status": "active",
        "start_date": "2026-01-01",
        "end_date": None,
        "next_due_date": "2026-01-01",
        **overrides,
    }


def _draft(**overrides):
    return {
        "id": DRAFT_ID,
        "organisation_id": ORG_ID,
        "template_id": TEMPLATE_ID,
        "due_date": "2026-06-01",
        "transaction_type": "supplier_invoice",
        "amount": 500,
        "currency": "ZAR",
        "description": "Office rent",
        "reference": "RENT",
        "draft_data": {"supplier_id": SUPPLIER_ID, "supplier_name": "Landlord"},
        "status": "pending",
        **overrides,
    }


def _tables():
    return {
        "recurring_transaction_templates": [_template()],
        "recurring_transaction_drafts": [_draft()],
        "invoices_extracted": [],
        "sales_invoices": [],
    }


def test_list_templates_excludes_completed_templates_and_route_uses_read_permission(monkeypatch):
    calls = []
    monkeypatch.setattr(recurring_router, "ensure_org_read", lambda user_id, org_id: calls.append((user_id, org_id)))
    db = _DB({
        "recurring_transaction_templates": [
            _template(name="Active template"),
            _template(id="completed-template", name="Done template", status="completed"),
        ]
    })

    result = recurring_router.list_templates_route(auth=(USER_ID, db), organisation_id=ORG_ID)

    assert calls == [(USER_ID, ORG_ID)]
    assert result["success"] is True
    assert [row["name"] for row in result["templates"]] == ["Active template"]


def test_create_template_route_validates_and_persists_template(monkeypatch):
    calls = []
    monkeypatch.setattr(recurring_router, "ensure_org_write", lambda user_id, org_id: calls.append((user_id, org_id)))
    db = _DB({"recurring_transaction_templates": []})

    result = recurring_router.create_template_route(
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
        payload={
            "name": "Annual insurance",
            "transaction_type": "supplier_invoice",
            "schedule_type": "annually",
            "start_date": "2099-01-15",
            "amount": 1200,
            "supplier_id": SUPPLIER_ID,
        },
    )

    assert calls == [(USER_ID, ORG_ID)]
    assert result["success"] is True
    assert result["template"]["status"] == "active"
    assert result["template"]["next_due_date"] == "2099-01-15"
    assert db.tables["recurring_transaction_templates"][0]["created_by"] == USER_ID


def test_create_template_route_returns_400_for_invalid_type(monkeypatch):
    monkeypatch.setattr(recurring_router, "ensure_org_write", lambda *_args: None)

    with pytest.raises(HTTPException) as exc:
        recurring_router.create_template_route(
            auth=(USER_ID, _DB({"recurring_transaction_templates": []})),
            organisation_id=ORG_ID,
            payload={
                "name": "Bad recurring transaction",
                "transaction_type": "unknown",
                "schedule_type": "monthly",
                "start_date": "2099-01-15",
                "amount": 100,
            },
        )

    assert exc.value.status_code == 400
    assert "Invalid transaction_type" in exc.value.detail


def test_list_drafts_route_filters_pending_drafts(monkeypatch):
    monkeypatch.setattr(recurring_router, "ensure_org_read", lambda *_args: None)
    db = _DB({
        "recurring_transaction_drafts": [
            _draft(id=DRAFT_ID, status="pending"),
            _draft(id="approved-draft", status="approved"),
        ]
    })

    result = recurring_router.list_drafts_route(
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
        status="pending",
    )

    assert result["success"] is True
    assert [row["id"] for row in result["drafts"]] == [DRAFT_ID]


def test_approve_supplier_invoice_draft_posts_invoice_and_marks_approved(monkeypatch):
    monkeypatch.setattr(recurring_router, "ensure_org_write", lambda *_args: None)
    db = _DB(_tables())

    result = recurring_router.approve_draft_route(
        draft_id=DRAFT_ID,
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
    )

    assert result["success"] is True
    assert result["status"] == "approved"
    assert result["posted_record_id"]
    assert len(db.tables["invoices_extracted"]) == 1
    assert db.tables["invoices_extracted"][0]["source"] == "recurring"
    assert db.tables["recurring_transaction_drafts"][0]["status"] == "approved"
    assert db.tables["recurring_transaction_drafts"][0]["reviewed_by"] == USER_ID


def test_skip_draft_route_marks_pending_draft_skipped(monkeypatch):
    monkeypatch.setattr(recurring_router, "ensure_org_write", lambda *_args: None)
    db = _DB(_tables())

    result = recurring_router.skip_draft_route(
        draft_id=DRAFT_ID,
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
    )

    assert result == {"success": True, "id": DRAFT_ID, "status": "skipped"}
    assert db.tables["recurring_transaction_drafts"][0]["status"] == "skipped"
    assert db.tables["recurring_transaction_drafts"][0]["reviewed_by"] == USER_ID


def test_generate_due_drafts_creates_pending_draft_and_advances_template():
    db = _DB({
        "recurring_transaction_templates": [_template(next_due_date="2000-01-01", schedule_day=1)],
        "recurring_transaction_drafts": [],
    })

    generated = recurring_service.generate_due_drafts(db)

    assert generated == 1
    assert len(db.tables["recurring_transaction_drafts"]) == 1
    draft = db.tables["recurring_transaction_drafts"][0]
    assert draft["status"] == "pending"
    assert draft["draft_data"]["supplier_id"] == SUPPLIER_ID
    assert db.tables["recurring_transaction_templates"][0]["next_due_date"] == "2000-02-01"


def test_generate_due_drafts_completes_expired_template_without_draft():
    db = _DB({
        "recurring_transaction_templates": [_template(next_due_date="2000-01-01", end_date="2000-01-15")],
        "recurring_transaction_drafts": [],
    })

    generated = recurring_service.generate_due_drafts(db)

    assert generated == 0
    assert db.tables["recurring_transaction_drafts"] == []
    assert db.tables["recurring_transaction_templates"][0]["status"] == "completed"
