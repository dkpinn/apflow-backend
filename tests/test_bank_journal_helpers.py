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
        "organisations": [
            {
                "id": ORG_ID,
                "vat_registered": True,
                "vat_registration_date": "2026-01-01",
            }
        ],
        "organisation_module_settings": [],
    })

    rows = build_journal_rows_for_line(
        db,
        organisation_id=ORG_ID,
        line={
            "bank_account_id": "bank-1",
            "line_date": "2026-06-30",
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


def _simple_db():
    return MemoryDB({
        "bank_accounts": [
            {"id": "bank-1", "organisation_id": ORG_ID, "gl_account_id": "bank-gl"},
        ],
        "organisations": [{"id": ORG_ID, "vat_registered": False}],
        "organisation_module_settings": [],
    })


def _rows_with(db, *, description="Office supplies", allocation_narration=None, override=None):
    line = {
        "bank_account_id": "bank-1",
        "line_date": "2026-06-30",
        "signed_amount": -100,
        "description": description,
    }
    if allocation_narration is not None:
        line["allocation_narration"] = allocation_narration
    return build_journal_rows_for_line(
        db,
        organisation_id=ORG_ID,
        line=line,
        gl_account_id="expense-1",
        tracking={},
        description_override=override,
    )


def test_journal_row_description_prefers_stored_narration_over_bank_description():
    rows = _rows_with(_simple_db(), allocation_narration="School fees term 1")
    assert rows[0]["description"] == "School fees term 1"


def test_journal_row_description_override_wins_over_narration():
    rows = _rows_with(_simple_db(), allocation_narration="Narration", override="Explicit override")
    assert rows[0]["description"] == "Explicit override"


def test_journal_row_description_falls_back_to_bank_description():
    rows = _rows_with(_simple_db(), description="Coffee Shop")
    assert rows[0]["description"] == "Coffee Shop"


def test_build_journal_rows_for_line_suppresses_vat_before_registration_date():
    db = MemoryDB({
        "bank_accounts": [
            {
                "id": "bank-1",
                "organisation_id": ORG_ID,
                "gl_account_id": "bank-gl",
            },
        ],
        "organisations": [
            {
                "id": ORG_ID,
                "vat_registered": True,
                "vat_registration_date": "2026-07-01",
            }
        ],
        "organisation_module_settings": [],
    })

    rows = build_journal_rows_for_line(
        db,
        organisation_id=ORG_ID,
        line={
            "bank_account_id": "bank-1",
            "line_date": "2026-06-30",
            "signed_amount": -115,
            "description": "Office supplies",
        },
        gl_account_id="expense-1",
        tracking={},
        vat_rate=15,
        vat_account_id="vat-control",
    )

    assert [row["account_id"] for row in rows] == ["expense-1", "bank-gl"]
    assert rows[0]["debit_amount"] == 115.0
