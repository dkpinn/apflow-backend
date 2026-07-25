import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260725110000_database_coordinated_invoice_worker.sql"
_SPEC = importlib.util.spec_from_file_location(
    "document_jobs_under_test", ROOT / "app/services/document_jobs.py"
)
assert _SPEC and _SPEC.loader
_DOCUMENT_JOBS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_DOCUMENT_JOBS)
claim_next_queued_job = _DOCUMENT_JOBS.claim_next_queued_job


class _Result:
    def __init__(self, data):
        self.data = data


class _Rpc:
    def __init__(self, owner):
        self.owner = owner

    def execute(self):
        return _Result(self.owner.result)


class _Db:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def rpc(self, name, params=None):
        self.calls.append((name, params))
        return _Rpc(self)


def test_claim_uses_database_rpc_with_worker_identity():
    db = _Db([{"id": "job-1"}])

    claimed = claim_next_queued_job(
        db,
        worker_id="worker-a",
        organisation_id="org-1",
        lease_seconds=600,
    )

    assert claimed == {"id": "job-1"}
    assert db.calls == [(
        "claim_next_document_processing_job",
        {
            "p_worker_id": "worker-a",
            "p_organisation_id": "org-1",
            "p_lease_seconds": 600,
        },
    )]


def test_migration_coordinates_claims_and_maintenance_in_postgres():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "for update skip locked" in sql
    assert "pg_try_advisory_xact_lock" in sql
    assert "lease_expires_at" in sql
    assert "attempt_count >= max_retries" in sql
    assert "grant execute on function public.claim_next_document_processing_job" in sql
    assert "to service_role" in sql


def test_api_does_not_start_or_drain_invoice_workers():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    router_sources = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "app/routers/invoices.py",
            "app/routers/invoices_queue.py",
            "app/routers/webhooks.py",
        )
    )

    assert "_background_sweep_thread" not in main
    assert "threading.Thread" not in main
    assert "add_task(run_extract_worker_until_empty)" not in router_sources


def test_dedicated_worker_has_an_executable_entrypoint():
    worker = (ROOT / "app/workers/invoice_worker.py").read_text(encoding="utf-8")

    assert "maintain_invoice_processing_queue" in worker
    assert "process_next_queued_invoice_job(worker_id=worker_id)" in worker
    assert 'if __name__ == "__main__":' in worker
