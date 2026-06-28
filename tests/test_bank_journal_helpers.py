from app.services.bank.journals import build_journal_rows_for_line, journal_preview_lines
from tests.conftest import MemoryDB


ORG_ID = "00000000-0000-0000-0000-000000000001"


def test_journal_preview_lines_adds_account_labels():
    db = MemoryDB({
        "accounts": [
            {"id": "expense-1", "organisation_id": ORG_ID, "code": "5000", "name": "Repairs"},
        ],
    })

    preview = journal_preview_lines(
        db,
        ORG_ID,
        [{"account_id": "expense-1", "debit_amount": 100, "credit_amount": 0}],
    )

    assert preview[0]["account_code"] == "5000"
    assert preview[0]["account_name"] == "Repairs"


def test_build_journal_rows_for_line_splits_vat_on_allocation_side():
    db = MemoryDB({
        "bank_accounts": [
            {
                "id": "bank-1",
                "organisation_id": ORG_ID,
                "gl_account_id": "bank-gl",
            },
        ],
        "organisation_module_settings": [],
    })

    rows = build_journal_rows_for_line(
        db,
        organisation_id=ORG_ID,
        line={
            "bank_account_id": "bank-1",
            "signed_amount": -115,
            "description": "Office supplies",
        },
        gl_account_id="expense-1",
        tracking={"project": "admin"},
        vat_rate=15,
        vat_account_id="vat-control",
    )

    assert [row["account_id"] for row in rows] == ["expense-1", "bank-gl", "vat-control"]
    assert rows[0]["debit_amount"] == 100.0
    assert rows[1]["credit_amount"] == 115.0
    assert rows[2]["debit_amount"] == 15.0
    assert rows[0]["tracking"] == {"project": "admin"}
    assert rows[2]["tracking"] == {}
