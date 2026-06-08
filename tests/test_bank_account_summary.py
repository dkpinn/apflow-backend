from app.services.bank_account_summary import (
    build_bank_balance_summary,
    calculate_statement_balances,
    posted_gl_balance,
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
        },
        {
            "id": "upload-period",
            "extraction_status": "extracted",
            "statement_period_to": "2026-05-31",
            "uploaded_at": "2026-06-01T00:00:00Z",
        },
        {
            "id": "upload-failed",
            "extraction_status": "failed",
            "statement_period_to": "2026-06-30",
            "uploaded_at": "2026-07-01T00:00:00Z",
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


def test_summary_marks_unlinked_tb_without_displaying_zero():
    summary = build_bank_balance_summary(
        _DB({}),
        organisation_id="org-1",
        account={"id": "bank-1", "gl_account_id": None},
        lines=[],
        uploads=[],
    )

    assert summary["bank_statement_balance"] is None
    assert summary["calculated_imported_balance"] is None
    assert summary["current_tb_balance"] is None
    assert summary["tb_balance_status"] == "gl_account_not_linked"
