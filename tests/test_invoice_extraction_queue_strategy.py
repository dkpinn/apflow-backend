import importlib.util
import pathlib
import sys
import threading
import types
import unittest

# Minimal stubs for dependencies not installed in the test environment.
if "fastapi" not in sys.modules:
    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.HTTPException = type("HTTPException", (Exception,), {})
    sys.modules["fastapi"] = fastapi_stub

if "supabase" not in sys.modules:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = type("Client", (), {})
    supabase_stub.create_client = lambda url, key: object()
    sys.modules["supabase"] = supabase_stub

# Minimal app package structure for relative imports.
sys.modules.setdefault("app", types.ModuleType("app"))
sys.modules.setdefault("app.db", types.ModuleType("app.db"))
sys.modules.setdefault("app.services", types.ModuleType("app.services"))
sys.modules.setdefault(
    "app.services.invoice_extraction_service",
    types.ModuleType("app.services.invoice_extraction_service"),
)

# Stub app.db.supabase_client
supabase_client_mod = types.ModuleType("app.db.supabase_client")
def get_supabase_client():
    return object()
supabase_client_mod.get_supabase_client = get_supabase_client
sys.modules["app.db.supabase_client"] = supabase_client_mod

# Stub audit log helper.
audit_log_mod = types.ModuleType("app.services.audit_log")
audit_log_mod.log_invoice_event = lambda *args, **kwargs: None
sys.modules["app.services.audit_log"] = audit_log_mod

# Stub document job helpers.
doc_jobs_mod = types.ModuleType("app.services.document_jobs")

created_jobs = []


def create_processing_job(supabase, *, organisation_id, invoice_raw_id, batch_id=None, priority=100):
    job = {
        "id": "job-1",
        "invoice_raw_id": invoice_raw_id,
        "organisation_id": organisation_id,
    }
    created_jobs.append(job)
    return job


def get_next_queued_job(supabase, *, organisation_id=None):
    return {
        "id": "job-1",
        "invoice_raw_id": "raw-1",
        "organisation_id": organisation_id or "org-1",
    }


def mark_job_processing(supabase, *, job_id, stage="processing"):
    pass


def mark_job_completed(supabase, *, job_id, stage="completed"):
    pass


def mark_job_failed(supabase, *, job_id, error, stage=None):
    pass


def safe_update_invoice_raw_status(supabase, *, invoice_raw_id, parse_status, extra=None):
    pass


doc_jobs_mod.create_processing_job = create_processing_job
doc_jobs_mod.get_next_queued_job = get_next_queued_job
doc_jobs_mod.mark_job_processing = mark_job_processing
doc_jobs_mod.mark_job_completed = mark_job_completed
doc_jobs_mod.mark_job_failed = mark_job_failed
doc_jobs_mod.safe_update_invoice_raw_status = safe_update_invoice_raw_status
sys.modules["app.services.document_jobs"] = doc_jobs_mod

# Stub invoice data builders.
data_builders_mod = types.ModuleType("app.services.invoice_data_builders")
data_builders_mod.utc_now_iso = lambda: "2026-05-27T00:00:00Z"
sys.modules["app.services.invoice_data_builders"] = data_builders_mod

# Stub invoice extraction helpers.
helpers_mod = types.ModuleType("app.services.invoice_extraction_service._helpers")

def get_raw_invoice(invoice_raw_id: str):
    return {"id": invoice_raw_id, "organisation_id": "org-1"}

helpers_mod.get_raw_invoice = get_raw_invoice
sys.modules["app.services.invoice_extraction_service._helpers"] = helpers_mod

# Stub job tracking.
job_tracking_mod = types.ModuleType("app.services.invoice_extraction_service._job_tracking")
job_tracking_mod.EXTRACT_WORKER_LOCK = threading.Lock()
sys.modules["app.services.invoice_extraction_service._job_tracking"] = job_tracking_mod

# Stub pipeline run.
pipeline_mod = types.ModuleType("app.services.invoice_extraction_service._pipeline")
record = {}

def run_invoice_extraction(*, invoice_raw_id, organisation_id=None, job_id=None, extraction_strategy=None):
    record["invoice_raw_id"] = invoice_raw_id
    record["organisation_id"] = organisation_id
    record["job_id"] = job_id
    record["extraction_strategy"] = extraction_strategy
    return {"success": True}

pipeline_mod.run_invoice_extraction = run_invoice_extraction
sys.modules["app.services.invoice_extraction_service._pipeline"] = pipeline_mod

# Import the queue module under its package name so relative imports resolve.
queue_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "invoice_extraction_service" / "_queue.py"
spec = importlib.util.spec_from_file_location("app.services.invoice_extraction_service._queue", queue_path)
queue = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = queue
spec.loader.exec_module(queue)


class InvoiceExtractionQueueStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        queue.EXTRACTION_STRATEGY_OVERRIDES.clear()

    def test_queue_invoice_job_records_strategy_override(self):
        job = queue.queue_invoice_job(
            invoice_raw_id="raw-1",
            organisation_id="org-1",
            extraction_strategy="vlm",
        )

        self.assertEqual(job["id"], "job-1")
        self.assertIn("job-1", queue.EXTRACTION_STRATEGY_OVERRIDES)
        self.assertEqual(queue.EXTRACTION_STRATEGY_OVERRIDES["job-1"], "vlm")

    def test_process_next_queued_invoice_job_applies_strategy_override(self):
        queue.queue_invoice_job(
            invoice_raw_id="raw-1",
            organisation_id="org-1",
            extraction_strategy="vlm",
        )
        result = queue.process_next_queued_invoice_job(organisation_id="org-1")

        self.assertTrue(result["success"])
        self.assertEqual(record["extraction_strategy"], "vlm")
        self.assertNotIn("job-1", queue.EXTRACTION_STRATEGY_OVERRIDES)


if __name__ == "__main__":
    unittest.main()
