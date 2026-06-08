from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response

from app.dependencies import UserAuth, ensure_org_read
from app.services.income_statement import generate_income_statement
from app.services.trial_balance import (
    generate_trial_balance,
    trial_balance_csv,
    trial_balance_text,
    trial_balance_xlsx,
)
from app.services.vat_report import (
    generate_vat_report,
    vat_report_csv,
    vat_report_text,
    vat_report_xlsx,
)


def _parse_compare_years(raw: Optional[str]) -> Optional[list[int]]:
    if not raw or not raw.strip():
        return None
    years: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            years.append(int(chunk))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="compare_years must be a comma-separated list of years") from exc
    return years

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


@router.get("/trial-balance")
def trial_balance_report(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str = Query(..., description="Snapshot date in YYYY-MM-DD format."),
    financial_year_end: Optional[str] = Query(default=None, description="Override the organisation's financial year-end month."),
    tracking_dimension_id: Optional[str] = Query(default=None),
    compare_years: Optional[str] = Query(default=None, description="Comma-separated list of years to compare against."),
    include_budget: bool = Query(default=False),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        return {
            "success": True,
            "report": generate_trial_balance(
                db,
                organisation_id=organisation_id,
                as_at_date=as_at_date,
                financial_year_end=financial_year_end,
                tracking_dimension_id=tracking_dimension_id,
                compare_years=_parse_compare_years(compare_years),
                include_budget=include_budget,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/trial-balance/export")
def export_trial_balance_report(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str = Query(..., description="Snapshot date in YYYY-MM-DD format."),
    financial_year_end: Optional[str] = Query(default=None),
    tracking_dimension_id: Optional[str] = Query(default=None),
    compare_years: Optional[str] = Query(default=None),
    include_budget: bool = Query(default=False),
    export_format: str = Query(..., alias="format", pattern="^(xlsx|csv|txt)$"),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        report = generate_trial_balance(
            db,
            organisation_id=organisation_id,
            as_at_date=as_at_date,
            financial_year_end=financial_year_end,
            tracking_dimension_id=tracking_dimension_id,
            compare_years=_parse_compare_years(compare_years),
            include_budget=include_budget,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename_base = f"trial-balance-{as_at_date}"
    if export_format == "csv":
        content = trial_balance_csv(report)
        media_type = "text/csv; charset=utf-8"
        extension = "csv"
    elif export_format == "txt":
        content = trial_balance_text(report)
        media_type = "text/plain; charset=utf-8"
        extension = "txt"
    else:
        try:
            content = trial_balance_xlsx(report)
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
