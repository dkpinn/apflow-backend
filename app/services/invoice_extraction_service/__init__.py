"""
invoice_extraction_service package
------------------------------------
Re-exports all public symbols so that external callers can continue to use:

    from app.services.invoice_extraction_service import run_invoice_extraction, ...

without any changes after the flat .py file was converted to this package.

Internal submodule layout:
  _job_tracking  — DB-backed re-extract job state + DB extract job status helpers
  _helpers       — error/context helpers + DB lookups + file/storage helpers
  _pipeline      — run_invoice_extraction (primary OCR → parse → persist pipeline)
  _reextraction  — run_invoice_re_extraction + run_reextract_job_background
  _queue         — queue_invoice_job + worker drain helpers
"""
from ._job_tracking import (
    EXTRACT_WORKER_LOCK,
    EXTRACT_STAGE_LABELS,
    EXTRACT_STAGE_PROGRESS,
    REEXTRACT_DEFAULT_DIAGNOSTIC,
    REEXTRACT_STAGE_LABELS,
    REEXTRACT_STAGE_PROGRESS,
    build_extract_job_status,
    create_reextract_job,
    get_extracted_invoice_id_for_raw,
    get_processing_job,
    get_reextract_job_status,
    update_reextract_job,
)
from ._helpers import (
    _resolve_reextract_context,
    _stringify_http_detail,
    get_organisation,
    get_raw_invoice,
    log_reextract_failure,
    rename_invoice_file_after_extraction,
    store_basic_document_page_snapshot,
)
from ._pipeline import run_invoice_extraction
from ._reextraction import run_invoice_re_extraction, run_reextract_job_background
from ._queue import (
    process_next_queued_invoice_job,
    queue_invoice_job,
    run_extract_worker_until_empty,
)

__all__ = [
    # Job tracking
    "EXTRACT_WORKER_LOCK",
    "EXTRACT_STAGE_LABELS",
    "EXTRACT_STAGE_PROGRESS",
    "REEXTRACT_DEFAULT_DIAGNOSTIC",
    "REEXTRACT_STAGE_LABELS",
    "REEXTRACT_STAGE_PROGRESS",
    "build_extract_job_status",
    "create_reextract_job",
    "get_extracted_invoice_id_for_raw",
    "get_processing_job",
    "get_reextract_job_status",
    "update_reextract_job",
    # Helpers
    "_resolve_reextract_context",
    "_stringify_http_detail",
    "get_organisation",
    "get_raw_invoice",
    "log_reextract_failure",
    "rename_invoice_file_after_extraction",
    "store_basic_document_page_snapshot",
    # Pipelines
    "run_invoice_extraction",
    "run_invoice_re_extraction",
    "run_reextract_job_background",
    # Queue / worker
    "process_next_queued_invoice_job",
    "queue_invoice_job",
    "run_extract_worker_until_empty",
]
