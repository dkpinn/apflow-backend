from __future__ import annotations

import os
from typing import Any

from app.services.bank_extraction_validation import source_requires_manual_review


def _gold_fixture_required_globally() -> bool:
    """Whether a corrected gold fixture + passing benchmark is a hard prerequisite
    for reconciling/posting a PDF/image upload.

    Off by default: gold files and benchmarks remain available as an internal
    accuracy tool, but they no longer gate everyday imports. Set
    BANK_REQUIRE_GOLD_FIXTURE=1 to restore the strict per-upload requirement.
    """
    return os.getenv("BANK_REQUIRE_GOLD_FIXTURE", "").strip().lower() in {"1", "true", "yes", "on"}


def _upload_id_for_line(line: dict[str, Any]) -> str | None:
    upload_id = line.get("bank_statement_upload_id")
    return str(upload_id) if upload_id else None


def gold_json_source_upload_id(gold_json: object) -> str | None:
    if not isinstance(gold_json, dict):
        return None
    source_upload_id = gold_json.get("_apflow_source_upload_id") or gold_json.get("source_upload_id")
    return str(source_upload_id) if source_upload_id else None


def corrected_fixture_rows_for_upload(
    db,
    *,
    organisation_id: str,
    upload_id: str,
) -> list[dict[str, Any]]:
    events = (
        db.table("bank_audit_events")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("event_type", "bank_statement_gold_file_created")
        .eq("bank_statement_upload_id", upload_id)
        .execute()
        .data
        or []
    )
    audit_gold_file_ids = {
        str((event.get("details") or {}).get("gold_file_id"))
        for event in events
        if isinstance(event.get("details"), dict) and (event.get("details") or {}).get("gold_file_id")
    }
    gold_files = (
        db.table("bank_statement_gold_files")
        .select("*")
        .eq("organisation_id", organisation_id)
        .execute()
        .data
        or []
    )
    upload_gold_files = [
        row
        for row in gold_files
        if gold_json_source_upload_id(row.get("gold_json")) == upload_id
        or str(row.get("id")) in audit_gold_file_ids
    ]
    return upload_gold_files


def upload_requires_corrected_fixture(upload: dict[str, Any]) -> bool:
    if not _gold_fixture_required_globally():
        return False
    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    raw_extraction = upload.get("raw_extraction") if isinstance(upload.get("raw_extraction"), dict) else {}
    pdf_rescue = evidence.get("pdf_rescue")
    if not isinstance(pdf_rescue, dict):
        pdf_rescue = raw_extraction.get("pdf_rescue") if isinstance(raw_extraction.get("pdf_rescue"), dict) else None
    return source_requires_manual_review(
        {
            "source_format": upload.get("source_format") or evidence.get("source_format") or raw_extraction.get("source_format"),
            "parser_strategy": evidence.get("parser_strategy") or raw_extraction.get("parser_strategy"),
            "pdf_rescue": pdf_rescue,
        }
    )


def _timestamp_text(value: Any) -> str:
    return str(value or "").strip()


def corrected_fixture_benchmark_blockers(
    db,
    *,
    organisation_id: str,
    upload_id: str,
) -> list[str]:
    upload_gold_files = corrected_fixture_rows_for_upload(
        db,
        organisation_id=organisation_id,
        upload_id=upload_id,
    )
    if not upload_gold_files:
        return []

    upload_rows = (
        db.table("bank_statement_uploads")
        .select("id, extracted_at")
        .eq("organisation_id", organisation_id)
        .eq("id", upload_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    upload_extracted_at = _timestamp_text((upload_rows[0] if upload_rows else {}).get("extracted_at"))

    document_ids = {
        str(row.get("document_id"))
        for row in upload_gold_files
        if row.get("document_id")
    }
    runs = (
        db.table("bank_statement_extraction_runs")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_upload_id", upload_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    latest_runs_by_document: dict[str, dict[str, Any]] = {}
    for row in runs:
        document_id = str(row.get("document_id") or "")
        if document_ids and document_id not in document_ids:
            continue
        latest_runs_by_document[document_id] = row
    if not latest_runs_by_document or document_ids - set(latest_runs_by_document):
        return ["Corrected gold fixture has not been benchmarked against the parser output"]
    if any(row.get("can_allocate") is not True for row in latest_runs_by_document.values()):
        return ["Corrected gold fixture benchmark did not match the parser output"]
    for gold_file in upload_gold_files:
        document_id = str(gold_file.get("document_id") or "")
        latest_run = latest_runs_by_document.get(document_id)
        if not latest_run:
            continue
        run_created_at = _timestamp_text(latest_run.get("created_at"))
        gold_verified_at = _timestamp_text(gold_file.get("verified_at") or gold_file.get("created_at"))
        if gold_verified_at and (not run_created_at or run_created_at < gold_verified_at):
            return ["Corrected gold fixture benchmark is stale; rerun it after the latest correction"]
        if upload_extracted_at and (not run_created_at or run_created_at < upload_extracted_at):
            return ["Corrected gold fixture benchmark is stale; rerun it after the latest extraction"]
    return []


def assert_upload_corrected_fixtures_benchmarked(
    db,
    *,
    organisation_id: str,
    upload_id: str,
    action: str,
    require_fixture: bool = False,
) -> None:
    if require_fixture and not corrected_fixture_rows_for_upload(
        db,
        organisation_id=organisation_id,
        upload_id=upload_id,
    ):
        raise ValueError(
            f"{action} is blocked: PDF/image/VLM bank statement extraction requires a corrected gold fixture and passing benchmark"
        )
    # Only enforce benchmark freshness when gold fixtures are a hard requirement.
    # With the default (optional/internal) mode, an un-benchmarked gold file saved
    # for accuracy testing must never block an everyday reconcile/post.
    if not _gold_fixture_required_globally():
        return
    blockers = corrected_fixture_benchmark_blockers(
        db,
        organisation_id=organisation_id,
        upload_id=upload_id,
    )
    if blockers:
        raise ValueError(f"{action} is blocked: {blockers[0]}")


def assert_bank_line_upload_extracted(
    db,
    *,
    organisation_id: str,
    line: dict[str, Any],
    action: str,
) -> None:
    upload_id = _upload_id_for_line(line)
    if not upload_id:
        raise ValueError(f"{action} requires a bank statement line linked to a verified upload")

    res = (
        db.table("bank_statement_uploads")
        .select("id, extraction_status, source_format, raw_extraction, extraction_evidence")
        .eq("id", upload_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError(f"{action} requires a verified bank statement upload")
    status = str(rows[0].get("extraction_status") or "").lower()
    if status != "extracted":
        raise ValueError(
            f"{action} is blocked until the bank statement extraction is reviewed and approved"
        )
    assert_upload_corrected_fixtures_benchmarked(
        db,
        organisation_id=organisation_id,
        upload_id=upload_id,
        action=action,
        require_fixture=upload_requires_corrected_fixture(rows[0]),
    )


def assert_bank_lines_uploads_extracted(
    db,
    *,
    organisation_id: str,
    lines: list[dict[str, Any]],
    action: str,
) -> None:
    if any(not _upload_id_for_line(line) for line in lines):
        raise ValueError(f"{action} requires bank statement lines linked to verified uploads")
    upload_ids = sorted({upload_id for line in lines if (upload_id := _upload_id_for_line(line))})
    if not upload_ids:
        return

    res = (
        db.table("bank_statement_uploads")
        .select("id, extraction_status, source_format, raw_extraction, extraction_evidence")
        .eq("organisation_id", organisation_id)
        .in_("id", upload_ids)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    uploads_by_id = {str(row.get("id")): row for row in rows}
    statuses = {upload_id: str(row.get("extraction_status") or "").lower() for upload_id, row in uploads_by_id.items()}
    blocked = [upload_id for upload_id in upload_ids if statuses.get(upload_id) != "extracted"]
    if blocked:
        raise ValueError(
            f"{action} is blocked until all bank statement extractions are reviewed and approved"
        )
    for upload_id in upload_ids:
        upload = uploads_by_id.get(upload_id) or {}
        assert_upload_corrected_fixtures_benchmarked(
            db,
            organisation_id=organisation_id,
            upload_id=upload_id,
            action=action,
            require_fixture=upload_requires_corrected_fixture(upload),
        )
