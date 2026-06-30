import pytest
from fastapi import HTTPException

from app.routers import receipt_inbox


ORG_ID = "00000000-0000-0000-0000-000000000001"
USER_ID = "00000000-0000-0000-0000-000000000002"
RECEIPT_ID = "00000000-0000-0000-0000-000000000003"
OTHER_RECEIPT_ID = "00000000-0000-0000-0000-000000000004"
BANK_LINE_ID = "00000000-0000-0000-0000-000000000005"
OTHER_BANK_LINE_ID = "00000000-0000-0000-0000-000000000006"


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _NotFilter:
    def __init__(self, query):
        self.query = query

    def is_(self, field, value):
        self.query.not_null_filters.append(field)
        return self.query


class _Query:
    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.in_filters = []
        self.not_null_filters = []
        self.order_fields = []
        self.range_from = None
        self.range_to = None
        self.update_payload = None
        self.single = False
        self.not_ = _NotFilter(self)

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def in_(self, field, values):
        self.in_filters.append((field, {str(value) for value in values}))
        return self

    def order(self, field, **_kwargs):
        self.order_fields.append((field, bool(_kwargs.get("desc"))))
        return self

    def range(self, start, end):
        self.range_from = start
        self.range_to = end
        return self

    def maybe_single(self):
        self.single = True
        return self

    def update(self, payload):
        self.update_payload = payload
        return self

    def _matching_rows(self):
        rows = list(self.db.tables.get(self.table_name, []))
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self.in_filters:
            rows = [row for row in rows if str(row.get(field)) in values]
        for field in self.not_null_filters:
            rows = [row for row in rows if row.get(field) is not None]
        for field, desc in reversed(self.order_fields):
            rows = sorted(rows, key=lambda row: (row.get(field) is None, row.get(field)), reverse=desc)
        if self.range_from is not None and self.range_to is not None:
            rows = rows[self.range_from : self.range_to + 1]
        return rows

    def execute(self):
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


def _tables():
    return {
        "invoices_extracted": [
            {
                "id": RECEIPT_ID,
                "organisation_id": ORG_ID,
                "invoice_raw_id": "raw-1",
                "supplier_name": "Coffee Shop",
                "invoice_number": "R-001",
                "invoice_date": "2026-06-01",
                "total_amount": 45.5,
                "document_type": "receipt",
                "parse_status": "parsed",
                "created_at": "2026-06-02T10:00:00",
            },
            {
                "id": OTHER_RECEIPT_ID,
                "organisation_id": ORG_ID,
                "invoice_raw_id": "raw-2",
                "supplier_name": "Fuel Station",
                "invoice_number": "R-002",
                "invoice_date": "2026-06-03",
                "total_amount": 600,
                "document_type": "receipt",
                "parse_status": "parsed",
                "created_at": "2026-06-03T10:00:00",
            },
            {
                "id": "00000000-0000-0000-0000-000000000099",
                "organisation_id": ORG_ID,
                "document_type": "tax_invoice",
                "created_at": "2026-06-04T10:00:00",
            },
        ],
        "bank_statement_lines": [
            {
                "id": BANK_LINE_ID,
                "organisation_id": ORG_ID,
                "line_date": "2026-06-02",
                "description": "Coffee Shop",
                "signed_amount": -45.5,
                "receipt_document_id": RECEIPT_ID,
            },
            {
                "id": OTHER_BANK_LINE_ID,
                "organisation_id": ORG_ID,
                "line_date": "2026-06-03",
                "description": "Fuel Station",
                "signed_amount": -600,
                "receipt_document_id": None,
            },
        ],
    }


def test_list_receipts_returns_receipts_with_linked_bank_line(monkeypatch):
    calls = []
    monkeypatch.setattr(receipt_inbox, "ensure_org_read", lambda user_id, org_id: calls.append((user_id, org_id)))
    db = _DB(_tables())

    result = receipt_inbox.list_receipts(
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
        status="all",
        limit=100,
        offset=0,
    )

    assert calls == [(USER_ID, ORG_ID)]
    assert result["success"] is True
    assert result["total"] == 2
    assert [row["id"] for row in result["receipts"]] == [OTHER_RECEIPT_ID, RECEIPT_ID]
    linked = next(row for row in result["receipts"] if row["id"] == RECEIPT_ID)
    assert linked["linked_bank_line"]["id"] == BANK_LINE_ID
    unlinked = next(row for row in result["receipts"] if row["id"] == OTHER_RECEIPT_ID)
    assert unlinked["linked_bank_line"] is None


def test_list_receipts_filters_matched_and_unmatched(monkeypatch):
    monkeypatch.setattr(receipt_inbox, "ensure_org_read", lambda *_args: None)
    db = _DB(_tables())

    matched = receipt_inbox.list_receipts(
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
        status="matched",
        limit=100,
        offset=0,
    )
    unmatched = receipt_inbox.list_receipts(
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
        status="unmatched",
        limit=100,
        offset=0,
    )

    assert [row["id"] for row in matched["receipts"]] == [RECEIPT_ID]
    assert [row["id"] for row in unmatched["receipts"]] == [OTHER_RECEIPT_ID]


def test_link_bank_line_updates_bank_statement_line(monkeypatch):
    calls = []
    monkeypatch.setattr(receipt_inbox, "ensure_org_write", lambda user_id, org_id: calls.append((user_id, org_id)))
    db = _DB(_tables())

    result = receipt_inbox.link_bank_line(
        receipt_id=OTHER_RECEIPT_ID,
        payload=receipt_inbox.LinkBankLineRequest(bank_line_id=OTHER_BANK_LINE_ID),
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
    )

    assert calls == [(USER_ID, ORG_ID)]
    assert result == {"success": True, "receipt_id": OTHER_RECEIPT_ID, "bank_line_id": OTHER_BANK_LINE_ID}
    bank_line = next(row for row in db.tables["bank_statement_lines"] if row["id"] == OTHER_BANK_LINE_ID)
    assert bank_line["receipt_document_id"] == OTHER_RECEIPT_ID


def test_link_bank_line_404s_for_missing_receipt(monkeypatch):
    monkeypatch.setattr(receipt_inbox, "ensure_org_write", lambda *_args: None)

    with pytest.raises(HTTPException) as exc:
        receipt_inbox.link_bank_line(
            receipt_id="00000000-0000-0000-0000-000000000088",
            payload=receipt_inbox.LinkBankLineRequest(bank_line_id=OTHER_BANK_LINE_ID),
            auth=(USER_ID, _DB(_tables())),
            organisation_id=ORG_ID,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Receipt not found"


def test_unlink_bank_line_clears_receipt_links(monkeypatch):
    calls = []
    monkeypatch.setattr(receipt_inbox, "ensure_org_write", lambda user_id, org_id: calls.append((user_id, org_id)))
    db = _DB(_tables())

    result = receipt_inbox.unlink_bank_line(
        receipt_id=RECEIPT_ID,
        auth=(USER_ID, db),
        organisation_id=ORG_ID,
    )

    assert calls == [(USER_ID, ORG_ID)]
    assert result == {"success": True, "receipt_id": RECEIPT_ID}
    bank_line = next(row for row in db.tables["bank_statement_lines"] if row["id"] == BANK_LINE_ID)
    assert bank_line["receipt_document_id"] is None
