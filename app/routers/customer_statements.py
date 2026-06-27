from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.dependencies import UserAuth, ensure_org_read
from app.services.customer_statements import (
    build_customer_statement,
    export_customer_statement_csv,
    list_customer_statement_summaries,
)


router = APIRouter(prefix="/api/customer-statements", tags=["customer-statements"])


@router.get("")
def list_statements(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str = Query(default_factory=lambda: date.today().isoformat()),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return {
            "success": True,
            **list_customer_statement_summaries(
                db,
                organisation_id=organisation_id,
                as_at_date=as_at_date,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{customer_id}")
def get_statement(
    auth: UserAuth,
    customer_id: str,
    organisation_id: str,
    as_at_date: str = Query(default_factory=lambda: date.today().isoformat()),
    from_date: str | None = Query(default=None),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return {
            "success": True,
            "statement": build_customer_statement(
                db,
                organisation_id=organisation_id,
                customer_id=customer_id,
                as_at_date=as_at_date,
                from_date=from_date,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{customer_id}/export")
def export_statement(
    auth: UserAuth,
    customer_id: str,
    organisation_id: str,
    as_at_date: str = Query(default_factory=lambda: date.today().isoformat()),
    from_date: str | None = Query(default=None),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        filename, csv_bytes = export_customer_statement_csv(
            db,
            organisation_id=organisation_id,
            customer_id=customer_id,
            as_at_date=as_at_date,
            from_date=from_date,
        )
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
