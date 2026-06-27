from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response

from app.dependencies import UserAuth
from app.services.aged_payables import generate_aged_payables
from app.services.aged_receivables import generate_aged_receivables
from app.services.balance_sheet import generate_balance_sheet
from app.services.cash_flow import generate_cash_flow
from app.services.cash_flow_forecast import generate_cash_flow_forecast
from app.services.general_ledger import generate_general_ledger
from app.services.income_statement import generate_income_statement
from app.services.transaction_report import (
    generate_transaction_report,
    transaction_report_csv,
    transaction_report_text,
    transaction_report_xlsx,
)
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


@router.get("/transactions")
def transaction_report(
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
            "report": generate_transaction_report(
                db,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/transactions/export")
def export_transaction_report(
    auth: UserAuth,
    organisation_id: str,
    date_from: str = Query(..., description="Start date in YYYY-MM-DD format."),
    date_to: str = Query(..., description="End date in YYYY-MM-DD format."),
    export_format: str = Query(..., alias="format", pattern="^(xlsx|csv|txt)$"),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        report = generate_transaction_report(
            db,
            organisation_id=organisation_id,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename_base = f"transactions-{date_from}-to-{date_to}"
    if export_format == "csv":
        content = transaction_report_csv(report)
        media_type = "text/csv; charset=utf-8"
        extension = "csv"
    elif export_format == "txt":
        content = transaction_report_text(report)
        media_type = "text/plain; charset=utf-8"
        extension = "txt"
    else:
        try:
            content = transaction_report_xlsx(report)
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
    _ensure_reports_view(db, user_id, organisation_id)
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


@router.get("/balance-sheet")
def balance_sheet_report(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str = Query(..., description="Snapshot date in YYYY-MM-DD format."),
    financial_year_end: Optional[str] = Query(default=None, description="Override the organisation's financial year-end month."),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        return {
            "success": True,
            "report": generate_balance_sheet(
                db,
                organisation_id=organisation_id,
                as_at_date=as_at_date,
                financial_year_end=financial_year_end,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/general-ledger")
def general_ledger_report(
    auth: UserAuth,
    organisation_id: str,
    date_from: str = Query(..., description="Start date in YYYY-MM-DD format."),
    date_to: str = Query(..., description="End date in YYYY-MM-DD format."),
    account_id: Optional[str] = Query(default=None),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        return {
            "success": True,
            "report": generate_general_ledger(
                db,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
                account_id=account_id,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/aged-payables")
def aged_payables_report(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str = Query(..., description="Snapshot date in YYYY-MM-DD format."),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        return {
            "success": True,
            "report": generate_aged_payables(
                db,
                organisation_id=organisation_id,
                as_at_date=as_at_date,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/aged-receivables")
def aged_receivables_report(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str = Query(..., description="Snapshot date in YYYY-MM-DD format."),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        return {
            "success": True,
            "report": generate_aged_receivables(
                db,
                organisation_id=organisation_id,
                as_at_date=as_at_date,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cash-flow")
def cash_flow_report(
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
            "report": generate_cash_flow(
                db,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cash-flow-forecast")
def cash_flow_forecast_report(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str = Query(default_factory=lambda: __import__("datetime").date.today().isoformat()),
    forecast_days: int = Query(default=90, ge=7, le=365),
):
    user_id, db = auth
    _ensure_reports_view(db, user_id, organisation_id)
    try:
        return {
            "success": True,
            "report": generate_cash_flow_forecast(
                db,
                organisation_id=organisation_id,
                as_at_date=as_at_date,
                forecast_days=forecast_days,
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
