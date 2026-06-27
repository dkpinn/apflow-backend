from __future__ import annotations

from fastapi import APIRouter, Query

from app.dependencies import UserAuth, ensure_org_read
from app.services.suspense_clearing import (
    get_suspense_summary,
    get_suspense_accounts,
    get_unreconciled_bank_lines,
    get_approved_unposted_invoices,
    get_extraction_failures,
)

router = APIRouter(prefix="/api/suspense-clearing", tags=["suspense-clearing"])


@router.get("/summary")
def suspense_summary(auth: UserAuth, organisation_id: str = Query(...)):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    return get_suspense_summary(db, organisation_id)


@router.get("/accounts")
def suspense_accounts(auth: UserAuth, organisation_id: str = Query(...)):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    return {"accounts": get_suspense_accounts(db, organisation_id)}


@router.get("/bank-lines")
def unreconciled_bank_lines(
    auth: UserAuth,
    organisation_id: str = Query(...),
    limit: int = Query(default=200, ge=1, le=500),
):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    items = get_unreconciled_bank_lines(db, organisation_id, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/approved-unposted")
def approved_unposted_invoices(
    auth: UserAuth,
    organisation_id: str = Query(...),
    limit: int = Query(default=200, ge=1, le=500),
):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    items = get_approved_unposted_invoices(db, organisation_id, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/extraction-failures")
def extraction_failures(
    auth: UserAuth,
    organisation_id: str = Query(...),
    limit: int = Query(default=100, ge=1, le=500),
):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    items = get_extraction_failures(db, organisation_id, limit=limit)
    return {"items": items, "total": len(items)}
