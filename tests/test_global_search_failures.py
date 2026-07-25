from __future__ import annotations

from app.services.global_search import SEARCH_SOURCES, search


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows, error=None):
        self.rows = rows
        self.error = error

    def select(self, *_args, **_kwargs): return self
    def eq(self, *_args, **_kwargs): return self
    def or_(self, *_args, **_kwargs): return self
    def limit(self, *_args, **_kwargs): return self
    def order(self, *_args, **_kwargs): return self

    def execute(self):
        if self.error:
            raise self.error
        return _Response(self.rows)


class _DB:
    SOURCE_TABLES = {
        "suppliers": "suppliers",
        "customers": "customers",
        "invoices_extracted": "supplier_invoices",
        "sales_invoices": "sales_invoices",
        "accounts": "accounts",
        "bank_statement_lines": "bank_lines",
    }

    def __init__(self, failures=(), tables=None):
        self.failures = set(failures)
        self.tables = tables or {}

    def table(self, name):
        source = self.SOURCE_TABLES[name]
        error = RuntimeError(f"{source} unavailable") if source in self.failures else None
        return _Query(self.tables.get(name, []), error)


def test_search_returns_results_and_structured_partial_errors():
    result = search(
        _DB(
            failures={"customers", "bank_lines"},
            tables={
                "suppliers": [{
                    "id": "supplier-1",
                    "supplier_name": "Acme Supplies",
                    "trading_name": None,
                    "supplier_code": "ACME",
                }],
            },
        ),
        "org-1",
        "acme",
    )

    assert result["results"][0]["title"] == "Acme Supplies"
    assert result["is_partial"] is True
    assert result["failed_sources"] == ["customers", "bank_lines"]
    assert {error["code"] for error in result["errors"]} == {"source_unavailable"}
    assert "suppliers" in result["searched_sources"]


def test_search_does_not_present_total_source_failure_as_complete_empty_result():
    result = search(_DB(failures=set(SEARCH_SOURCES)), "org-1", "invoice")

    assert result["results"] == []
    assert result["is_partial"] is True
    assert result["searched_sources"] == []
    assert result["failed_sources"] == list(SEARCH_SOURCES)
    assert len(result["errors"]) == len(SEARCH_SOURCES)


def test_short_search_is_explicitly_complete_without_querying_sources():
    result = search(_DB(failures=set(SEARCH_SOURCES)), "org-1", "a")
    assert result == {
        "results": [],
        "query": "a",
        "is_partial": False,
        "errors": [],
        "searched_sources": [],
        "failed_sources": [],
    }
