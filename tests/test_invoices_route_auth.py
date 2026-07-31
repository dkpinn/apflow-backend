from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from app.dependencies import authenticated_user
import app.routers.invoices as invoices
import app.routers.invoices_queue as invoices_queue
import app.routers.invoices_review as invoices_review
from tests.conftest import MemoryDB

AUTH = ("user-1", None)


class _FailStorageDB:
    @property
    def storage(self):
        raise AssertionError("storage should not be touched before auth passes")


class _StorageBucket:
    def __init__(self):
        self.removed = []

    def remove(self, paths):
        self.removed.extend(paths)


class _StorageClient:
    def __init__(self, bucket):
        self.bucket = bucket

    def from_(self, name):
        assert name == "invoices"
        return self.bucket


class _MemoryDBWithStorage(MemoryDB):
    def __init__(self, tables):
        super().__init__(tables)
        self.storage_bucket = _StorageBucket()
        self.storage = _StorageClient(self.storage_bucket)


def test_process_next_invoice_job_requires_write_access(monkeypatch):
    processed = []
    monkeypatch.setattr(
        invoices,
        "ensure_org_write",
        lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")),
    )
    monkeypatch.setattr(invoices, "process_next_queued_invoice_job", lambda **kwargs: processed.append(kwargs))
    payload = invoices.ProcessNextJobRequest(organisation_id="org-1")

    with pytest.raises(HTTPException) as exc_info:
        invoices.process_next_invoice_job(payload, AUTH)

    assert exc_info.value.status_code == 403
    assert processed == []


def test_get_invoice_raw_file_requires_read_access_before_storage(monkeypatch):
    monkeypatch.setattr(invoices, "supabase", _FailStorageDB())
    monkeypatch.setattr(
        invoices,
        "get_raw_invoice",
        lambda _raw_id: {
            "id": "raw-1",
            "organisation_id": "org-1",
            "file_path": "org-1/invoices/file.pdf",
            "file_type": "application/pdf",
        },
    )
    monkeypatch.setattr(
        invoices,
        "ensure_org_read",
        lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")),
    )

    with pytest.raises(HTTPException) as exc_info:
        invoices.get_invoice_raw_file("raw-1", AUTH)

    assert exc_info.value.status_code == 403


def test_save_line_items_authorizes_before_supplier_lookup(monkeypatch):
    supplier_lookups = []
    monkeypatch.setattr(
        invoices,
        "_load_extracted_invoice_for_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")),
    )
    monkeypatch.setattr(invoices, "fetch_supplier_processing_settings", lambda *_args: supplier_lookups.append(True))
    payload = invoices.SaveLineItemsRequest(
        invoice_extracted_id="inv-1",
        organisation_id="org-1",
        supplier_id="sup-1",
        line_items=[],
    )

    with pytest.raises(HTTPException) as exc_info:
        invoices.save_invoice_line_items(payload, AUTH)

    assert exc_info.value.status_code == 403
    assert supplier_lookups == []


def test_invoice_review_fastapi_calls_require_bearer_token():
    with pytest.raises(HTTPException) as exc_info:
        authenticated_user(authorization=None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Missing bearer token"


def test_invoice_detail_fastapi_routes_keep_auth_dependency():
    protected_handlers = [
        invoices_review.get_invoice_review_data,
        invoices_review.update_invoice_document_fields,
        invoices.save_invoice_line_items,
        invoices.reapply_supplier_rules_endpoint,
        invoices.generate_invoice_preview,
        invoices_queue.re_extract_invoice,
        invoices_queue.get_re_extract_status,
        invoices.reset_invoice_for_reparse,
        invoices.delete_invoice_upload,
    ]

    for handler in protected_handlers:
        signature = inspect.signature(handler)
        assert "auth" in signature.parameters, f"{handler.__name__} must accept auth"
        assert signature.parameters["auth"].default is inspect.Signature.empty


def test_reset_invoice_authorizes_before_deleting(monkeypatch):
    monkeypatch.setattr(
        invoices,
        "get_raw_invoice",
        lambda _raw_id: {"id": "raw-1", "organisation_id": "org-1"},
    )
    monkeypatch.setattr(
        invoices,
        "ensure_org_write",
        lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")),
    )
    monkeypatch.setattr(invoices, "supabase", _FailStorageDB())

    with pytest.raises(HTTPException) as exc_info:
        invoices.reset_invoice_for_reparse(
            "raw-1",
            invoices.ResetInvoiceRequest(organisation_id="org-1"),
            AUTH,
        )

    assert exc_info.value.status_code == 403


def test_reset_invoice_removes_derived_data_and_queues(monkeypatch):
    db = MemoryDB({
        "invoices_raw": [{"id": "raw-1", "organisation_id": "org-1", "parse_status": "completed"}],
        "invoices_extracted": [{"id": "inv-1", "invoice_raw_id": "raw-1", "posting_status": "unposted"}],
        "invoice_line_items": [{"id": "line-1", "invoice_extracted_id": "inv-1"}],
        "invoice_parse_attempts": [{"id": "attempt-1", "invoice_raw_id": "raw-1"}],
        "invoice_agent_suggestions": [{"id": "suggestion-1", "invoice_raw_id": "raw-1"}],
        "invoice_extraction_feedback": [{"id": "feedback-1", "invoice_raw_id": "raw-1"}],
        "invoice_audit_log": [{"id": "audit-1", "invoice_id": "inv-1"}],
        "document_pages": [{"id": "page-1", "invoice_raw_id": "raw-1"}],
        "invoice_page_groups": [{"id": "group-1", "invoice_raw_id": "raw-1"}],
        "supplier_payment_run_items": [],
    })
    queued = []
    monkeypatch.setattr(invoices, "supabase", db)
    monkeypatch.setattr(invoices, "get_raw_invoice", lambda _raw_id: db.tables["invoices_raw"][0])
    monkeypatch.setattr(invoices, "ensure_org_write", lambda *_args: None)
    monkeypatch.setattr(
        invoices,
        "queue_invoice_job",
        lambda **kwargs: queued.append(kwargs) or {"id": "job-1"},
    )

    result = invoices.reset_invoice_for_reparse(
        "raw-1",
        invoices.ResetInvoiceRequest(organisation_id="org-1"),
        AUTH,
    )

    assert result["status"] == "queued"
    assert queued == [{"invoice_raw_id": "raw-1", "organisation_id": "org-1"}]
    assert db.tables["invoices_extracted"] == []
    assert db.tables["invoice_line_items"] == []
    assert db.tables["document_pages"] == []
    assert db.tables["invoices_raw"][0]["parse_status"] == "pending"


def test_reset_invoice_rejects_posted_invoice(monkeypatch):
    db = MemoryDB({
        "invoices_extracted": [{"id": "inv-1", "invoice_raw_id": "raw-1", "posting_status": "posted"}],
        "supplier_payment_run_items": [],
    })
    monkeypatch.setattr(invoices, "supabase", db)
    monkeypatch.setattr(
        invoices,
        "get_raw_invoice",
        lambda _raw_id: {"id": "raw-1", "organisation_id": "org-1"},
    )
    monkeypatch.setattr(invoices, "ensure_org_write", lambda *_args: None)

    with pytest.raises(HTTPException) as exc_info:
        invoices.reset_invoice_for_reparse(
            "raw-1",
            invoices.ResetInvoiceRequest(organisation_id="org-1"),
            AUTH,
        )

    assert exc_info.value.status_code == 409
    assert db.tables["invoices_extracted"][0]["id"] == "inv-1"


def test_delete_invoice_removes_database_rows_and_storage(monkeypatch):
    raw = {
        "id": "raw-1",
        "organisation_id": "org-1",
        "file_path": "org-1/invoices/invoice.pdf",
    }
    db = _MemoryDBWithStorage({
        "invoices_raw": [raw],
        "invoices_extracted": [{"id": "inv-1", "invoice_raw_id": "raw-1", "posting_status": "unposted"}],
        "invoice_line_items": [{"id": "line-1", "invoice_extracted_id": "inv-1"}],
        "invoice_parse_attempts": [{"id": "attempt-1", "invoice_raw_id": "raw-1"}],
        "invoice_agent_suggestions": [{"id": "suggestion-1", "invoice_raw_id": "raw-1"}],
        "invoice_extraction_feedback": [{"id": "feedback-1", "invoice_raw_id": "raw-1"}],
        "invoice_audit_log": [{"id": "audit-1", "invoice_id": "inv-1"}],
        "invoice_audit_events": [{"id": "event-1", "invoice_raw_id": "raw-1"}],
        "document_pages": [{"id": "page-1", "invoice_raw_id": "raw-1"}],
        "invoice_page_groups": [{"id": "group-1", "invoice_raw_id": "raw-1"}],
        "supplier_payment_run_items": [],
    })
    monkeypatch.setattr(invoices, "supabase", db)
    monkeypatch.setattr(invoices, "get_raw_invoice", lambda _raw_id: raw)
    monkeypatch.setattr(invoices, "ensure_org_write", lambda *_args: None)

    result = invoices.delete_invoice_upload("raw-1", AUTH, organisation_id="org-1")

    assert result == {"success": True, "invoice_raw_id": "raw-1", "storage_deleted": True}
    assert db.tables["invoices_raw"] == []
    assert db.tables["invoices_extracted"] == []
    assert db.tables["invoice_audit_events"] == []
    assert db.storage_bucket.removed == ["org-1/invoices/invoice.pdf"]
