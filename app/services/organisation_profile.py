"""
organisation_profile.py
-----------------------
Return the organisation-profile fields needed by the Document Auto-Fill tool.
Reads directly from the organisations table (legal_name, registration_number,
vat_number, physical_address_*, phone are already stored there by the settings
page).  No separate table is needed.
"""
from __future__ import annotations

from app.db.supabase_client import get_supabase_client

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]

_DEFAULTS: dict = {
    "legal_name": "",
    "registration_number": "",
    "vat_number": "",
    "address_line1": "",
    "address_line2": "",
    "city": "",
    "postal_code": "",
    "country": "",
    "phone": "",
    "email": "",
}


def get_organisation_profile(organisation_id: str, *, db=None) -> dict:
    """
    Return organisation profile data for the document auto-fill tool.
    Maps physical_address_* columns to the simple address_line* keys the
    frontend expects.
    """
    client = db or supabase
    try:
        res = (
            client
            .table("organisations")
            .select(
                "legal_name,registration_number,vat_number,"
                "physical_address_line_1,physical_address_line_2,"
                "physical_city,physical_postal_code,country,phone,primary_email"
            )
            .eq("id", organisation_id)
            .limit(1)
            .execute()
        )
        if res.data:
            row = res.data[0]
            return {
                "legal_name": row.get("legal_name") or "",
                "registration_number": row.get("registration_number") or "",
                "vat_number": row.get("vat_number") or "",
                "address_line1": row.get("physical_address_line_1") or "",
                "address_line2": row.get("physical_address_line_2") or "",
                "city": row.get("physical_city") or "",
                "postal_code": row.get("physical_postal_code") or "",
                "country": row.get("country") or "",
                "phone": row.get("phone") or "",
                "email": row.get("primary_email") or "",
            }
    except Exception:
        pass
    return dict(_DEFAULTS)
