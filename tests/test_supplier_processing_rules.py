import pytest

from app.services.invoice_line_items import build_line_item_payload, validate_line_item_allocations
from app.services.invoice_parse_attempts import ensure_parsed_data_attempt
from app.services.invoice_supplier_rules import (
    apply_supplier_processing_rules,
    reapply_supplier_rules_to_invoice,
)
from app.services.supplier_matcher import attempt_supplier_auto_link


class _SupplierQuery:
    def __init__(self, suppliers):
        self.suppliers = suppliers

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def execute(self):
        return type("Result", (), {"data": self.suppliers})()


class _SupplierClient:
    def __init__(self, suppliers):
        self.suppliers = suppliers

    def table(self, name):
        assert name == "suppliers"
        return _SupplierQuery(self.suppliers)


class _Result:
    def __init__(self, data):
        self.data = data


class _MemoryQuery:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.filters = []
        self.operation = "select"
        self.payload = None

    def select(self, *_args):
        self.operation = "select"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.filters.append((key, set(values)))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def _matches(self, row):
        return all(
            row.get(key) in value if isinstance(value, set) else row.get(key) == value
            for key, value in self.filters
        )

    def execute(self):
        rows = self.client.tables.setdefault(self.table_name, [])
        if self.operation == "select":
            return _Result([row.copy() for row in rows if self._matches(row)])
        if self.operation == "update":
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self.payload)
                    updated.append(row.copy())
            return _Result(updated)
        if self.operation == "delete":
            deleted = [row for row in rows if self._matches(row)]
            self.client.tables[self.table_name] = [row for row in rows if not self._matches(row)]
            return _Result([row.copy() for row in deleted])
        if self.operation == "insert":
            payload = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = []
            for item in payload:
                row = dict(item)
                row.setdefault("id", f"{self.table_name}-{len(rows) + len(inserted) + 1}")
                inserted.append(row)
            rows.extend(inserted)
            return _Result([row.copy() for row in inserted])
        return _Result([])


class _MemorySupabase:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _MemoryQuery(self, name)


def test_supplier_rule_defaults_preserve_extracted_line_items():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "total_amount": 230.0,
        "line_items": [
            {"description": "Item A", "quantity": 2, "unit_price": 50.0, "line_total": 100.0},
            {"description": "Item B", "quantity": 1, "unit_price": 130.0, "line_total": 130.0},
        ],
    }

    result = apply_supplier_processing_rules(parsed, {})

    assert result["line_items"] == parsed["line_items"]
    assert result["invoice_patch"] == {}


def test_supplier_auto_link_requires_configured_match_count():
    supabase = _SupplierClient([
        {
            "id": "supplier-1",
            "supplier_name": "Example Supplier",
            "trading_name": "Example Trading",
            "vat_number": "411 111 1111",
            "company_registration_number": "REG-1",
            "account_number": "ACC-1",
            "bank_account_number": "123-456",
            "phone": "031 555 0101",
            "default_email": "accounts@example.test",
        },
    ])

    assert attempt_supplier_auto_link(
        supabase,
        org_id="org-1",
        supplier_name_extracted="Example Supplier",
    ) is None

    assert attempt_supplier_auto_link(
        supabase,
        org_id="org-1",
        supplier_name_extracted="Different Name",
        vat_number_extracted="4111111111",
    ) is None

    assert attempt_supplier_auto_link(
        supabase,
        org_id="org-1",
        supplier_name_extracted="Example Supplier",
        vat_number_extracted="4111111111",
    ) == "supplier-1"


def test_supplier_rule_parse_line_items_false_creates_summary_line():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "subtotal": 200.0,
        "total_amount": 230.0,
        "line_items": [
            {"description": "Item A", "quantity": 2, "unit_price": 50.0, "line_total": 100.0},
        ],
    }

    result = apply_supplier_processing_rules(parsed, {"parse_line_items": False})

    assert result["line_items"] == [{
        "description": "Purchase from Example Supplier",
        "quantity": 1,
        "unit_price": 200.0,
        "line_total": 200.0,
    }]


def test_supplier_rule_strips_vat_inclusive_unit_prices():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "subtotal": 200.0,
        "tax_amount": 30.0,
        "total_amount": 230.0,
        "line_items": [
            {"description": "Item A", "quantity": 2, "unit_price": 115.0, "line_total": 230.0},
        ],
    }

    result = apply_supplier_processing_rules(parsed, {"line_items_include_vat": True})

    assert result["line_items"][0]["unit_price"] == 100.0
    assert result["line_items"][0]["line_total"] == 200.0


def test_supplier_rule_applies_default_expense_account_to_invoice_and_lines():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "total_amount": 100.0,
        "line_items": [
            {"description": "Item A", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        ],
    }

    result = apply_supplier_processing_rules(
        parsed,
        {"default_expense_account": "6000/Office"},
    )

    assert result["invoice_patch"] == {"expense_account": "6000/Office"}
    assert result["line_items"][0]["expense_account"] == "6000/Office"


def test_supplier_allocation_rule_overrides_default_account_and_balances_split():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "document_type": "tax_invoice",
        "total_amount": 100.0,
        "line_items": [
            {"description": "Gate repair top gate", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        ],
    }

    result = apply_supplier_processing_rules(
        parsed,
        {
            "default_expense_account": "6000/Default",
            "allocation_rules": [{
                "id": "rule-1",
                "name": "Gate repairs",
                "active": True,
                "priority": 10,
                "document_scope": "invoice",
                "match_type": "contains",
                "match_field": "description",
                "pattern": "gate repair",
                "splits": [
                    {"expense_account": "7100/Repairs", "tracking": {"dim-1": "top-gate"}, "percent": 60},
                    {"expense_account": "7200/Maintenance", "tracking": {"dim-1": "shared"}, "percent": 40},
                ],
            }],
        },
    )

    item = result["line_items"][0]
    assert item["expense_account"] == "7100/Repairs"
    assert item["tracking"] == {"dim-1": "top-gate"}
    assert [split["amount"] for split in item["allocations"]] == [60.0, 40.0]
    assert sum(split["amount"] for split in item["allocations"]) == 100.0


def test_supplier_allocation_rule_respects_document_scope_and_priority():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "document_type": "credit_note",
        "total_amount": -50.0,
        "line_items": [
            {"description": "Gate repair reversal", "line_total": -50.0},
        ],
    }

    result = apply_supplier_processing_rules(
        parsed,
        {
            "allocation_rules": [
                {
                    "id": "invoice-rule",
                    "name": "Invoice repairs",
                    "active": True,
                    "priority": 1,
                    "document_scope": "invoice",
                    "match_type": "contains",
                    "pattern": "gate",
                    "splits": [{"expense_account": "6000/Wrong", "percent": 100}],
                },
                {
                    "id": "credit-rule",
                    "name": "Credit repairs",
                    "active": True,
                    "priority": 20,
                    "document_scope": "credit_note",
                    "match_type": "contains",
                    "pattern": "gate",
                    "splits": [{"expense_account": "7000/Credit", "percent": 100}],
                },
            ],
        },
    )

    assert result["line_items"][0]["expense_account"] == "7000/Credit"
    assert result["line_items"][0]["allocations"][0]["amount"] == 50.0


def test_line_item_payload_persists_expense_account():
    payload = build_line_item_payload(
        invoice_extracted_id="invoice-1",
        organisation_id="org-1",
        line_items=[{
            "description": "Item A",
            "quantity": 1,
            "unit_price": 100.0,
            "line_total": 100.0,
            "expense_account": "6000/Office",
        }],
    )

    assert payload[0]["expense_account"] == "6000/Office"


def test_line_item_allocation_validation_accepts_balanced_split():
    validate_line_item_allocations([
        {
            "description": "Shared expense",
            "line_total": 150,
            "allocations": [
                {"expense_account": "6000", "tracking": {"cost_centre": "head-office"}, "amount": 100},
                {"expense_account": "6000", "tracking": {"cost_centre": "finance"}, "amount": 50},
            ],
        },
    ])


def test_line_item_allocation_validation_accepts_credit_note_magnitude_split():
    validate_line_item_allocations([
        {
            "description": "Credit reversal",
            "line_total": -150,
            "allocations": [
                {"expense_account": "6000", "tracking": {"cost_centre": "head-office"}, "amount": 100},
                {"expense_account": "6000", "tracking": {"cost_centre": "finance"}, "amount": 50},
            ],
        },
    ])


def test_line_item_allocation_validation_rejects_unbalanced_split():
    with pytest.raises(ValueError, match="allocations total"):
        validate_line_item_allocations([
            {
                "description": "Shared expense",
                "line_total": 150,
                "allocations": [
                    {"expense_account": "6000", "amount": 100},
                    {"expense_account": "6000", "amount": 45},
                ],
            },
        ])


def test_parsed_data_attempt_captures_vlm_line_items_when_text_is_empty():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "invoice_number": "INV-1",
        "total_amount": 300.0,
        "line_items": [
            {"description": "Item A", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
            {"description": "Item B", "quantity": 2, "unit_price": 100.0, "line_total": 200.0},
        ],
    }

    attempts = ensure_parsed_data_attempt([], parsed_data=parsed, text="")

    assert len(attempts) == 1
    assert attempts[0]["strategy"] == "final_extraction_snapshot"
    assert attempts[0]["line_items"] == parsed["line_items"]
    assert attempts[0]["parsed_data"]["line_items"] == parsed["line_items"]


def _rule_test_db(*, supplier_settings=None, attempts=None, current_line_items=None):
    supplier = {
        "id": "supplier-1",
        "parse_line_items": True,
        "line_items_include_vat": False,
        "default_vat_rate": None,
        "default_expense_account": None,
        **(supplier_settings or {}),
    }
    return _MemorySupabase({
        "suppliers": [supplier],
        "invoice_parse_attempts": attempts or [],
        "invoice_line_items": current_line_items or [],
        "invoices_extracted": [{
            "id": "invoice-1",
            "organisation_id": "org-1",
            "invoice_raw_id": "raw-1",
            "supplier_id": "supplier-1",
        }],
        "invoice_audit_events": [],
    })


def _invoice():
    return {
        "id": "invoice-1",
        "organisation_id": "org-1",
        "invoice_raw_id": "raw-1",
        "supplier_id": "supplier-1",
        "supplier_name_extracted": "Example Supplier",
        "subtotal": 300.0,
        "total_amount": 300.0,
    }


def _raw_attempt(line_items):
    return {
        "id": "attempt-1",
        "invoice_raw_id": "raw-1",
        "attempt_number": 1,
        "strategy": "final_extraction_snapshot",
        "selected": True,
        "parsed_data": {
            "supplier_name_extracted": "Example Supplier",
            "subtotal": 300.0,
            "total_amount": 300.0,
            "line_items": line_items,
        },
        "line_items": line_items,
    }


def test_reapply_rules_uses_raw_parse_attempt_instead_of_current_summary_row():
    raw_lines = [
        {"description": "Item A", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        {"description": "Item B", "quantity": 2, "unit_price": 100.0, "line_total": 200.0},
    ]
    db = _rule_test_db(
        attempts=[_raw_attempt(raw_lines)],
        current_line_items=[{
            "invoice_extracted_id": "invoice-1",
            "description": "Purchase from Example Supplier",
            "quantity": 1,
            "line_total": 300.0,
        }],
    )

    result = reapply_supplier_rules_to_invoice(db, invoice=_invoice(), supplier_id="supplier-1")

    assert result["source"] == "selected_parse_attempt"
    assert result["needs_reextract"] is False
    assert [row["description"] for row in db.tables["invoice_line_items"]] == ["Item A", "Item B"]


def test_reapply_rules_can_collapse_raw_lines_to_supplier_summary():
    raw_lines = [
        {"description": "Item A", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        {"description": "Item B", "quantity": 2, "unit_price": 100.0, "line_total": 200.0},
    ]
    db = _rule_test_db(
        supplier_settings={"parse_line_items": False},
        attempts=[_raw_attempt(raw_lines)],
    )

    result = reapply_supplier_rules_to_invoice(db, invoice=_invoice(), supplier_id="supplier-1")

    assert result["source"] == "selected_parse_attempt"
    assert result["line_items_count"] == 1
    assert db.tables["invoice_line_items"][0]["description"] == "Purchase from Example Supplier"
    assert db.tables["invoice_line_items"][0]["line_total"] == 300.0


def test_reapply_rules_restores_raw_lines_after_summary_mode_is_turned_back_on():
    raw_lines = [
        {"description": "Item A", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        {"description": "Item B", "quantity": 2, "unit_price": 100.0, "line_total": 200.0},
    ]
    db = _rule_test_db(
        supplier_settings={"parse_line_items": False},
        attempts=[_raw_attempt(raw_lines)],
    )

    reapply_supplier_rules_to_invoice(db, invoice=_invoice(), supplier_id="supplier-1")
    db.tables["suppliers"][0]["parse_line_items"] = True
    result = reapply_supplier_rules_to_invoice(db, invoice=_invoice(), supplier_id="supplier-1")

    assert result["source"] == "selected_parse_attempt"
    assert [row["description"] for row in db.tables["invoice_line_items"]] == ["Item A", "Item B"]


def test_reapply_rules_without_raw_snapshot_does_not_overwrite_generated_summary():
    summary = {
        "invoice_extracted_id": "invoice-1",
        "description": "Purchase from Example Supplier",
        "quantity": 1,
        "line_total": 300.0,
    }
    db = _rule_test_db(current_line_items=[summary.copy()])

    result = reapply_supplier_rules_to_invoice(db, invoice=_invoice(), supplier_id="supplier-1")

    assert result["needs_reextract"] is True
    assert result["skipped"] is True
    assert db.tables["invoice_line_items"] == [summary]
