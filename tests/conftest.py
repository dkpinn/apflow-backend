"""Shared fixtures and stub DB helpers for the test suite.

Classes (import directly or use via pytest fixtures):
  StubResult   — minimal .data / .count response object
  StubQuery    — read-only query chain with eq/neq/in_/gte/lte/order/limit/rpc
  StubDB       — tables-dict-backed read-only client
  MemoryQuery  — mutable query chain that also supports insert/update/delete
  MemoryDB     — mutable in-memory client (+ MemoryAuth for token→user_id)

Fixtures:
  stub_db(tables, *, rpc_result)  → StubDB
  memory_db(tables, *, token_users) → MemoryDB
  fake_auth                       → ("test-user-1", StubDB({}))
"""
from __future__ import annotations

from typing import Any
import pytest


# ── Result object ────────────────────────────────────────────────────────────

class StubResult:
    def __init__(self, data=None, count=None):
        self.data = data or []
        self.count = count


# ── Read-only stub (select queries only) ─────────────────────────────────────

class StubQuery:
    """Chainable read-only stub that applies filters on execute().

    Supports: select, eq, neq, in_, gte, lte, order, limit.
    ISO-format date strings compare correctly with >=/<= because they sort
    lexicographically in chronological order.
    """

    def __init__(self, rows: list):
        self._rows = list(rows)
        self._filters: list[tuple] = []
        self._neq_filters: list[tuple] = []
        self._in_filters: list[tuple] = []
        self._gte: list[tuple] = []
        self._lte: list[tuple] = []
        self._limit: int | None = None

    def select(self, *_a, **_kw):
        return self

    def eq(self, key: str, value: Any):
        self._filters.append(("eq", key, value))
        return self

    def neq(self, key: str, value: Any):
        self._filters.append(("neq", key, value))
        return self

    def in_(self, key: str, values):
        self._in_filters.append((key, {str(v) for v in values}))
        return self

    def gte(self, key: str, value: Any):
        self._gte.append((key, value))
        return self

    def lte(self, key: str, value: Any):
        self._lte.append((key, value))
        return self

    def order(self, *_a, **_kw):
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def execute(self) -> StubResult:
        rows = self._rows
        for op, key, value in self._filters:
            if op == "eq":
                rows = [r for r in rows if r.get(key) == value]
            else:
                rows = [r for r in rows if r.get(key) != value]
        for key, values in self._in_filters:
            rows = [r for r in rows if str(r.get(key)) in values]
        for key, value in self._gte:
            rows = [r for r in rows if r.get(key) is not None and r.get(key) >= value]
        for key, value in self._lte:
            rows = [r for r in rows if r.get(key) is not None and r.get(key) <= value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return StubResult(rows)


class _RpcQuery:
    def __init__(self, data):
        self._data = data if isinstance(data, list) else ([data] if data is not None else [])

    def execute(self) -> StubResult:
        return StubResult(self._data)


class StubDB:
    """Read-only Supabase stub backed by an in-memory tables dict.

    Usage::

        db = StubDB({
            "organisation_users": [
                {"user_id": "u1", "organisation_id": "o1", "role": "owner", "status": "active"},
            ]
        })
    """

    def __init__(self, tables: dict[str, list] | None = None, *, rpc_result=None):
        self.tables: dict[str, list] = tables or {}
        self._rpc_result = rpc_result
        self.rpc_calls: list[tuple] = []

    def table(self, name: str) -> StubQuery:
        return StubQuery(self.tables.get(name, []))

    def rpc(self, name: str, params: dict) -> _RpcQuery:
        self.rpc_calls.append((name, params))
        return _RpcQuery(self._rpc_result)


# ── Mutable in-memory DB (select + insert + update + delete) ─────────────────

class MemoryAuth:
    """Stub for supabase.auth that maps bearer tokens to user IDs."""

    def __init__(self, token_users: dict[str, str] | None = None):
        self._token_users = token_users or {}

    def get_user(self, token: str):
        user_id = self._token_users.get(token)
        if user_id is None:
            raise ValueError(f"Unknown token: {token!r}")
        user = type("User", (), {"id": user_id})()
        return type("UserResponse", (), {"user": user})()


class MemoryQuery:
    """Mutable query builder that executes against a shared MemoryDB.tables dict."""

    def __init__(self, client: "MemoryDB", table_name: str):
        self._client = client
        self._table = table_name
        self._op = "select"
        self._payload: Any = None
        self._filters: list[tuple] = []
        self._neq_filters: list[tuple] = []
        self._in_filters: list[tuple] = []
        self._limit: int | None = None
        self._count_mode: str | None = None

    def select(self, *_a, **_kw):
        self._op = "select"
        self._count_mode = _kw.get("count")
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, key: str, value: Any):
        self._filters.append((key, value))
        return self

    def neq(self, key: str, value: Any):
        self._neq_filters.append((key, value))
        return self

    def in_(self, key: str, values):
        self._in_filters.append((key, set(values)))
        return self

    def order(self, *_a, **_kw):
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def _matches(self, row: dict) -> bool:
        return all(row.get(k) == v for k, v in self._filters) and all(
            row.get(k) != v for k, v in self._neq_filters
        ) and all(
            row.get(k) in vs for k, vs in self._in_filters
        )

    def execute(self) -> StubResult:
        rows = self._client.tables.setdefault(self._table, [])
        if self._op == "select":
            matched = [r.copy() for r in rows if self._matches(r)]
            total = len(matched)
            result = matched[: self._limit] if self._limit is not None else matched
            return StubResult(result, count=total if self._count_mode else None)
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            inserted: list[dict] = []
            for item in items:
                row = dict(item)
                row.setdefault("id", f"{self._table}-{len(rows) + len(inserted) + 1}")
                inserted.append(row)
            rows.extend(inserted)
            return StubResult([r.copy() for r in inserted])
        if self._op == "update":
            updated: list[dict] = []
            for row in rows:
                if self._matches(row):
                    row.update(self._payload)
                    updated.append(row.copy())
            return StubResult(updated)
        if self._op == "delete":
            kept = [r for r in rows if not self._matches(r)]
            deleted = len(rows) - len(kept)
            rows[:] = kept
            return StubResult([], count=deleted)
        return StubResult([])


class MemoryDB:
    """Mutable in-memory Supabase client supporting CRUD operations.

    Usage::

        db = MemoryDB({"orgs": [{"id": "org-1", "name": "Acme"}]})
        db.table("orgs").insert({"id": "org-2", "name": "Beta"}).execute()
        assert len(db.tables["orgs"]) == 2
    """

    def __init__(
        self,
        tables: dict[str, list] | None = None,
        *,
        token_users: dict[str, str] | None = None,
    ):
        self.tables: dict[str, list] = {k: list(v) for k, v in (tables or {}).items()}
        self.auth = MemoryAuth(token_users)

    def table(self, name: str) -> MemoryQuery:
        return MemoryQuery(self, name)


# ── pytest fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def stub_db():
    """Factory: ``stub_db({"table": [rows]})`` → :class:`StubDB`."""
    def _make(tables: dict | None = None, *, rpc_result=None) -> StubDB:
        return StubDB(tables, rpc_result=rpc_result)
    return _make


@pytest.fixture
def memory_db():
    """Factory: ``memory_db({"table": [rows]})`` → :class:`MemoryDB`."""
    def _make(tables: dict | None = None, *, token_users: dict | None = None) -> MemoryDB:
        return MemoryDB(tables, token_users=token_users)
    return _make


@pytest.fixture
def fake_auth(stub_db):
    """Minimal ``(user_id, db)`` tuple for authenticated route testing."""
    return ("test-user-1", stub_db())
