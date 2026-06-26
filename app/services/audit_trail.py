from __future__ import annotations

from datetime import date
from typing import Any


DEFAULT_LIMIT = 100
MAX_SOURCE_ROWS = 1000
KNOWN_SOURCES = {"general", "bank", "invoice", "sales"}


def _rows(db, table: str, organisation_id: str, select: str = "*") -> list[dict[str, Any]]:
    try:
        return (
            db.table(table)
            .select(select)
            .eq("organisation_id", organisation_id)
            .limit(MAX_SOURCE_ROWS)
            .execute()
            .data
            or []
        )
    except Exception:
        return []


def _parse_date(value: str | None, *, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD format") from exc


def _event_date(row: dict[str, Any]) -> date | None:
    created_at = row.get("created_at")
    if not created_at:
        return None
    try:
        return date.fromisoformat(str(created_at)[:10])
    except ValueError:
        return None


def _entity_from_bank(row: dict[str, Any]) -> tuple[str, str | None]:
    for entity_type, key in (
        ("bank_statement_line", "bank_statement_line_id"),
        ("bank_statement_upload", "bank_statement_upload_id"),
        ("gl_journal", "gl_journal_id"),
        ("bank_account", "bank_account_id"),
    ):
        if row.get(key):
            return entity_type, str(row.get(key))
    return "bank", None


def _entity_from_invoice(row: dict[str, Any]) -> tuple[str, str | None]:
    for entity_type, key in (
        ("supplier_invoice", "invoice_extracted_id"),
        ("invoice_raw", "invoice_raw_id"),
        ("document_job", "job_id"),
    ):
        if row.get(key):
            return entity_type, str(row.get(key))
    return "invoice", None


def _contains(row: dict[str, Any], needle: str) -> bool:
    haystack = " ".join(
        str(row.get(key) or "")
        for key in (
            "source",
            "event_type",
            "entity_type",
            "entity_id",
            "actor_user_id",
            "summary",
            "details",
        )
    ).lower()
    return needle in haystack


def _generic_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source": "general",
            "id": row.get("id"),
            "organisation_id": row.get("organisation_id"),
            "event_type": row.get("action_type"),
            "entity_type": row.get("entity_type"),
            "entity_id": str(row.get("entity_id")) if row.get("entity_id") else None,
            "actor_user_id": str(row.get("user_id")) if row.get("user_id") else None,
            "actor_type": "user" if row.get("user_id") else "system",
            "summary": row.get("action_summary") or row.get("action_type"),
            "details": row.get("metadata_json") or {},
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]


def _bank_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        entity_type, entity_id = _entity_from_bank(row)
        events.append({
            "source": "bank",
            "id": row.get("id"),
            "organisation_id": row.get("organisation_id"),
            "event_type": row.get("event_type"),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor_user_id": str(row.get("actor_user_id")) if row.get("actor_user_id") else None,
            "actor_type": row.get("actor_type") or "user",
            "summary": row.get("event_type"),
            "details": row.get("details") or {},
            "created_at": row.get("created_at"),
        })
    return events


def _invoice_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        entity_type, entity_id = _entity_from_invoice(row)
        summary_bits = [row.get("event_type")]
        if row.get("stage"):
            summary_bits.append(f"stage={row.get('stage')}")
        if row.get("field_name"):
            summary_bits.append(f"field={row.get('field_name')}")
        events.append({
            "source": "invoice",
            "id": row.get("id"),
            "organisation_id": row.get("organisation_id"),
            "event_type": row.get("event_type"),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor_user_id": str(row.get("actor_user_id")) if row.get("actor_user_id") else None,
            "actor_type": row.get("actor_type") or "system",
            "summary": " | ".join(str(bit) for bit in summary_bits if bit),
            "details": {
                "stage": row.get("stage"),
                "field_name": row.get("field_name"),
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "source": row.get("source"),
                "notes": row.get("notes"),
            },
            "created_at": row.get("created_at"),
        })
    return events


def _sales_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source": "sales",
            "id": row.get("id"),
            "organisation_id": row.get("organisation_id"),
            "event_type": row.get("event_type"),
            "entity_type": "sales_invoice",
            "entity_id": str(row.get("sales_invoice_id")) if row.get("sales_invoice_id") else None,
            "actor_user_id": str(row.get("actor_user_id")) if row.get("actor_user_id") else None,
            "actor_type": "user" if row.get("actor_user_id") else "system",
            "summary": row.get("event_type"),
            "details": row.get("details") or {},
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]


def list_audit_trail(
    db,
    *,
    organisation_id: str,
    source: str | None = None,
    event_type: str | None = None,
    actor_user_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    if source and source not in KNOWN_SOURCES:
        raise ValueError("source must be one of general, bank, invoice, or sales")
    start = _parse_date(date_from, field="date_from")
    end = _parse_date(date_to, field="date_to")
    if start and end and start > end:
        raise ValueError("date_from must be on or before date_to")

    events: list[dict[str, Any]] = []
    wanted = {source} if source else KNOWN_SOURCES
    if "general" in wanted:
        events.extend(_generic_events(_rows(db, "audit_log", organisation_id)))
    if "bank" in wanted:
        events.extend(_bank_events(_rows(db, "bank_audit_events", organisation_id)))
    if "invoice" in wanted:
        events.extend(_invoice_events(_rows(db, "invoice_audit_events", organisation_id)))
    if "sales" in wanted:
        events.extend(_sales_events(_rows(db, "sales_invoice_audit_events", organisation_id)))

    needle = (search or "").strip().lower()
    filtered = []
    for row in events:
        row_date = _event_date(row)
        if event_type and row.get("event_type") != event_type:
            continue
        if actor_user_id and row.get("actor_user_id") != actor_user_id:
            continue
        if entity_type and row.get("entity_type") != entity_type:
            continue
        if entity_id and row.get("entity_id") != entity_id:
            continue
        if start and row_date and row_date < start:
            continue
        if end and row_date and row_date > end:
            continue
        if needle and not _contains(row, needle):
            continue
        filtered.append(row)

    filtered.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    capped_limit = max(1, min(limit, 500))
    return {
        "organisation_id": organisation_id,
        "events": filtered[:capped_limit],
        "summary": {
            "total": len(filtered),
            "returned": min(len(filtered), capped_limit),
            "by_source": {
                source_key: sum(1 for row in filtered if row.get("source") == source_key)
                for source_key in sorted(KNOWN_SOURCES)
            },
        },
    }
