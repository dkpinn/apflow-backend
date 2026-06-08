"""
organisation_extraction_settings.py
------------------------------------
Read/update organisation-level extraction & reporting settings
(extraction_strategy, vlm_enabled, auto_link_amount_tiers, reporting_standard, etc).
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.db.supabase_client import get_supabase_client
from app.services.supplier_matching_config import serialise_amount_tiers

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]


def get_organisation_extraction_settings(organisation_id: str, *, db=None) -> dict:
    client = db or supabase
    try:
        settings_res = (
            client
            .table("organisations")
            .select(
                "extraction_strategy, ask_per_upload, vlm_enabled, "
                "supplier_auto_link_min_matches, auto_link_amount_tiers, "
                "reporting_standard, income_statement_presentation"
            )
            .eq("id", organisation_id)
            .limit(1)
            .execute()
        )
        settings = settings_res.data[0] if settings_res.data else {}
    except Exception:
        # Fallback if newer columns not yet migrated — extraction still proceeds with defaults
        try:
            settings_res = (
                client
                .table("organisations")
                .select("extraction_strategy, ask_per_upload, vlm_enabled, supplier_auto_link_min_matches")
                .eq("id", organisation_id)
                .limit(1)
                .execute()
            )
            settings = settings_res.data[0] if settings_res.data else {}
        except Exception:
            settings = {}
    min_matches = settings.get("supplier_auto_link_min_matches")
    try:
        min_matches = int(min_matches)
    except (TypeError, ValueError):
        min_matches = 2
    reporting_standard = settings.get("reporting_standard") or "ifrs"
    presentation = settings.get("income_statement_presentation") or "function"
    if reporting_standard == "us_gaap":
        presentation = "function"
    try:
        amount_tiers = serialise_amount_tiers(settings.get("auto_link_amount_tiers") or [])
    except (ValueError, Exception):
        amount_tiers = []
    return {
        "extraction_strategy": settings.get("extraction_strategy") or "auto_group",
        "ask_per_upload": bool(settings.get("ask_per_upload")),
        "vlm_enabled": bool(settings.get("vlm_enabled")),
        "supplier_auto_link_min_matches": min(4, max(1, min_matches)),
        "auto_link_amount_tiers": amount_tiers,
        "reporting_standard": reporting_standard,
        "income_statement_presentation": presentation,
    }


def update_organisation_extraction_settings(organisation_id: str, updates: dict, *, db=None) -> dict:
    if not updates:
        raise ValueError("No settings provided to update")

    client = db or supabase
    update_res = (
        client
        .table("organisations")
        .update(updates)
        .eq("id", organisation_id)
        .execute()
    )

    if not update_res.data:
        raise HTTPException(status_code=404, detail="Organisation not found")

    return get_organisation_extraction_settings(organisation_id, db=client)
