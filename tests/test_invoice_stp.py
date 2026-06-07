from unittest.mock import patch

from app.services.invoice_stp import attempt_invoice_stp


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def neq(self, key, value):
        self.filters.append((key, value, True))
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        rows = self.rows
        for item in self.filters:
            key, value = item[:2]
            if len(item) == 3:
                rows = [row for row in rows if row.get(key) != value]
            else:
                rows = [row for row in rows if row.get(key) == value]
        return _Result(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _ready():
    return {"ready": True, "review_status": "in_review"}


def _db(*, maximum=100, supplier_org="org-1"):
    return _DB({
        "invoices_extracted": [{
            "id": "invoice-1",
            "organisation_id": "org-1",
            "supplier_id": "supplier-1",
            "invoice_number": "INV-1",
            "posting_status": "unposted",
        }],
        "suppliers": [{
            "id": "supplier-1",
            "organisation_id": supplier_org,
            "active": True,
            "stp_enabled": True,
            "stp_max_amount": maximum,
        }],
    })


def test_stp_rejects_duplicate_supplier_reference():
    db = _db()
    db.tables["invoices_extracted"].append({
        **db.tables["invoices_extracted"][0],
        "id": "invoice-2",
    })
    result = attempt_invoice_stp(
        db,
        invoice_id="invoice-1",
        org_id="org-1",
        readiness_result=_ready(),
    )
    assert result == {"status": "not_eligible", "reason": "duplicate_invoice"}


def test_stp_maximum_is_inclusive_for_any_trusted_supplier_link():
    prepared = {
        "invoice_id": "invoice-1",
        "organisation_id": "org-1",
        "gross_total": 100,
    }
    with (
        patch("app.services.invoice_stp.prepare_invoice_gl_posting", return_value=prepared),
        patch(
            "app.services.invoice_stp.persist_prepared_invoice_posting",
            return_value={"success": True, "journal_id": "journal-1"},
        ),
    ):
        result = attempt_invoice_stp(
            _db(maximum=100),
            invoice_id="invoice-1",
            org_id="org-1",
            readiness_result=_ready(),
        )
    assert result["status"] == "posted"


def test_stp_rejects_cross_organisation_supplier_and_unready_invoice():
    result = attempt_invoice_stp(
        _db(supplier_org="org-2"),
        invoice_id="invoice-1",
        org_id="org-1",
        readiness_result=_ready(),
    )
    assert result == {"status": "not_eligible", "reason": "supplier_not_trusted"}

    result = attempt_invoice_stp(
        _db(),
        invoice_id="invoice-1",
        org_id="org-1",
        readiness_result={"ready": False, "review_status": "needs_info"},
    )
    assert result == {"status": "not_eligible", "reason": "invoice_not_ready"}


def test_stp_returns_failed_when_atomic_posting_fails():
    with patch(
        "app.services.invoice_stp.prepare_invoice_gl_posting",
        side_effect=ValueError("missing tracking"),
    ):
        result = attempt_invoice_stp(
            _db(),
            invoice_id="invoice-1",
            org_id="org-1",
            readiness_result=_ready(),
        )
    assert result["status"] == "failed"
    assert result["reason"] == "missing tracking"
