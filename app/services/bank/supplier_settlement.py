from __future__ import annotations

from typing import Any


def accept_supplier_invoice_bank_match(
    db,
    *,
    organisation_id: str,
    bank_statement_line_id: str,
    suggestion_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Atomically settle a posted supplier invoice from a matched bank line."""
    result = db.rpc(
        "accept_supplier_invoice_bank_match_atomic",
        {
            "p_org_id": organisation_id,
            "p_bank_statement_line_id": bank_statement_line_id,
            "p_suggestion_id": suggestion_id,
            "p_user_id": actor_user_id,
        },
    ).execute()
    data = getattr(result, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not data.get("journal_id"):
        raise ValueError("Supplier invoice settlement did not return a posted journal")
    return data


def reverse_supplier_invoice_bank_match(
    db,
    *,
    organisation_id: str,
    journal_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Atomically reverse the journal and remove its supplier settlement."""
    result = db.rpc(
        "reverse_supplier_invoice_bank_match_atomic",
        {
            "p_org_id": organisation_id,
            "p_journal_id": journal_id,
            "p_user_id": actor_user_id,
        },
    ).execute()
    data = getattr(result, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not data.get("reversal_id"):
        raise ValueError("Supplier invoice settlement reversal did not return a reversal journal")
    return data
