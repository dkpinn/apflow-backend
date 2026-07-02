from app.services.bank_account_summary import (
    build_bank_balance_summary,
    calculate_statement_balances,
    posted_gl_balance,
    posted_gl_opening_balance,
    select_latest_statement,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []
        self.ids = None

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.ids = (key, {str(value) for value in values})
        return self

    def execute(self):
        rows = [
            row
            for row in self.rows
            if all(row.get(key) == value for key, value in self.filters)
        ]
        if self.ids:
            key, values = self.ids
            rows = [row for row in rows if str(row.get(key)) in values]
        return _Result(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def test_latest_statement_uses_effective_end_then_transaction_and_upload_dates():
    uploads = [
        {
            "id": "upload-title-only",
            "extraction_status": "extracted",
            "statement_period_to": None,
            "uploaded_at": "2026-06-05T00:00:00Z",
            "closing_balance": "1125.00",
        },
        {
            "id": "upload-period",
            "extraction_status": "extracted",
            "statement_period_to": "2026-05-31",
            "uploaded_at": "2026-06-01T00:00:00Z",
            "closing_balance": "1000.00",
        },
        {
            "id": "upload-failed",
            "extraction_status": "failed",
            "statement_period_to": "2026-06-30",
            "uploaded_at": "2026-07-01T00:00:00Z",
            "closing_balance": "1500.00",
        },
    ]
    lines = [
        {
            "bank_statement_upload_id": "upload-title-only",
            "line_date": "2026-06-02",
        },
        {
            "bank_statement_upload_id": "upload-period",
            "line_date": "2026-05-30",
        },
    ]

    latest, latest_line_date = select_latest_statement(uploads, lines)

    assert latest["id"] == "upload-title-only"
    assert latest_line_date == "2026-06-02"


def test_latest_statement_ignores_non_extracted_and_missing_closing_balance():
    uploads = [
        {
            "id": "latest-processing",
            "extraction_status": "processing",
            "statement_period_to": "2026-06-30",
            "uploaded_at": "2026-07-01T00:00:00Z",
            "closing_balance": "9999.00",
        },
        {
            "id": "missing-closing",
            "extraction_status": "extracted",
            "statement_period_to": "2026-06-30",
            "uploaded_at": "2026-07-02T00:00:00Z",
            "closing_balance": None,
        },
        {
            "id": "valid",
            "extraction_status": "extracted",
            "statement_period_to": "2026-05-31",
            "uploaded_at": "2026-06-01T00:00:00Z",
            "closing_balance": "1100.00",
        },
    ]

    latest, latest_line_date = select_latest_statement(uploads, [])

    assert latest["id"] == "valid"
    assert latest_line_date is None


def test_calculated_imported_balance_includes_every_latest_statement_row():
    upload = {
        "id": "upload-1",
        "opening_balance": "1000.00",
        "closing_balance": "1125.00",
    }
    lines = [
        {"bank_statement_upload_id": "upload-1", "signed_amount": "150.00"},
        {"bank_statement_upload_id": "upload-1", "signed_amount": "-25.00"},
        {"bank_statement_upload_id": "upload-2", "signed_amount": "999.00"},
    ]

    bank_balance, imported_balance = calculate_statement_balances(upload, lines)

    assert bank_balance == 1125.0
    assert imported_balance == 1125.0


def test_balanced_upload_can_use_closing_balance_when_stored_rows_were_filtered():
    upload = {
        "id": "upload-1",
        "opening_balance": "1000.00",
        "closing_balance": "1125.00",
        "balance_status": "balanced",
    }

    bank_balance, imported_balance = calculate_statement_balances(upload, [])

    assert bank_balance == 1125.0
    assert imported_balance == 1125.0


def test_posted_gl_balance_excludes_drafts_and_reversed_originals():
    db = _DB(
        {
            "gl_journal_lines": [
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "posted",
                    "debit_amount": "100.00",
                    "credit_amount": "0",
                },
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "draft",
                    "debit_amount": "50.00",
                    "credit_amount": "0",
                },
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "reversed-original",
                    "debit_amount": "20.00",
                    "credit_amount": "0",
                },
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "reversal",
                    "debit_amount": "0",
                    "credit_amount": "20.00",
                },
            ],
            "gl_journals": [
                {"id": "posted", "organisation_id": "org-1", "status": "posted"},
                {"id": "draft", "organisation_id": "org-1", "status": "draft"},
                {"id": "reversed-original", "organisation_id": "org-1", "status": "reversed"},
                {"id": "reversal", "organisation_id": "org-1", "status": "posted"},
            ],
        }
    )

    assert posted_gl_balance(
        db,
        organisation_id="org-1",
        gl_account_id="bank-gl",
    ) == 80.0


def test_posted_gl_opening_balance_uses_only_posted_opening_journals():
    db = _DB(
        {
            "gl_journal_lines": [
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "opening",
                    "debit_amount": "100.00",
                    "credit_amount": "0",
                },
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "normal",
                    "debit_amount": "50.00",
                    "credit_amount": "0",
                },
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "draft-opening",
                    "debit_amount": "25.00",
                    "credit_amount": "0",
                },
            ],
            "gl_journals": [
                {"id": "opening", "organisation_id": "org-1", "status": "posted", "source_type": "opening_balance"},
                {"id": "normal", "organisation_id": "org-1", "status": "posted", "source_type": "bank_transaction"},
                {"id": "draft-opening", "organisation_id": "org-1", "status": "draft", "source_type": "opening_balance"},
            ],
        }
    )

    assert posted_gl_opening_balance(
        db,
        organisation_id="org-1",
        gl_account_id="bank-gl",
    ) == 100.0


def test_summary_marks_unlinked_tb_without_displaying_zero():
    summary = build_bank_balance_summary(
        _DB({}),
        organisation_id="org-1",
        account={"id": "bank-1", "gl_account_id": None, "opening_balance": "0"},
        lines=[],
        uploads=[],
    )

    assert summary["bank_statement_balance"] == 0.0
    assert summary["calculated_imported_balance"] == 0.0
    assert summary["current_tb_balance"] is None
    assert summary["bank_balance_status"] == "available"
    assert summary["imported_balance_status"] == "available"
    assert summary["tb_balance_status"] == "gl_account_not_linked"


def test_summary_uses_coa_opening_balance_when_statement_header_is_missing():
    summary = build_bank_balance_summary(
        _DB({
            "gl_journal_lines": [
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "opening",
                    "debit_amount": "1000.00",
                    "credit_amount": "0",
                }
            ],
            "gl_journals": [
                {"id": "opening", "organisation_id": "org-1", "status": "posted", "source_type": "opening_balance"}
            ],
        }),
        organisation_id="org-1",
        account={
            "id": "bank-1",
            "gl_account_id": "bank-gl",
            "opening_balance": "9999.00",
            "current_reconciled_balance": "777.00",
        },
        lines=[
            {"bank_statement_upload_id": "upload-1", "signed_amount": "150.00", "line_date": "2026-06-02"},
            {"bank_statement_upload_id": "upload-1", "signed_amount": "-25.00", "line_date": "2026-06-03"},
        ],
        uploads=[{"id": "upload-1", "extraction_status": "extracted"}],
    )

    assert summary["bank_statement_balance"] == 1000.0
    assert summary["calculated_imported_balance"] == 1125.0
    assert summary["current_tb_balance"] == 1000.0


def test_summary_uses_latest_valid_statement_closing_balance():
    summary = build_bank_balance_summary(
        _DB({
            "gl_journal_lines": [
                {
                    "organisation_id": "org-1",
                    "account_id": "bank-gl",
                    "gl_journal_id": "opening",
                    "debit_amount": "4489.79",
                    "credit_amount": "0",
                }
            ],
            "gl_journals": [
                {"id": "opening", "organisation_id": "org-1", "status": "posted", "source_type": "opening_balance"}
            ],
        }),
        organisation_id="org-1",
        account={
            "id": "bank-1",
            "gl_account_id": "bank-gl",
        },
        lines=[
            {"bank_statement_upload_id": "older", "signed_amount": "100.00", "line_date": "2026-05-01"},
            {"bank_statement_upload_id": "latest", "signed_amount": "250.00", "line_date": "2026-06-01"},
        ],
        uploads=[
            {
                "id": "older",
                "extraction_status": "extracted",
                "statement_period_to": "2026-05-31",
                "opening_balance": "4489.79",
                "closing_balance": "4589.79",
                "uploaded_at": "2026-06-01T00:00:00Z",
            },
            {
                "id": "latest",
                "extraction_status": "extracted",
                "statement_period_to": "2026-06-30",
                "opening_balance": "4589.79",
                "closing_balance": "4839.79",
                "uploaded_at": "2026-07-01T00:00:00Z",
            },
            {
                "id": "newer-failed",
                "extraction_status": "failed",
                "statement_period_to": "2026-07-31",
                "opening_balance": "4839.79",
                "closing_balance": "9999.00",
                "uploaded_at": "2026-08-01T00:00:00Z",
            },
        ],
    )

    assert summary["bank_statement_balance"] == 4839.79
    assert summary["calculated_imported_balance"] == 4839.79
    assert summary["current_tb_balance"] == 4489.79
    assert summary["latest_statement_upload_id"] == "latest"
