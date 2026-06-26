from datetime import date

import pytest
from fastapi import HTTPException

from app.routers import accounting_periods
from app.services.accounting_periods import list_accounting_periods, save_accounting_period


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, db, table, rows):
        self.db = db
        self.table = table
        self.rows = list(rows or [])
        self.filters = []
        self._limit = None
        self.upsert_row = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, value):
        self._limit = value
        return self

    def upsert(self, row, **_kwargs):
        self.upsert_row = dict(row)
        return self

    def execute(self):
        if self.upsert_row is not None:
            table = self.db.tables.setdefault(self.table, [])
            existing = next(
                (
                    row
                    for row in table
                    if row.get("organisation_id") == self.upsert_row.get("organisation_id")
                    and row.get("period_start") == self.upsert_row.get("period_start")
                ),
                None,
            )
            if existing:
                existing.update(self.upsert_row)
                saved = existing
            else:
                saved = {"id": f"{self.table}-1", **self.upsert_row}
                table.append(saved)
            return _Response([saved])

        rows = self.rows
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self, name, self.tables.get(name, []))


def test_list_accounting_periods_merges_saved_and_generated_periods():
    db = _DB(
        {
            "organisation_accounting_periods": [
                {
                    "id": "period-1",
                    "organisation_id": "org-1",
                    "period_start": "2026-06-01",
                    "period_end": "2026-06-30",
                    "status": "locked",
                    "lock_date": "2026-06-30",
                    "checklist": {"bank_reconciled": True, "reports_reviewed": True},
                    "notes": "Closed",
                }
            ]
        }
    )

    result = list_accounting_periods(
        db,
        organisation_id="org-1",
        months_back=1,
        months_forward=0,
        today=date(2026, 6, 25),
    )

    assert result["effective_lock_date"] == "2026-06-30"
    assert result["summary"]["locked"] == 1
    assert result["summary"]["generated"] == 1
    saved = next(row for row in result["periods"] if row["period_start"] == "2026-06-01")
    assert saved["status"] == "locked"
    assert saved["checklist_completed"] == 2


def test_save_accounting_period_validates_and_upserts():
    db = _DB({"organisation_accounting_periods": []})

    saved = save_accounting_period(
        db,
        organisation_id="org-1",
        user_id="user-1",
        period_start="2026-06-01",
        period_end="2026-06-30",
        status="closed",
        lock_date="2026-06-30",
        checklist={"bank_reconciled": True},
        notes="Month end done",
    )

    assert saved["status"] == "closed"
    assert saved["lock_date"] == "2026-06-30"
    assert saved["checklist"]["bank_reconciled"] is True
    assert saved["checklist_total"] == 5

    with pytest.raises(ValueError, match="lock_date cannot be after period_end"):
        save_accounting_period(
            db,
            organisation_id="org-1",
            user_id="user-1",
            period_start="2026-06-01",
            period_end="2026-06-30",
            status="locked",
            lock_date="2026-07-01",
        )


def test_accounting_period_routes_use_read_and_admin_permissions(monkeypatch):
    calls = []
    db = _DB({"organisation_accounting_periods": []})

    monkeypatch.setattr(accounting_periods, "ensure_org_read", lambda user_id, org_id: calls.append(("read", user_id, org_id)))
    monkeypatch.setattr(accounting_periods, "ensure_org_admin", lambda user_id, org_id: calls.append(("admin", user_id, org_id)))

    accounting_periods.accounting_periods(auth=("user-1", db), organisation_id="org-1")
    accounting_periods.save_period(
        accounting_periods.SaveAccountingPeriodRequest(
            organisation_id="org-1",
            period_start="2026-06-01",
            period_end="2026-06-30",
            status="open",
        ),
        auth=("user-1", db),
    )

    assert calls == [("read", "user-1", "org-1"), ("admin", "user-1", "org-1")]


def test_accounting_period_save_rejects_unauthorised(monkeypatch):
    def _deny(*_args):
        raise HTTPException(status_code=403, detail="Nope")

    monkeypatch.setattr(accounting_periods, "ensure_org_admin", _deny)

    with pytest.raises(HTTPException) as exc:
        accounting_periods.save_period(
            accounting_periods.SaveAccountingPeriodRequest(
                organisation_id="org-1",
                period_start="2026-06-01",
                period_end="2026-06-30",
            ),
            auth=("user-1", _DB({})),
        )

    assert exc.value.status_code == 403
