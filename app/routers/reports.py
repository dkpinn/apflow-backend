from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response

from app.dependencies import UserAuth, ensure_org_read
from app.services.income_statement import generate_income_statement
from app.services.vat_report import (
    generate_vat_report,
    vat_report_csv,
    vat_report_text,
    vat_report_xlsx,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _ensure_reports_view(db, user_id: str, organisation_id: str) -> None:
    rows = (
        db.table("organisation_users")
        .select("role, permissions")
        .eq("organisation_id", organisation_id)
        .eq("user_id", user_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status_code=403, detail="You do not have access to this organisation")

    membership = rows[0]
    role = membership.get("role")
    permissions = membership.get("permissions") if isinstance(membership.get("permissions"), dict) else {}
    if role not in {"owner", "admin", "accountant"} and not permissions.get("reports_view"):
        raise HTTPException(status_code=403, detail="You do not have permission to view reports")


@router.get("/income-statement")
def income_statement_report(
    auth: UserAuth,
    organisation_id: str,
    date_from: str = Query(..., description="Start date in YYYY-MM-DD format."),
    date_to: str = Query(..., description="End date in YYYY-MM-DD format."),
    reporting_standard: Optional[str] = Query(default=None),
    presentation: Optional[str] = Query(default=None),
):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    try:
        return {
            "success": True,
            "report": generate_income_statement(
                db,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
                reporting_standard=reporting_standard,
                presentation=presentation,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/vat")
def vat_report(
    auth: UserAuth,
    organisation_id: str,
    date_from: str = Query(..., description="Start date in YYYY-MM-DD format."),
    date_to: str = Query(..., description="End date in YYYY-MM-DD format."),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        return {
            "success": True,
            "report": generate_vat_report(
                db,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/vat/export")
def export_vat_report(
    auth: UserAuth,
    organisation_id: str,
    date_from: str = Query(..., description="Start date in YYYY-MM-DD format."),
    date_to: str = Query(..., description="End date in YYYY-MM-DD format."),
    export_format: str = Query(..., alias="format", pattern="^(xlsx|csv|txt)$"),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        report = generate_vat_report(
            db,
            organisation_id=organisation_id,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename_base = f"vat-report-{date_from}-to-{date_to}"
    if export_format == "csv":
        content = vat_report_csv(report)
        media_type = "text/csv; charset=utf-8"
        extension = "csv"
    elif export_format == "txt":
        content = vat_report_text(report)
        media_type = "text/plain; charset=utf-8"
        extension = "txt"
    else:
        try:
            content = vat_report_xlsx(report)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        extension = "xlsx"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename_base}.{extension}"',
        },
    )
