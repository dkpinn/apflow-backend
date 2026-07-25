from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers import finance_leases


ORG_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
AUTH = ("user-1", None)


class _Result:
    def __init__(self, data=None):
        self.data = data or []


class _Table:
    def __init__(self, inserts):
        self.inserts = inserts

    def insert(self, row):
        self.inserts.append(row)
        return self

    def execute(self):
        return _Result()


class _DB:
    def __init__(self):
        self.inserts = []

    def table(self, _name):
        return _Table(self.inserts)


@pytest.mark.parametrize(
    "bucket,path",
    [
        ("statement-files", f"{ORG_ID}/finance-leases/lease.pdf"),
        ("finance-lease-docs", f"{OTHER_ORG_ID}/finance-leases/lease.pdf"),
        ("finance-lease-docs", f"{ORG_ID}/../{OTHER_ORG_ID}/lease.pdf"),
        ("finance-lease-docs", f"{ORG_ID}/%2e%2e/{OTHER_ORG_ID}/lease.pdf"),
        ("finance-lease-docs", f"{ORG_ID}\\finance-leases\\lease.pdf"),
    ],
)
def test_upload_document_rejects_untrusted_storage_reference(monkeypatch, bucket, path):
    db = _DB()
    monkeypatch.setattr(finance_leases, "_auth", lambda _auth: ("user-1", db))
    monkeypatch.setattr(finance_leases, "ensure_org_write", lambda *_args: None)
    payload = finance_leases.UploadDocumentRequest(
        organisation_id=ORG_ID,
        original_filename="lease.pdf",
        mime_type="application/pdf",
        storage_bucket=bucket,
        storage_path=path,
        extract=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        finance_leases.upload_lease_document("new", payload, AUTH)

    assert exc_info.value.status_code == 422
    assert db.inserts == []


def test_upload_document_records_valid_org_owned_object(monkeypatch):
    db = _DB()
    monkeypatch.setattr(finance_leases, "_auth", lambda _auth: ("user-1", db))
    monkeypatch.setattr(finance_leases, "ensure_org_write", lambda *_args: None)
    path = f"{ORG_ID}/finance-leases/lease.pdf"
    payload = finance_leases.UploadDocumentRequest(
        organisation_id=ORG_ID,
        original_filename="lease.pdf",
        mime_type="application/pdf",
        storage_path=path,
        extract=False,
    )

    result = finance_leases.upload_lease_document("new", payload, AUTH)

    assert result["success"] is True
    assert db.inserts[0]["storage_bucket"] == finance_leases.FINANCE_LEASE_DOCUMENT_BUCKET
    assert db.inserts[0]["storage_path"] == path
