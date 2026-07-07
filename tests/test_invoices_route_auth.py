from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.invoices as invoices

AUTH = ("user-1", None)


class _FailStorageDB:
    @property
    def storage(self):
        raise AssertionError("storage should not be touched before auth passes")


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
