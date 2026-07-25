from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

from app.routers import reports
from app.services import cash_flow_forecast


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, *, error=None):
        self.error = error

    def select(self, *_args, **_kwargs): return self
    def eq(self, *_args, **_kwargs): return self
    def gt(self, *_args, **_kwargs): return self
    def gte(self, *_args, **_kwargs): return self
    def lte(self, *_args, **_kwargs): return self

    def execute(self):
        if self.error:
            raise self.error
        return _Response()


class _DB:
    def __init__(self, failing_table=None):
        self.failing_table = failing_table

    def table(self, name):
        error = RuntimeError(f"{name} unavailable") if name == self.failing_table else None
        return _Query(error=error)


def test_forecast_fails_when_opening_bank_balances_are_unavailable():
    with pytest.raises(cash_flow_forecast.CashFlowForecastDataError) as exc_info:
        cash_flow_forecast.generate_cash_flow_forecast(
            _DB("bank_accounts"), "org-1", "2026-07-01", 30
        )
    assert exc_info.value.source == "bank_balances"


def test_forecast_fails_when_supplier_payables_are_unavailable(monkeypatch):
    monkeypatch.setattr(
        cash_flow_forecast,
        "generate_aged_payables",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("payables unavailable")),
    )
    with pytest.raises(cash_flow_forecast.CashFlowForecastDataError) as exc_info:
        cash_flow_forecast.generate_cash_flow_forecast(_DB(), "org-1", "2026-07-01", 30)
    assert exc_info.value.source == "supplier_payables"


def test_forecast_fails_when_customer_receivables_are_unavailable(monkeypatch):
    monkeypatch.setattr(
        cash_flow_forecast,
        "generate_aged_payables",
        lambda *_args, **_kwargs: {"suppliers": [], "warnings": []},
    )
    with pytest.raises(cash_flow_forecast.CashFlowForecastDataError) as exc_info:
        cash_flow_forecast.generate_cash_flow_forecast(
            _DB("sales_invoices"), "org-1", "2026-07-01", 30
        )
    assert exc_info.value.source == "customer_receivables"


def test_forecast_marks_optional_recurring_source_failure_as_partial(monkeypatch):
    monkeypatch.setattr(
        cash_flow_forecast,
        "generate_aged_payables",
        lambda *_args, **_kwargs: {"suppliers": [], "warnings": []},
    )
    forecast = cash_flow_forecast.generate_cash_flow_forecast(
        _DB("recurring_transaction_templates"), "org-1", "2026-07-01", 30
    )
    assert forecast["is_partial"] is True
    assert forecast["warnings"] == [{
        "code": "recurring_transactions_unavailable",
        "message": "Recurring transactions could not be loaded and are excluded from this forecast.",
    }]


def test_forecast_route_returns_service_unavailable_for_core_source(monkeypatch):
    monkeypatch.setattr(reports, "_ensure_reports_view", lambda *_args: None)
    monkeypatch.setattr(
        reports,
        "generate_cash_flow_forecast",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cash_flow_forecast.CashFlowForecastDataError("supplier_payables")
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        reports.cash_flow_forecast_report(
            auth=("user-1", object()),
            organisation_id="org-1",
            as_at_date="2026-07-01",
            forecast_days=30,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["source"] == "supplier_payables"
