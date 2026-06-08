from app.services.income_statement import generate_income_statement


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self.in_filters = []
        self.gte_filters = []
        self.lte_filters = []
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def in_(self, field, values):
        self.in_filters.append((field, {str(value) for value in values}))
        return self

    def gte(self, field, value):
        self.gte_filters.append((field, value))
        return self

    def lte(self, field, value):
        self.lte_filters.append((field, value))
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
        for field, value in self.gte_filters:
            rows = [row for row in rows if row.get(field) >= value]
        for field, value in self.lte_filters:
            rows = [row for row in rows if row.get(field) <= value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _line(report, section, key):
    return next(row for row in report["sections"][section] if row["key"] == key)


def _base_tables(*, reporting_standard="ifrs", presentation="function"):
    return {
        "organisations": [
            {
                "id": "org-1",
                "reporting_standard": reporting_standard,
                "income_statement_presentation": presentation,
            }
        ],
        "gl_journals": [
            {"id": "journal-1", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-01-31"},
        ],
        "gl_journal_lines": [],
        "accounts": [],
        "tracking_dimensions": [],
        "tracking_values": [],
    }


def test_ifrs_nature_groups_expenses_by_account_nature():
    tables = _base_tables(presentation="nature")
    tables["accounts"] = [
        {"id": "rev", "organisation_id": "org-1", "name": "Sales", "type": "income", "income_statement_nature": "revenue", "special_report_classification": "none"},
        {"id": "mat", "organisation_id": "org-1", "name": "Materials", "type": "expense", "income_statement_nature": "raw_materials_consumables", "special_report_classification": "none"},
        {"id": "sal", "organisation_id": "org-1", "name": "Salaries", "type": "expense", "income_statement_nature": "employee_benefits", "special_report_classification": "none"},
        {"id": "dep", "organisation_id": "org-1", "name": "Depreciation", "type": "expense", "income_statement_nature": "depreciation_amortisation", "special_report_classification": "none"},
        {"id": "rent", "organisation_id": "org-1", "name": "Rent", "type": "expense", "income_statement_nature": "other_operating_expenses", "special_report_classification": "none"},
    ]
    tables["gl_journal_lines"] = [
        {"id": "l-rev", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "rev", "debit_amount": 0, "credit_amount": 1000, "tracking": {}},
        {"id": "l-mat", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "mat", "debit_amount": 300, "credit_amount": 0, "tracking": {}},
        {"id": "l-sal", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "sal", "debit_amount": 200, "credit_amount": 0, "tracking": {}},
        {"id": "l-dep", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "dep", "debit_amount": 50, "credit_amount": 0, "tracking": {}},
        {"id": "l-rent", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "rent", "debit_amount": 100, "credit_amount": 0, "tracking": {}},
    ]

    report = generate_income_statement(_DB(tables), organisation_id="org-1", date_from="2026-01-01", date_to="2026-01-31")

    assert report["presentation"] == "nature"
    assert _line(report, "operating_expenses", "raw_materials_consumables")["amount"] == 300.0
    assert _line(report, "operating_expenses", "employee_benefits")["amount"] == 200.0
    assert _line(report, "operating_expenses", "depreciation_amortisation")["amount"] == 50.0
    assert _line(report, "operating_expenses", "other_operating_expenses")["amount"] == 100.0
    assert report["subtotals"]["gross_profit"] is None
    assert report["subtotals"]["operating_profit"] == 350.0


def test_function_presentation_splits_shared_expense_by_tracking_value():
    tables = _base_tables(presentation="function")
    tables["tracking_dimensions"] = [
        {"id": "dim-fn", "organisation_id": "org-1", "name": "Cost Centre", "active": True, "is_income_statement_function_driver": True},
    ]
    tables["tracking_values"] = [
        {"id": "factory", "dimension_id": "dim-fn", "active": True, "income_statement_function": "cogs"},
        {"id": "sales", "dimension_id": "dim-fn", "active": True, "income_statement_function": "selling"},
        {"id": "head-office", "dimension_id": "dim-fn", "active": True, "income_statement_function": "g_and_a"},
    ]
    tables["accounts"] = [
        {"id": "rev", "organisation_id": "org-1", "name": "Sales", "type": "income", "income_statement_nature": "revenue", "special_report_classification": "none"},
        {"id": "rent", "organisation_id": "org-1", "name": "Rent", "type": "expense", "income_statement_nature": "other_operating_expenses", "special_report_classification": "none"},
    ]
    tables["gl_journal_lines"] = [
        {"id": "l-rev", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "rev", "debit_amount": 0, "credit_amount": 200, "tracking": {}},
        {"id": "l-cogs", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "rent", "debit_amount": 40, "credit_amount": 0, "tracking": {"dim-fn": "factory"}},
        {"id": "l-selling", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "rent", "debit_amount": 20, "credit_amount": 0, "tracking": {"dim-fn": "sales"}},
        {"id": "l-ga", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "rent", "debit_amount": 40, "credit_amount": 0, "tracking": {"dim-fn": "head-office"}},
    ]

    report = generate_income_statement(_DB(tables), organisation_id="org-1", date_from="2026-01-01", date_to="2026-01-31")

    assert _line(report, "operating_expenses", "cogs")["amount"] == 40.0
    assert _line(report, "operating_expenses", "selling")["amount"] == 20.0
    assert _line(report, "operating_expenses", "g_and_a")["amount"] == 40.0
    assert report["subtotals"]["gross_profit"] == 160.0
    assert report["subtotals"]["operating_profit"] == 100.0


def test_us_gaap_forces_function_presentation():
    tables = _base_tables(reporting_standard="us_gaap", presentation="nature")

    report = generate_income_statement(_DB(tables), organisation_id="org-1", date_from="2026-01-01", date_to="2026-01-31")

    assert report["reporting_standard"] == "us_gaap"
    assert report["presentation"] == "function"
    assert report["warnings"][0]["code"] == "us_gaap_forced_function"


def test_function_presentation_falls_back_to_account_default_then_other_operating():
    tables = _base_tables(presentation="function")
    tables["accounts"] = [
        {"id": "rd", "organisation_id": "org-1", "name": "Research payroll", "type": "expense", "income_statement_nature": "employee_benefits", "default_income_statement_function": "r_and_d", "special_report_classification": "none"},
        {"id": "uncoded", "organisation_id": "org-1", "name": "Uncoded expense", "type": "expense", "income_statement_nature": "other_operating_expenses", "special_report_classification": "none"},
    ]
    tables["gl_journal_lines"] = [
        {"id": "l-rd", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "rd", "debit_amount": 75, "credit_amount": 0, "tracking": {}},
        {"id": "l-uncoded", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "uncoded", "debit_amount": 25, "credit_amount": 0, "tracking": {}},
    ]

    report = generate_income_statement(_DB(tables), organisation_id="org-1", date_from="2026-01-01", date_to="2026-01-31")

    assert _line(report, "operating_expenses", "r_and_d")["amount"] == 75.0
    assert _line(report, "operating_expenses", "other_operating")["amount"] == 25.0
    assert any(warning["code"] == "missing_income_statement_function" for warning in report["warnings"])


def test_special_items_bypass_operating_grouping_and_ifrs_excludes_extraordinary():
    tables = _base_tables(reporting_standard="ifrs", presentation="function")
    tables["accounts"] = [
        {"id": "rev", "organisation_id": "org-1", "name": "Sales", "type": "income", "income_statement_nature": "revenue", "special_report_classification": "none"},
        {"id": "opex", "organisation_id": "org-1", "name": "Office", "type": "expense", "income_statement_nature": "other_operating_expenses", "default_income_statement_function": "g_and_a", "special_report_classification": "none"},
        {"id": "interest", "organisation_id": "org-1", "name": "Interest", "type": "expense", "special_report_classification": "finance_cost"},
        {"id": "associate", "organisation_id": "org-1", "name": "Associate profit", "type": "income", "special_report_classification": "associate_profit"},
        {"id": "disc", "organisation_id": "org-1", "name": "Discontinued loss", "type": "expense", "special_report_classification": "discontinued_operations"},
        {"id": "extra", "organisation_id": "org-1", "name": "Extraordinary", "type": "expense", "special_report_classification": "extraordinary"},
    ]
    tables["gl_journal_lines"] = [
        {"id": "l-rev", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "rev", "debit_amount": 0, "credit_amount": 1000, "tracking": {}},
        {"id": "l-opex", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "opex", "debit_amount": 100, "credit_amount": 0, "tracking": {}},
        {"id": "l-interest", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "interest", "debit_amount": 20, "credit_amount": 0, "tracking": {}},
        {"id": "l-associate", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "associate", "debit_amount": 0, "credit_amount": 30, "tracking": {}},
        {"id": "l-disc", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "disc", "debit_amount": 10, "credit_amount": 0, "tracking": {}},
        {"id": "l-extra", "organisation_id": "org-1", "gl_journal_id": "journal-1", "account_id": "extra", "debit_amount": 5, "credit_amount": 0, "tracking": {}},
    ]

    report = generate_income_statement(_DB(tables), organisation_id="org-1", date_from="2026-01-01", date_to="2026-01-31")

    assert report["subtotals"]["operating_profit"] == 900.0
    assert _line(report, "below_operating", "finance_costs")["amount"] == 20.0
    assert _line(report, "associate_profit", "associate_profit")["amount"] == 30.0
    assert _line(report, "discontinued_operations", "discontinued_operations")["amount"] == -10.0
    assert report["sections"]["extraordinary_items"] == []
    assert report["subtotals"]["net_income"] == 900.0
    assert any(warning["code"] == "extraordinary_items_prohibited" for warning in report["warnings"])
