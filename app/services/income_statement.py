from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional


REPORTING_STANDARDS = {"ifrs", "us_gaap", "uk_gaap_frs_102", "aspe"}
PRESENTATIONS = {"function", "nature"}
NATURE_KEYS = {
    "revenue",
    "changes_in_inventories",
    "raw_materials_consumables",
    "employee_benefits",
    "depreciation_amortisation",
    "other_operating_expenses",
    "other_operating_income",
}
FUNCTION_KEYS = {"cogs", "selling", "g_and_a", "r_and_d", "other_operating"}
SPECIAL_KEYS = {
    "none",
    "finance_cost",
    "associate_profit",
    "discontinued_operations",
    "extraordinary",
}

NATURE_LABELS = {
    "revenue": "Revenue",
    "changes_in_inventories": "Changes in inventories of finished goods and work in progress",
    "raw_materials_consumables": "Raw materials and consumables used",
    "employee_benefits": "Employee benefits expense",
    "depreciation_amortisation": "Depreciation and amortisation expense",
    "other_operating_expenses": "Other operating expenses",
    "other_operating_income": "Other operating income",
}
FUNCTION_LABELS = {
    "cogs": "Cost of goods sold",
    "selling": "Selling expenses",
    "g_and_a": "General and administrative expenses",
    "r_and_d": "Research and development expenses",
    "other_operating": "Other operating expenses",
}


def money(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def amount_out(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _fetch_rows(query) -> list[dict]:
    res = query.execute()
    return list(res.data or [])


def _fetch_organisation_settings(db, organisation_id: str) -> dict:
    rows = _fetch_rows(
        db.table("organisations")
        .select("reporting_standard, income_statement_presentation")
        .eq("id", organisation_id)
        .limit(1)
    )
    return rows[0] if rows else {}


def resolve_income_statement_options(
    db,
    *,
    organisation_id: str,
    reporting_standard: Optional[str] = None,
    presentation: Optional[str] = None,
) -> tuple[str, str, list[dict]]:
    warnings: list[dict] = []
    settings = _fetch_organisation_settings(db, organisation_id)
    standard = (reporting_standard or settings.get("reporting_standard") or "ifrs").lower()
    resolved_presentation = (
        presentation
        or settings.get("income_statement_presentation")
        or "function"
    ).lower()

    if standard not in REPORTING_STANDARDS:
        raise ValueError("Invalid reporting standard")
    if resolved_presentation not in PRESENTATIONS:
        raise ValueError("Invalid Income Statement presentation")

    if standard == "us_gaap" and resolved_presentation != "function":
        resolved_presentation = "function"
        warnings.append({
            "code": "us_gaap_forced_function",
            "message": "US GAAP Income Statements are presented by function.",
        })

    return standard, resolved_presentation, warnings


def _fetch_posted_journals(db, organisation_id: str, date_from: str, date_to: str) -> list[dict]:
    return _fetch_rows(
        db.table("gl_journals")
        .select("id, journal_date")
        .eq("organisation_id", organisation_id)
        .eq("status", "posted")
        .gte("journal_date", date_from)
        .lte("journal_date", date_to)
    )


def _fetch_journal_lines(db, organisation_id: str, journal_ids: list[str]) -> list[dict]:
    if not journal_ids:
        return []
    return _fetch_rows(
        db.table("gl_journal_lines")
        .select("id, gl_journal_id, account_id, debit_amount, credit_amount, tracking")
        .eq("organisation_id", organisation_id)
        .in_("gl_journal_id", journal_ids)
    )


def _fetch_accounts(db, organisation_id: str, account_ids: list[str]) -> dict[str, dict]:
    if not account_ids:
        return {}
    rows = _fetch_rows(
        db.table("accounts")
        .select(
            "id, code, name, type, income_statement_nature, "
            "default_income_statement_function, special_report_classification"
        )
        .eq("organisation_id", organisation_id)
        .in_("id", account_ids)
    )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _fetch_function_driver(db, organisation_id: str) -> tuple[Optional[dict], dict[str, str]]:
    dimensions = _fetch_rows(
        db.table("tracking_dimensions")
        .select("id, name")
        .eq("organisation_id", organisation_id)
        .eq("active", True)
        .eq("is_income_statement_function_driver", True)
        .limit(1)
    )
    if not dimensions:
        return None, {}

    dimension = dimensions[0]
    values = _fetch_rows(
        db.table("tracking_values")
        .select("id, income_statement_function")
        .eq("dimension_id", dimension.get("id"))
        .eq("active", True)
    )
    value_map = {
        str(row.get("id")): row.get("income_statement_function")
        for row in values
        if row.get("id") and row.get("income_statement_function") in FUNCTION_KEYS
    }
    return dimension, value_map


def _section_lines(groups: dict[str, Decimal], labels: dict[str, str], order: list[str]) -> list[dict]:
    lines: list[dict] = []
    for key in order:
        amount = groups.get(key, Decimal("0.00"))
        if amount:
            lines.append({"key": key, "label": labels.get(key, key), "amount": amount_out(amount)})
    return lines


def generate_income_statement(
    db,
    *,
    organisation_id: str,
    date_from: str,
    date_to: str,
    reporting_standard: Optional[str] = None,
    presentation: Optional[str] = None,
) -> dict:
    standard, resolved_presentation, warnings = resolve_income_statement_options(
        db,
        organisation_id=organisation_id,
        reporting_standard=reporting_standard,
        presentation=presentation,
    )

    function_driver, function_value_map = _fetch_function_driver(db, organisation_id)
    journals = _fetch_posted_journals(db, organisation_id, date_from, date_to)
    journal_ids = [str(row.get("id")) for row in journals if row.get("id")]
    journal_lines = _fetch_journal_lines(db, organisation_id, journal_ids)
    account_ids = sorted({str(row.get("account_id")) for row in journal_lines if row.get("account_id")})
    accounts = _fetch_accounts(db, organisation_id, account_ids)

    revenue = Decimal("0.00")
    nature_expenses = {key: Decimal("0.00") for key in NATURE_KEYS}
    function_expenses = {key: Decimal("0.00") for key in FUNCTION_KEYS}
    finance_costs = Decimal("0.00")
    associate_profit = Decimal("0.00")
    discontinued_operations = Decimal("0.00")
    extraordinary_items = Decimal("0.00")
    operating_profit_effect = Decimal("0.00")

    driver_dimension_id = str(function_driver.get("id")) if function_driver else None

    for line in journal_lines:
        account = accounts.get(str(line.get("account_id")))
        if not account:
            warnings.append({
                "code": "missing_account",
                "message": "A journal line references an account that could not be loaded.",
                "account_id": line.get("account_id"),
            })
            continue

        account_type = (account.get("type") or "").lower()
        special = account.get("special_report_classification") or "none"
        if special not in SPECIAL_KEYS:
            special = "none"

        debit = money(line.get("debit_amount"))
        credit = money(line.get("credit_amount"))
        profit_effect = credit - debit

        if special == "finance_cost":
            finance_costs += -profit_effect
            continue
        if special == "associate_profit":
            associate_profit += profit_effect
            continue
        if special == "discontinued_operations":
            discontinued_operations += profit_effect
            continue
        if special == "extraordinary":
            if standard in {"ifrs", "us_gaap"}:
                warnings.append({
                    "code": "extraordinary_items_prohibited",
                    "message": "Extraordinary items are prohibited under IFRS and US GAAP and were excluded.",
                    "account_id": account.get("id"),
                    "account_name": account.get("name"),
                })
                continue
            extraordinary_items += profit_effect
            continue

        if account_type == "income":
            revenue += profit_effect
            operating_profit_effect += profit_effect
            continue

        if account_type != "expense":
            continue

        expense_amount = -profit_effect
        operating_profit_effect += profit_effect

        nature = account.get("income_statement_nature") or "other_operating_expenses"
        if nature not in NATURE_KEYS or nature == "revenue":
            nature = "other_operating_expenses"
        if not account.get("income_statement_nature"):
            warnings.append({
                "code": "missing_income_statement_nature",
                "message": "Expense account has no Income Statement nature classification.",
                "account_id": account.get("id"),
                "account_name": account.get("name"),
            })
        nature_expenses[nature] += expense_amount

        function = None
        if driver_dimension_id:
            tracking = line.get("tracking") if isinstance(line.get("tracking"), dict) else {}
            tracking_value_id = tracking.get(driver_dimension_id)
            if tracking_value_id:
                function = function_value_map.get(str(tracking_value_id))
        if not function:
            function = account.get("default_income_statement_function")
        if function not in FUNCTION_KEYS:
            function = "other_operating"
            warnings.append({
                "code": "missing_income_statement_function",
                "message": "Expense line has no mapped Function classification; classified as Other operating.",
                "account_id": account.get("id"),
                "account_name": account.get("name"),
                "journal_line_id": line.get("id"),
            })
        function_expenses[function] += expense_amount

    if resolved_presentation == "function":
        operating_expense_lines = _section_lines(
            function_expenses,
            FUNCTION_LABELS,
            ["cogs", "selling", "g_and_a", "r_and_d", "other_operating"],
        )
        cogs = function_expenses.get("cogs", Decimal("0.00"))
        gross_profit = revenue - cogs
    else:
        operating_expense_lines = _section_lines(
            nature_expenses,
            NATURE_LABELS,
            [
                "changes_in_inventories",
                "raw_materials_consumables",
                "employee_benefits",
                "depreciation_amortisation",
                "other_operating_expenses",
            ],
        )
        gross_profit = None

    profit_before_discontinued = (
        operating_profit_effect
        - finance_costs
        + associate_profit
        + extraordinary_items
    )
    net_income = profit_before_discontinued + discontinued_operations

    return {
        "organisation_id": organisation_id,
        "date_from": date_from,
        "date_to": date_to,
        "reporting_standard": standard,
        "presentation": resolved_presentation,
        "function_driver_dimension": function_driver,
        "sections": {
            "revenue": [{"key": "revenue", "label": "Revenue", "amount": amount_out(revenue)}] if revenue else [],
            "operating_expenses": operating_expense_lines,
            "below_operating": [
                {"key": "finance_costs", "label": "Finance costs", "amount": amount_out(finance_costs)}
            ] if finance_costs else [],
            "associate_profit": [
                {"key": "associate_profit", "label": "Share of profit from associates", "amount": amount_out(associate_profit)}
            ] if associate_profit else [],
            "extraordinary_items": [
                {"key": "extraordinary_items", "label": "Extraordinary items", "amount": amount_out(extraordinary_items)}
            ] if extraordinary_items else [],
            "discontinued_operations": [
                {"key": "discontinued_operations", "label": "Discontinued operations", "amount": amount_out(discontinued_operations)}
            ] if discontinued_operations else [],
        },
        "subtotals": {
            "revenue": amount_out(revenue),
            "gross_profit": amount_out(gross_profit) if gross_profit is not None else None,
            "operating_profit": amount_out(operating_profit_effect),
            "profit_before_discontinued_operations": amount_out(profit_before_discontinued),
            "net_income": amount_out(net_income),
        },
        "warnings": warnings,
    }
