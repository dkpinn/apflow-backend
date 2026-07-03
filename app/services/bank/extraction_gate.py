from __future__ import annotations

from typing import Any


def _upload_id_for_line(line: dict[str, Any]) -> str | None:
    upload_id = line.get("bank_statement_upload_id")
    return str(upload_id) if upload_id else None


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
