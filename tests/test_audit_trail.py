import pytest
from fastapi import HTTPException

import app.routers.audit_trail as audit_router
from app.services.audit_trail import list_audit_trail
from tests.conftest import MemoryDB


ORG_ID = "org-1"


def _tables():
    return {
        "audit_log": [
            {
                "id": "general-1",
                "organisation_id": ORG_ID,
                "user_id": "user-1",
                "entity_type": "supplier",
                "entity_id": "supplier-1",
                "action_type": "updated",
                "action_summary": "Supplier updated",
                "metadata_json": {"field": "vat_number"},
                "created_at": "2026-06-04T08:00:00Z",
            }
        ],
        "bank_audit_events": [
            {
                "id": "bank-1",
                "organisation_id": ORG_ID,
                "bank_account_id": "bank-account-1",
                "bank_statement_line_id": "line-1",
                "event_type": "bank_line_reviewed",
                "actor_user_id": "user-2",
                "actor_type": "user",
                "details": {"note": "reviewed"},
                "created_at": "2026-06-03T08:00:00Z",
            }
        ],
        "invoice_audit_events": [
            {
                "id": "invoice-1",
                "organisation_id": ORG_ID,
                "invoice_raw_id": "raw-1",
                "invoice_extracted_id": "invoice-extracted-1",
                "event_type": "invoice_extracted_created",
                "stage": "extraction",
                "field_name": None,
                "actor_type": "system",
                "actor_user_id": None,
                "source": "fastapi",
                "notes": "created",
                "created_at": "2026-06-02T08:00:00Z",
            }
        ],
        "sales_invoice_audit_events": [
            {
                "id": "sales-1",
                "organisation_id": ORG_ID,
                "sales_invoice_id": "sales-invoice-1",
                "event_type": "issued",
                "actor_user_id": "user-3",
                "details": {"invoice_number": "INV-1"},
                "created_at": "2026-06-01T08:00:00Z",
            }
        ],
    }


def test_audit_trail_combines_sources_newest_first():
    result = list_audit_trail(MemoryDB(_tables()), organisation_id=ORG_ID)

    assert [row["source"] for row in result["events"]] == ["general", "bank", "invoice", "sales"]
    assert result["summary"]["total"] == 4
    assert result["summary"]["by_source"]["bank"] == 1


def test_audit_trail_filters_by_source_actor_entity_and_search():
    result = list_audit_trail(
        MemoryDB(_tables()),
        organisation_id=ORG_ID,
        source="bank",
        actor_user_id="user-2",
        entity_type="bank_statement_line",
        entity_id="line-1",
        search="reviewed",
    )

    assert result["summary"]["total"] == 1
    assert result["events"][0]["event_type"] == "bank_line_reviewed"


def test_audit_trail_filters_by_date_range():
    result = list_audit_trail(
        MemoryDB(_tables()),
        organisation_id=ORG_ID,
        date_from="2026-06-02",
        date_to="2026-06-03",
    )

    assert [row["source"] for row in result["events"]] == ["bank", "invoice"]


def test_audit_trail_rejects_invalid_source_and_date_range():
    with pytest.raises(ValueError, match="source"):
        list_audit_trail(MemoryDB(_tables()), organisation_id=ORG_ID, source="bad")

    with pytest.raises(ValueError, match="date_from"):
        list_audit_trail(
            MemoryDB(_tables()),
            organisation_id=ORG_ID,
            date_from="2026-06-03",
            date_to="2026-06-02",
        )


def test_audit_trail_route_enforces_read_permission(monkeypatch):
    db = MemoryDB(_tables())
    calls = []
    monkeypatch.setattr(
        audit_router,
        "ensure_org_read",
        lambda user_id, organisation_id: calls.append((user_id, organisation_id)),
    )

    result = audit_router.audit_trail(
        ORG_ID,
        auth=("user-1", db),
        source=None,
        event_type=None,
        actor_user_id=None,
        entity_type=None,
        entity_id=None,
        date_from=None,
        date_to=None,
        search=None,
        limit=10,
    )

    assert result["success"] is True
    assert result["audit_trail"]["summary"]["total"] == 4
    assert calls == [("user-1", ORG_ID)]


def test_audit_trail_route_returns_400_for_bad_source(monkeypatch):
    monkeypatch.setattr(audit_router, "ensure_org_read", lambda *_args: None)

    with pytest.raises(HTTPException) as exc_info:
        audit_router.audit_trail("org-1", auth=("user-1", MemoryDB(_tables())), source="bad")

    assert exc_info.value.status_code == 400
