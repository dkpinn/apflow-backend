import pytest
from fastapi import HTTPException

import app.routers.invoice_extraction_admin as admin


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        return _Result(self.rows)


class _Db:
    def __init__(self, case):
        self.case = case

    def table(self, name):
        assert name == "invoice_extraction_gold_documents"
        return _Query([self.case])


def _locked_case():
    return {
        "id": "gold-1",
        "dataset_split": "locked",
        "invoice_extracted_id": "invoice-1",
    }


def _development_case():
    return {
        "id": "gold-1",
        "dataset_split": "development",
        "invoice_extracted_id": "invoice-1",
        "gold_json": {
            "schema_version": 1,
            "document": {},
            "line_items": [],
        },
    }


def test_locked_gold_truth_cannot_be_replaced(monkeypatch):
    monkeypatch.setattr(admin, "_platform_db", lambda _auth: ("owner-1", _Db(_locked_case())))

    with pytest.raises(HTTPException) as exc_info:
        admin.update_gold_document(
            "gold-1",
            admin.GoldDocumentUpdate(gold_json={"document": {}, "line_items": []}),
            ("owner-1", object()),
        )

    assert exc_info.value.status_code == 409
    assert "frozen" in exc_info.value.detail


def test_locked_gold_truth_cannot_be_recaptured_from_current_invoice(monkeypatch):
    monkeypatch.setattr(admin, "_platform_db", lambda _auth: ("owner-1", _Db(_locked_case())))

    with pytest.raises(HTTPException) as exc_info:
        admin.capture_current_as_gold("gold-1", ("owner-1", object()))

    assert exc_info.value.status_code == 409
    assert "frozen" in exc_info.value.detail


def test_outdated_gold_truth_cannot_be_moved_to_locked_split(monkeypatch):
    monkeypatch.setattr(admin, "_platform_db", lambda _auth: ("owner-1", _Db(_development_case())))

    with pytest.raises(HTTPException) as exc_info:
        admin.update_gold_document(
            "gold-1",
            admin.GoldDocumentUpdate(dataset_split="locked"),
            ("owner-1", object()),
        )

    assert exc_info.value.status_code == 400
    assert "cannot be locked" in exc_info.value.detail["message"]
    assert any("schema is outdated" in blocker for blocker in exc_info.value.detail["blockers"])
