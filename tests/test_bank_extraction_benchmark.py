from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

import app.routers.bank_extraction_benchmark as beb
from app.services.bank_statement_extraction.models import ParsedBankLine
from tests.conftest import MemoryDB


ORG_ID = "00000000-0000-0000-0000-000000000001"
GOLD_ID = "00000000-0000-0000-0000-000000000002"
UPLOAD_ID = "00000000-0000-0000-0000-000000000003"
AUTH = ("user-1", None)


class _MemoryDBWithStorage(MemoryDB):
    class _Bucket:
        def download(self, path):
            assert path == "org/bad.pdf"
            return b"pdf bytes"

    class _Storage:
        def from_(self, bucket):
            assert bucket == "statement-files"
            return _MemoryDBWithStorage._Bucket()

    storage = _Storage()


def _gold_file(**overrides):
    return {
        "id": GOLD_ID,
        "organisation_id": ORG_ID,
        "document_id": "bad-import",
        "bank": "ABSA",
        "account_type": "current_account",
        "document_variant": "pdf",
        "gold_json": {
            "document_id": "bad-import",
            "bank": "ABSA",
            "account_type": "current_account",
            "document_variant": "pdf",
            "statement_start_date": "2024-01-01",
            "statement_end_date": "2024-01-31",
            "opening_balance": 1000,
            "closing_balance": 900,
            "transactions": [
                {
                    "transaction_index": 1,
                    "date": "2024-01-05",
                    "description": "Coffee",
                    "amount": -100,
                    "running_balance": 900,
                }
            ],
        },
        "gold_pdf_storage_bucket": "statement-files",
        "gold_pdf_storage_path": "org/bad.pdf",
        **overrides,
    }


def _line():
    return ParsedBankLine(
        line_date="2024-01-05",
        value_date=None,
        description="Coffee",
        reference=None,
        counterparty=None,
        debit_amount=Decimal("100.00"),
        credit_amount=Decimal("0.00"),
        signed_amount=Decimal("-100.00"),
        balance_amount=Decimal("900.00"),
        currency="ZAR",
    )


def test_run_org_gold_file_benchmark_downloads_source_and_saves_run(monkeypatch):
    db = _MemoryDBWithStorage({
        "bank_statement_gold_files": [_gold_file(gold_json={
            **_gold_file()["gold_json"],
            "_apflow_source_upload_id": UPLOAD_ID,
        })],
        "bank_statement_extraction_runs": [],
    })
    monkeypatch.setattr(beb, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(beb, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(
        beb,
        "extract_statement",
        lambda *_a, **_kw: (
            {
                "extractor": "bank_statement",
                "statement_period_from": "2024-01-01",
                "statement_period_to": "2024-01-31",
                "opening_balance": 1000,
                "closing_balance": 900,
            },
            [_line()],
        ),
    )

    result = beb.run_org_gold_file_benchmark(GOLD_ID, AUTH)

    assert result["success"] is True
    assert result["validation_result"]["can_allocate"] is True
    run = db.tables["bank_statement_extraction_runs"][0]
    assert run["organisation_id"] == ORG_ID
    assert run["bank_statement_upload_id"] == UPLOAD_ID
    assert run["document_id"] == "bad-import"
    assert run["expected_transaction_count"] == 1
    assert run["extracted_transaction_count"] == 1
    assert run["can_allocate"] is True


def test_run_org_gold_file_benchmark_blocks_missing_source(monkeypatch):
    db = MemoryDB({
        "bank_statement_gold_files": [_gold_file(gold_pdf_storage_path=None)],
        "bank_statement_extraction_runs": [],
    })
    monkeypatch.setattr(beb, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(beb, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        beb.run_org_gold_file_benchmark(GOLD_ID, AUTH)

    assert exc_info.value.status_code == 400
    assert "source document" in exc_info.value.detail
