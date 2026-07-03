from __future__ import annotations

from typing import Any


def _upload_id_for_line(line: dict[str, Any]) -> str | None:
    upload_id = line.get("bank_statement_upload_id")
    return str(upload_id) if upload_id else None


def gold_json_source_upload_id(gold_json: object) -> str | None:
    if not isinstance(gold_json, dict):
        return None
    source_upload_id = gold_json.get("_apflow_source_upload_id") or gold_json.get("source_upload_id")
    return str(source_upload_id) if source_upload_id else None


def corrected_fixture_benchmark_blockers(
    db,
    *,
    organisation_id: str,
    upload_id: str,
) -> list[str]:
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
    if not upload_gold_files:
        return []

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
    return []


def assert_upload_corrected_fixtures_benchmarked(
    db,
    *,
    organisation_id: str,
    upload_id: str,
    action: str,
) -> None:
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
        .select("id, extraction_status")
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
        .select("id, extraction_status")
        .eq("organisation_id", organisation_id)
        .in_("id", upload_ids)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    statuses = {str(row.get("id")): str(row.get("extraction_status") or "").lower() for row in rows}
    blocked = [upload_id for upload_id in upload_ids if statuses.get(upload_id) != "extracted"]
    if blocked:
        raise ValueError(
            f"{action} is blocked until all bank statement extractions are reviewed and approved"
        )
    for upload_id in upload_ids:
        assert_upload_corrected_fixtures_benchmarked(
            db,
            organisation_id=organisation_id,
            upload_id=upload_id,
            action=action,
        )
