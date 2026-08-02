from __future__ import annotations

from pathlib import Path

import app.services.invoice_extraction_benchmark_suite as suite_service


class _Result:
    def __init__(self, data=None):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.filters = []
        self.patch = None

    def select(self, *_args, **_kwargs):
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        rows = self.db.tables.get(self.table, [])
        matches = [row for row in rows if all(row.get(field) == value for field, value in self.filters)]
        if self.patch is not None:
            for row in matches:
                row.update(self.patch)
        return _Result(matches)


class _Db:
    def __init__(self):
        self.tables = {
            "invoice_extraction_benchmark_suites": [{
                "id": "suite-1",
                "status": "running",
                "requested_by": "owner-1",
            }],
            "invoice_extraction_gold_documents": [{
                "id": "gold-1",
                "invoice_raw_id": "raw-1",
                "invoice_extracted_id": "invoice-1",
                "organisation_id": "org-1",
                "gold_json": {},
            }],
            "invoice_extraction_benchmark_suite_items": [{
                "id": "item-1",
                "suite_id": "suite-1",
                "gold_document_id": "gold-1",
                "status": "running",
                "claimed_by": "worker-1",
            }],
        }

    def table(self, name):
        return _Query(self, name)


def _claimed_item(*, attempts=1, max_attempts=2):
    return {
        "id": "item-1",
        "suite_id": "suite-1",
        "gold_document_id": "gold-1",
        "attempt_count": attempts,
        "max_attempts": max_attempts,
    }


def test_worker_completes_suite_item_and_records_quality_result(monkeypatch):
    db = _Db()
    monkeypatch.setattr(suite_service, "claim_next_benchmark_item", lambda *_args, **_kwargs: _claimed_item())
    monkeypatch.setattr(suite_service, "run_benchmark_case", lambda *_args, **_kwargs: {
        "result": {
            "accuracy": 0.98,
            "correction_count": 1,
            "critical_error_count": 0,
            "within_two_corrections": True,
        },
        "run": {"id": "run-1"},
    })

    result = suite_service.process_next_benchmark_item(worker_id="worker-1", db=db)

    assert result["status"] == "completed"
    assert result["quality_passed"] is True
    item = db.tables["invoice_extraction_benchmark_suite_items"][0]
    assert item["status"] == "completed"
    assert item["run_id"] == "run-1"
    assert item["quality_passed"] is True
    assert item["claimed_by"] is None


def test_worker_requeues_transient_failure_before_attempt_limit(monkeypatch):
    db = _Db()
    monkeypatch.setattr(suite_service, "claim_next_benchmark_item", lambda *_args, **_kwargs: _claimed_item(attempts=1))
    monkeypatch.setattr(suite_service, "run_benchmark_case", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable")))

    result = suite_service.process_next_benchmark_item(worker_id="worker-1", db=db)

    assert result["status"] == "retrying"
    item = db.tables["invoice_extraction_benchmark_suite_items"][0]
    assert item["status"] == "queued"
    assert item["error"] == "model unavailable"


def test_worker_marks_repeated_failure_terminal(monkeypatch):
    db = _Db()
    monkeypatch.setattr(suite_service, "claim_next_benchmark_item", lambda *_args, **_kwargs: _claimed_item(attempts=2))
    monkeypatch.setattr(suite_service, "run_benchmark_case", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad source")))

    result = suite_service.process_next_benchmark_item(worker_id="worker-1", db=db)

    assert result["status"] == "failed"
    item = db.tables["invoice_extraction_benchmark_suite_items"][0]
    assert item["status"] == "failed"
    assert item["completed_at"] is not None


def test_stale_worker_cannot_overwrite_item_reclaimed_by_another_worker(monkeypatch):
    db = _Db()
    db.tables["invoice_extraction_benchmark_suite_items"][0]["claimed_by"] = "worker-2"
    monkeypatch.setattr(suite_service, "claim_next_benchmark_item", lambda *_args, **_kwargs: _claimed_item())
    monkeypatch.setattr(suite_service, "run_benchmark_case", lambda *_args, **_kwargs: {
        "result": {
            "accuracy": 1.0,
            "correction_count": 0,
            "critical_error_count": 0,
            "within_two_corrections": True,
        },
        "run": {"id": "run-stale"},
    })

    result = suite_service.process_next_benchmark_item(worker_id="worker-1", db=db)

    assert result["status"] == "stale_claim"
    item = db.tables["invoice_extraction_benchmark_suite_items"][0]
    assert item["status"] == "running"
    assert item["claimed_by"] == "worker-2"
    assert item.get("run_id") is None


def test_empty_suite_queue_returns_empty(monkeypatch):
    monkeypatch.setattr(suite_service, "claim_next_benchmark_item", lambda *_args, **_kwargs: None)

    assert suite_service.process_next_benchmark_item(worker_id="worker-1", db=_Db()) == {
        "success": True,
        "status": "empty",
    }


def test_suite_migration_uses_service_role_atomic_claims_and_recovers_expired_work():
    migration = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "db"
        / "applied"
        / "20260801120000_invoice_extraction_benchmark_suites.sql"
    ).read_text(encoding="utf-8")

    assert "claim_next_invoice_extraction_benchmark_item" in migration
    assert "for update of item skip locked" in migration.lower()
    assert "Only the service role may claim invoice benchmark items" in migration
    assert "lease expired and maximum attempts were reached" in migration
