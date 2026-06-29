from uuid import UUID

from app.models.schemas import RunReconciliationRequest
from app.services.reconciliation_engine import run_reconciliation


ORG_ID = "00000000-0000-0000-0000-000000000001"
SUPPLIER_ID = "00000000-0000-0000-0000-000000000002"
STATEMENT_RAW_ID = "00000000-0000-0000-0000-000000000003"
STATEMENT_LINE_ID = "00000000-0000-0000-0000-000000000004"
PAYMENT_ID = "00000000-0000-0000-0000-000000000005"
INVOICE_ID = "00000000-0000-0000-0000-000000000006"


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.in_filters = []
        self.insert_payload = None
        self.update_payload = None

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

    def in_(self, field, values):
        self.in_filters.append((field, {str(value) for value in values}))
        return self

    def execute(self):
        if self.insert_payload is not None:
            table = self.db.tables.setdefault(self.table_name, [])
            payloads = self.insert_payload if isinstance(self.insert_payload, list) else [self.insert_payload]
            inserted = []
            for payload in payloads:
                row = dict(payload)
                table.append(row)
                inserted.append(row)
            return _Response(inserted)

        rows = list(self.db.tables.get(self.table_name, []))
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self.in_filters:
            rows = [row for row in rows if str(row.get(field)) in values]
        if self.update_payload is not None:
            for row in rows:
                row.update(self.update_payload)
            return _Response(rows)
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self, name)


def _request(**options):
    payload = {
        "organisation_id": ORG_ID,
        "supplier_id": SUPPLIER_ID,
        "statement_raw_id": STATEMENT_RAW_ID,
    }
    if options:
        payload["options"] = options
    return RunReconciliationRequest(**payload)


def test_run_reconciliation_matches_supplier_payment_run_by_reference():
    db = _DB({
        "statement_lines": [
            {
                "id": STATEMENT_LINE_ID,
                "organisation_id": ORG_ID,
                "statement_raw_id": STATEMENT_RAW_ID,
                "line_date": "2026-06-29",
                "reference": "EFT PAYRUN-ABC-ACME",
                "description": "Supplier batch payment",
                "debit_amount": 650,
                "credit_amount": 0,
            }
        ],
        "invoices_extracted": [
            {
                "id": INVOICE_ID,
                "organisation_id": ORG_ID,
                "supplier_id": SUPPLIER_ID,
                "invoice_number": "ACME-100",
                "invoice_date": "2026-06-20",
                "total_amount": 650,
                "supplier_name": "Acme Supplies",
            }
        ],
        "payments": [
            {
                "id": PAYMENT_ID,
                "organisation_id": ORG_ID,
                "supplier_id": SUPPLIER_ID,
                "payment_date": "2026-06-29",
                "payment_reference": "PAYRUN-ABC-ACME",
                "amount": 650,
                "payment_source": "supplier_payment_run",
            }
        ],
        "reconciliations": [],
        "reconciliation_lines": [
            {
                "id": "existing-link",
                "organisation_id": ORG_ID,
                "payment_id": PAYMENT_ID,
                "invoice_extracted_id": INVOICE_ID,
                "match_status": "matched",
            }
        ],
    })

    result = run_reconciliation(db, _request(amount_tolerance=0.01))

    assert result.summary.matched == 1
    assert result.summary.missing_invoice_count == 0
    assert result.lines[0].matched_payment_id == UUID(PAYMENT_ID)
    assert result.lines[0].matched_payment_reference == "PAYRUN-ABC-ACME"
    assert db.tables["statement_lines"][0]["match_status"] == "matched"
    inserted_statement_rows = [
        row for row in db.tables["reconciliation_lines"]
        if row.get("statement_line_id") == STATEMENT_LINE_ID
    ]
    assert inserted_statement_rows[0]["payment_id"] == PAYMENT_ID
    assert inserted_statement_rows[0]["invoice_extracted_id"] is None


def test_run_reconciliation_matches_unique_supplier_payment_run_by_amount_and_date():
    second_payment_id = "00000000-0000-0000-0000-000000000007"
    generic_payment_id = "00000000-0000-0000-0000-000000000008"
    db = _DB({
        "statement_lines": [
            {
                "id": STATEMENT_LINE_ID,
                "organisation_id": ORG_ID,
                "statement_raw_id": STATEMENT_RAW_ID,
                "line_date": "2026-06-30",
                "reference": "ACME EFT",
                "description": "Batch settlement",
                "debit_amount": 500,
                "credit_amount": 0,
            }
        ],
        "invoices_extracted": [],
        "payments": [
            {
                "id": PAYMENT_ID,
                "organisation_id": ORG_ID,
                "supplier_id": SUPPLIER_ID,
                "payment_date": "2026-06-28",
                "payment_reference": "PAYRUN-NEAR",
                "amount": 500,
                "payment_source": "supplier_payment_run",
            },
            {
                "id": second_payment_id,
                "organisation_id": ORG_ID,
                "supplier_id": SUPPLIER_ID,
                "payment_date": "2026-05-01",
                "payment_reference": "PAYRUN-OLD",
                "amount": 500,
                "payment_source": "supplier_payment_run",
            },
            {
                "id": generic_payment_id,
                "organisation_id": ORG_ID,
                "supplier_id": SUPPLIER_ID,
                "payment_date": "2026-06-30",
                "payment_reference": "MANUAL-PAYMENT",
                "amount": 500,
                "payment_source": "manual",
            },
        ],
        "reconciliations": [],
        "reconciliation_lines": [],
    })

    result = run_reconciliation(db, _request(amount_tolerance=0.01, date_tolerance_days=5))

    assert result.summary.matched == 1
    assert result.lines[0].matched_payment_id == UUID(PAYMENT_ID)
    assert "amount/date" in (result.lines[0].notes or "")
    inserted_statement_rows = [
        row for row in db.tables["reconciliation_lines"]
        if row.get("statement_line_id") == STATEMENT_LINE_ID
    ]
    assert inserted_statement_rows[0]["payment_id"] == PAYMENT_ID
