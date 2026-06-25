"""Finance Leases API router — IFRS 16 sub-ledger.

Endpoints:
  GET    /api/finance-leases                           list leases for org
  POST   /api/finance-leases                           create lease + schedule + inception journal
  GET    /api/finance-leases/{id}                      get lease with schedule, documents, postings
  PATCH  /api/finance-leases/{id}                      update header (draft only)
  GET    /api/finance-leases/{id}/schedule             get amortization schedule rows
  PATCH  /api/finance-leases/{id}/schedule/{period}    update additional debit/credit on a row
  POST   /api/finance-leases/{id}/post-payment/{period} post payment + optional depreciation journals
  POST   /api/finance-leases/{id}/period-end           post ST/LT reclassification
  POST   /api/finance-leases/{id}/upload-document      upload PDF and trigger VLM extraction
"""

from __future__ import annotations

import json
import uuid as _uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.finance_lease_amortization import (
    build_amortization_schedule,
    compute_present_value,
    estimate_num_periods,
    validate_schedule_balances,
)
from app.services.finance_lease_gl import (
    assert_balanced,
    build_depreciation_lines,
    build_inception_lines,
    build_payment_lines,
    compute_monthly_depreciation,
)

router = APIRouter(prefix="/api/finance-leases", tags=["finance-leases"])

PaymentFrequency = Literal["monthly", "quarterly", "semi_annual", "annual"]


# ── Pydantic models ────────────────────────────────────────────────────────────

class LeaseAccountLinks(BaseModel):
    rou_asset_account_id:          str
    accum_depreciation_account_id: str
    depreciation_expense_account_id: str
    interest_expense_account_id:   str
    liability_lt_account_id:       str
    liability_st_account_id:       str


class CreateLeaseRequest(BaseModel):
    organisation_id:      str
    lessor_name:          str = Field(min_length=1, max_length=250)
    asset_description:    str = Field(min_length=1, max_length=500)
    reference_number:     Optional[str] = Field(default=None, max_length=100)
    commencement_date:    date
    end_date:             date
    payment_amount:       Decimal = Field(gt=0)
    payment_frequency:    PaymentFrequency = "monthly"
    annual_interest_rate: Decimal = Field(gt=0, lt=100)
    documentation_fees:   Decimal = Field(default=Decimal("0"), ge=0)
    initial_liability:    Optional[Decimal] = Field(default=None, gt=0)
    notes:                Optional[str] = Field(default=None, max_length=2000)
    accounts:             LeaseAccountLinks
    journal_date:         Optional[date] = None

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        start = info.data.get("commencement_date")
        if start and v <= start:
            raise ValueError("end_date must be after commencement_date")
        return v

    @field_validator("lessor_name", "asset_description")
    @classmethod
    def clean_text(cls, v: str) -> str:
        clean = " ".join(v.split())
        if not clean:
            raise ValueError("This field must not be blank")
        return clean


class UpdateLeaseRequest(BaseModel):
    organisation_id:    str
    lessor_name:        Optional[str] = Field(default=None, min_length=1, max_length=250)
    asset_description:  Optional[str] = Field(default=None, min_length=1, max_length=500)
    reference_number:   Optional[str] = Field(default=None, max_length=100)
    notes:              Optional[str] = Field(default=None, max_length=2000)
    accounts:           Optional[LeaseAccountLinks] = None


class PostPaymentRequest(BaseModel):
    organisation_id:    str
    journal_date:       date
    bank_account_id:    str
    post_depreciation:  bool = True
    description:        Optional[str] = None


class PeriodEndRequest(BaseModel):
    organisation_id: str
    period_end_date: date
    description:     Optional[str] = None


class ScheduleAdjustmentRequest(BaseModel):
    organisation_id:  str
    additional_debit: Decimal = Field(default=Decimal("0"), ge=0)
    additional_credit: Decimal = Field(default=Decimal("0"), ge=0)
    notes:            Optional[str] = Field(default=None, max_length=500)


class UploadDocumentRequest(BaseModel):
    organisation_id:   str
    original_filename: str
    mime_type:         str
    storage_bucket:    str = "finance-lease-docs"
    storage_path:      str
    file_size_bytes:   Optional[int] = None
    extract:           bool = True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _svc():
    return get_supabase_client()


def _auth(auth: UserAuth):
    user_id, _ = auth
    return str(user_id), _svc()


def _get_lease(db, *, lease_id: str, organisation_id: str) -> dict:
    res = (
        db.table("finance_leases")
        .select("*")
        .eq("id", lease_id)
        .eq("organisation_id", organisation_id)
        .eq("active", True)
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Finance lease not found")
    return res.data


def _validate_accounts(db, *, organisation_id: str, account_ids: list[str]) -> None:
    """Confirm all provided account UUIDs exist in this organisation."""
    unique = list({a for a in account_ids if a})
    if not unique:
        return
    res = (
        db.table("accounts")
        .select("id")
        .eq("organisation_id", organisation_id)
        .in_("id", unique)
        .execute()
    )
    found = {r["id"] for r in (res.data or [])}
    missing = [a for a in unique if a not in found]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"GL accounts not found in this organisation: {missing}",
        )


def _post_gl_journal(
    db,
    *,
    organisation_id: str,
    source_id: str,
    journal_date: date,
    description: str,
    lines: list[dict],
    created_by: str,
) -> str:
    """Insert a balanced GL journal and its lines; return the journal ID."""
    assert_balanced(lines)
    total = round(sum(float(l.get("debit_amount", 0)) for l in lines), 2)
    journal_id = str(_uuid.uuid4())

    db.table("gl_journals").insert({
        "id":             journal_id,
        "organisation_id": organisation_id,
        "source_type":    "finance_lease",
        "source_id":      source_id,
        "journal_date":   journal_date.isoformat(),
        "description":    description,
        "status":         "posted",
        "total_debit":    total,
        "total_credit":   total,
        "created_by":     created_by,
        "posted_by":      created_by,
        "posted_at":      datetime.utcnow().isoformat(),
    }).execute()

    line_rows = [
        {
            "organisation_id": organisation_id,
            "gl_journal_id":   journal_id,
            "account_id":      l["account_id"],
            "description":     l.get("description", ""),
            "debit_amount":    float(l.get("debit_amount", 0)),
            "credit_amount":   float(l.get("credit_amount", 0)),
            "tracking":        {},
            "sort_order":      l.get("sort_order", 0),
        }
        for l in lines
    ]
    db.table("gl_journal_lines").insert(line_rows).execute()
    return journal_id


def _log_posting_event(
    db,
    *,
    organisation_id: str,
    lease_id: str,
    journal_id: str,
    event_type: str,
    journal_date: date,
    period_number: int | None,
    description: str | None,
    amount: float,
    created_by: str,
) -> None:
    db.table("finance_lease_gl_postings").insert({
        "organisation_id": organisation_id,
        "lease_id":        lease_id,
        "journal_id":      journal_id,
        "event_type":      event_type,
        "period_number":   period_number,
        "journal_date":    journal_date.isoformat(),
        "description":     description,
        "amount":          amount,
        "created_by":      created_by,
    }).execute()


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("")
def list_finance_leases(
    organisation_id: str,
    auth: UserAuth,
    include_archived: bool = False,
    status: Optional[str] = None,
):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)

    query = (
        db.table("finance_leases")
        .select(
            "id, organisation_id, lessor_name, asset_description, reference_number, "
            "commencement_date, end_date, lease_term_months, "
            "payment_amount, payment_frequency, annual_interest_rate, "
            "initial_liability, documentation_fees, rou_asset_cost, "
            "current_liability_balance, current_rou_net_book_value, last_posted_period, "
            "status, inception_posted_at, active, created_at, updated_at"
        )
        .eq("organisation_id", organisation_id)
    )
    if not include_archived:
        query = query.eq("active", True)
    if status:
        query = query.eq("status", status)

    res = query.order("commencement_date", desc=True).execute()
    return {"success": True, "leases": res.data or []}


@router.post("", status_code=201)
def create_finance_lease(payload: CreateLeaseRequest, auth: UserAuth):
    """Create lease, build amortization schedule, and post inception GL journal."""
    user_id, db = _auth(auth)
    ensure_org_write(user_id, payload.organisation_id)

    # Validate all 6 GL accounts exist in this org
    acct = payload.accounts
    _validate_accounts(db, organisation_id=payload.organisation_id, account_ids=[
        acct.rou_asset_account_id,
        acct.accum_depreciation_account_id,
        acct.depreciation_expense_account_id,
        acct.interest_expense_account_id,
        acct.liability_lt_account_id,
        acct.liability_st_account_id,
    ])

    # Compute initial_liability via PV annuity if not provided
    if payload.initial_liability is not None:
        initial_liability = payload.initial_liability
    else:
        n = estimate_num_periods(
            commencement_date=payload.commencement_date,
            end_date=payload.end_date,
            frequency=payload.payment_frequency,
        )
        initial_liability = compute_present_value(
            payment_amount=payload.payment_amount,
            annual_rate=payload.annual_interest_rate,
            frequency=payload.payment_frequency,
            num_periods=n,
        )

    rou_asset_cost = initial_liability + payload.documentation_fees

    # Build amortization schedule
    try:
        schedule_rows = build_amortization_schedule(
            initial_liability=initial_liability,
            payment_amount=payload.payment_amount,
            annual_rate=payload.annual_interest_rate,
            frequency=payload.payment_frequency,
            commencement_date=payload.commencement_date,
            end_date=payload.end_date,
        )
        validate_schedule_balances(schedule_rows)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Insert lease header (status='draft')
    lease_id = str(_uuid.uuid4())
    lease_row: dict[str, Any] = {
        "id":                           lease_id,
        "organisation_id":              payload.organisation_id,
        "lessor_name":                  payload.lessor_name,
        "asset_description":            payload.asset_description,
        "reference_number":             payload.reference_number,
        "commencement_date":            payload.commencement_date.isoformat(),
        "end_date":                     payload.end_date.isoformat(),
        "payment_amount":               str(payload.payment_amount),
        "payment_frequency":            payload.payment_frequency,
        "annual_interest_rate":         str(payload.annual_interest_rate),
        "initial_liability":            str(initial_liability),
        "documentation_fees":           str(payload.documentation_fees),
        "rou_asset_cost":               str(rou_asset_cost),
        "rou_asset_account_id":         acct.rou_asset_account_id,
        "accum_depreciation_account_id": acct.accum_depreciation_account_id,
        "depreciation_expense_account_id": acct.depreciation_expense_account_id,
        "interest_expense_account_id":  acct.interest_expense_account_id,
        "liability_lt_account_id":      acct.liability_lt_account_id,
        "liability_st_account_id":      acct.liability_st_account_id,
        "notes":                        payload.notes,
        "status":                       "draft",
        "created_by":                   user_id,
        "updated_by":                   user_id,
    }
    db.table("finance_leases").insert(lease_row).execute()

    # Bulk-insert schedule rows
    schedule_inserts = [
        {
            "organisation_id":  payload.organisation_id,
            "lease_id":         lease_id,
            "period_number":    r.period_number,
            "period_date":      r.period_date.isoformat(),
            "opening_balance":  str(r.opening_balance),
            "payment_amount":   str(r.payment_amount),
            "interest_amount":  str(r.interest_amount),
            "principal_amount": str(r.principal_amount),
            "closing_balance":  str(r.closing_balance),
        }
        for r in schedule_rows
    ]
    db.table("finance_lease_schedule").insert(schedule_inserts).execute()

    # Build and post inception journal via SECURITY DEFINER RPC
    lease_dict = {**lease_row, "rou_asset_cost": str(rou_asset_cost)}
    inception_lines = build_inception_lines(lease_dict)
    j_date = (payload.journal_date or payload.commencement_date).isoformat()
    description = f"Finance Lease Inception – {payload.lessor_name} / {payload.asset_description}"

    try:
        rpc_result = db.rpc(
            "post_lease_inception_atomic",
            {
                "p_org_id":       payload.organisation_id,
                "p_lease_id":     lease_id,
                "p_user_id":      user_id,
                "p_journal_date": j_date,
                "p_description":  description,
                "p_lines":        json.dumps(inception_lines),
            },
        ).execute()
    except Exception as exc:
        # Roll back the inserted rows if the RPC fails
        db.table("finance_lease_schedule").delete().eq("lease_id", lease_id).execute()
        db.table("finance_leases").delete().eq("id", lease_id).execute()
        raise HTTPException(status_code=500, detail=f"Inception journal failed: {exc}")

    # Return the activated lease with schedule
    lease_res = db.table("finance_leases").select("*").eq("id", lease_id).single().execute()
    return {
        "success":  True,
        "lease":    lease_res.data,
        "schedule": [r.as_dict() for r in schedule_rows],
    }


@router.get("/{lease_id}")
def get_finance_lease(
    lease_id: str,
    organisation_id: str,
    auth: UserAuth,
):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)

    lease = _get_lease(db, lease_id=lease_id, organisation_id=organisation_id)

    schedule = (
        db.table("finance_lease_schedule")
        .select("*")
        .eq("lease_id", lease_id)
        .order("period_number")
        .execute()
    ).data or []

    documents = (
        db.table("finance_lease_documents")
        .select("*")
        .eq("lease_id", lease_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    postings = (
        db.table("finance_lease_gl_postings")
        .select("*")
        .eq("lease_id", lease_id)
        .order("journal_date", desc=True)
        .limit(50)
        .execute()
    ).data or []

    return {
        "success":  True,
        "lease":    lease,
        "schedule": schedule,
        "documents": documents,
        "postings": postings,
    }


@router.patch("/{lease_id}")
def update_finance_lease(
    lease_id: str,
    payload: UpdateLeaseRequest,
    auth: UserAuth,
):
    user_id, db = _auth(auth)
    ensure_org_write(user_id, payload.organisation_id)

    lease = _get_lease(db, lease_id=lease_id, organisation_id=payload.organisation_id)
    if lease["status"] != "draft":
        raise HTTPException(
            status_code=409,
            detail="Only draft leases can be edited after creation",
        )

    updates: dict[str, Any] = {"updated_by": user_id}
    if payload.lessor_name is not None:
        updates["lessor_name"] = payload.lessor_name
    if payload.asset_description is not None:
        updates["asset_description"] = payload.asset_description
    if payload.reference_number is not None:
        updates["reference_number"] = payload.reference_number
    if payload.notes is not None:
        updates["notes"] = payload.notes
    if payload.accounts is not None:
        acct = payload.accounts
        _validate_accounts(db, organisation_id=payload.organisation_id, account_ids=[
            acct.rou_asset_account_id, acct.accum_depreciation_account_id,
            acct.depreciation_expense_account_id, acct.interest_expense_account_id,
            acct.liability_lt_account_id, acct.liability_st_account_id,
        ])
        updates.update({
            "rou_asset_account_id":            acct.rou_asset_account_id,
            "accum_depreciation_account_id":   acct.accum_depreciation_account_id,
            "depreciation_expense_account_id": acct.depreciation_expense_account_id,
            "interest_expense_account_id":     acct.interest_expense_account_id,
            "liability_lt_account_id":         acct.liability_lt_account_id,
            "liability_st_account_id":         acct.liability_st_account_id,
        })

    if len(updates) == 1:
        return {"success": True, "lease": lease}

    res = (
        db.table("finance_leases")
        .update(updates)
        .eq("id", lease_id)
        .eq("organisation_id", payload.organisation_id)
        .execute()
    )
    return {"success": True, "lease": res.data[0] if res.data else lease}


@router.get("/{lease_id}/schedule")
def get_lease_schedule(
    lease_id: str,
    organisation_id: str,
    auth: UserAuth,
):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    _get_lease(db, lease_id=lease_id, organisation_id=organisation_id)

    res = (
        db.table("finance_lease_schedule")
        .select("*")
        .eq("lease_id", lease_id)
        .order("period_number")
        .execute()
    )
    return {"success": True, "schedule": res.data or []}


@router.patch("/{lease_id}/schedule/{period_number}")
def update_schedule_row(
    lease_id: str,
    period_number: int,
    payload: ScheduleAdjustmentRequest,
    auth: UserAuth,
):
    user_id, db = _auth(auth)
    ensure_org_write(user_id, payload.organisation_id)

    lease = _get_lease(db, lease_id=lease_id, organisation_id=payload.organisation_id)
    if lease["status"] not in ("draft", "active"):
        raise HTTPException(status_code=409, detail="Cannot edit schedule rows on a completed lease")

    row_res = (
        db.table("finance_lease_schedule")
        .select("*")
        .eq("lease_id", lease_id)
        .eq("period_number", period_number)
        .single()
        .execute()
    )
    if not row_res.data:
        raise HTTPException(status_code=404, detail=f"Period {period_number} not found")
    if row_res.data["posted"]:
        raise HTTPException(status_code=409, detail="Cannot edit a period that has already been posted")

    updated = (
        db.table("finance_lease_schedule")
        .update({
            "additional_debit":  str(payload.additional_debit),
            "additional_credit": str(payload.additional_credit),
            "notes":             payload.notes,
        })
        .eq("lease_id", lease_id)
        .eq("period_number", period_number)
        .execute()
    )
    return {"success": True, "row": updated.data[0] if updated.data else row_res.data}


@router.post("/{lease_id}/post-payment/{period_number}")
def post_payment_journal(
    lease_id: str,
    period_number: int,
    payload: PostPaymentRequest,
    auth: UserAuth,
):
    """Post the monthly payment journal (and optionally the depreciation journal)."""
    user_id, db = _auth(auth)
    ensure_org_write(user_id, payload.organisation_id)

    lease = _get_lease(db, lease_id=lease_id, organisation_id=payload.organisation_id)
    if lease["status"] != "active":
        raise HTTPException(status_code=409, detail="Only active leases can have payment journals posted")

    row_res = (
        db.table("finance_lease_schedule")
        .select("*")
        .eq("lease_id", lease_id)
        .eq("period_number", period_number)
        .single()
        .execute()
    )
    if not row_res.data:
        raise HTTPException(status_code=404, detail=f"Period {period_number} not found")
    schedule_row = row_res.data
    if schedule_row["posted"]:
        raise HTTPException(status_code=409, detail=f"Period {period_number} has already been posted")

    _validate_accounts(db, organisation_id=payload.organisation_id,
                       account_ids=[payload.bank_account_id])

    # Build payment journal lines
    payment_desc = (
        payload.description
        or f"Finance Lease Payment – Period {period_number} – {lease['asset_description']}"
    )
    payment_lines = build_payment_lines(lease, schedule_row, payload.bank_account_id,
                                        description=payment_desc)

    payment_journal_id = _post_gl_journal(
        db,
        organisation_id=payload.organisation_id,
        source_id=lease_id,
        journal_date=payload.journal_date,
        description=payment_desc,
        lines=payment_lines,
        created_by=user_id,
    )

    principal = float(schedule_row["principal_amount"])
    interest  = float(schedule_row["interest_amount"])

    _log_posting_event(db,
        organisation_id=payload.organisation_id,
        lease_id=lease_id,
        journal_id=payment_journal_id,
        event_type="payment",
        journal_date=payload.journal_date,
        period_number=period_number,
        description=payment_desc,
        amount=principal + interest,
        created_by=user_id,
    )

    # Mark schedule row as posted
    db.table("finance_lease_schedule").update({
        "posted":     True,
        "journal_id": payment_journal_id,
        "posted_at":  datetime.utcnow().isoformat(),
        "posted_by":  user_id,
    }).eq("lease_id", lease_id).eq("period_number", period_number).execute()

    # Update lease running balances
    new_liability = float(lease["current_liability_balance"]) - principal
    updates: dict[str, Any] = {
        "current_liability_balance": round(new_liability, 2),
        "last_posted_period":        period_number,
        "updated_by":                user_id,
    }

    depreciation_journal_id: str | None = None
    if payload.post_depreciation:
        monthly_dep = compute_monthly_depreciation(lease)
        dep_desc = f"ROU Asset Depreciation – Period {period_number} – {lease['asset_description']}"
        dep_lines = build_depreciation_lines(lease, monthly_depreciation=monthly_dep, description=dep_desc)

        depreciation_journal_id = _post_gl_journal(
            db,
            organisation_id=payload.organisation_id,
            source_id=lease_id,
            journal_date=payload.journal_date,
            description=dep_desc,
            lines=dep_lines,
            created_by=user_id,
        )
        _log_posting_event(db,
            organisation_id=payload.organisation_id,
            lease_id=lease_id,
            journal_id=depreciation_journal_id,
            event_type="depreciation",
            journal_date=payload.journal_date,
            period_number=period_number,
            description=dep_desc,
            amount=monthly_dep,
            created_by=user_id,
        )
        new_nbv = float(lease["current_rou_net_book_value"]) - monthly_dep
        updates["current_rou_net_book_value"] = round(new_nbv, 2)

    # Check if all periods are now posted → mark lease complete
    remaining = (
        db.table("finance_lease_schedule")
        .select("id", count="exact")
        .eq("lease_id", lease_id)
        .eq("posted", False)
        .execute()
    )
    if (remaining.count or 0) == 0:
        updates["status"] = "completed"

    db.table("finance_leases").update(updates).eq("id", lease_id).execute()

    return {
        "success":                 True,
        "payment_journal_id":      payment_journal_id,
        "depreciation_journal_id": depreciation_journal_id,
        "period_number":           period_number,
    }


@router.post("/{lease_id}/period-end")
def post_period_end_reclassification(
    lease_id: str,
    payload: PeriodEndRequest,
    auth: UserAuth,
):
    """Reclassify the next 12 months of principal from long-term to short-term liability."""
    user_id, db = _auth(auth)
    ensure_org_write(user_id, payload.organisation_id)

    _get_lease(db, lease_id=lease_id, organisation_id=payload.organisation_id)

    try:
        result = db.rpc(
            "post_lease_period_end_reclassification_atomic",
            {
                "p_org_id":          payload.organisation_id,
                "p_lease_id":        lease_id,
                "p_user_id":         user_id,
                "p_period_end_date": payload.period_end_date.isoformat(),
                "p_description":     payload.description,
            },
        ).execute()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"success": True, **result.data}


@router.post("/{lease_id}/upload-document")
def upload_lease_document(
    lease_id: str,
    payload: UploadDocumentRequest,
    auth: UserAuth,
):
    """Record an uploaded document and (optionally) run VLM extraction."""
    user_id, db = _auth(auth)
    ensure_org_write(user_id, payload.organisation_id)

    # Confirm lease exists (the lease may not yet be saved if doc uploaded first)
    if lease_id != "new":
        _get_lease(db, lease_id=lease_id, organisation_id=payload.organisation_id)

    doc_id = str(_uuid.uuid4())
    doc_row: dict[str, Any] = {
        "id":                doc_id,
        "organisation_id":   payload.organisation_id,
        "lease_id":          None if lease_id == "new" else lease_id,
        "original_filename": payload.original_filename,
        "mime_type":         payload.mime_type,
        "storage_bucket":    payload.storage_bucket,
        "storage_path":      payload.storage_path,
        "file_size_bytes":   payload.file_size_bytes,
        "extraction_status": "uploaded",
        "uploaded_by":       user_id,
    }
    db.table("finance_lease_documents").insert(doc_row).execute()

    extracted_data: dict | None = None
    if payload.extract:
        db.table("finance_lease_documents").update(
            {"extraction_status": "processing"}
        ).eq("id", doc_id).execute()

        try:
            from app.services.finance_lease_extraction import extract_lease_document

            # Download file bytes from Supabase storage
            file_res = db.storage.from_(payload.storage_bucket).download(payload.storage_path)
            file_bytes = file_res if isinstance(file_res, bytes) else bytes(file_res)

            extracted_data = extract_lease_document(file_bytes, mime_type=payload.mime_type)
            model_used = extracted_data.pop("_provider", None)

            db.table("finance_lease_documents").update({
                "extraction_status": "completed",
                "extracted_data":    extracted_data,
                "extraction_model":  model_used,
            }).eq("id", doc_id).execute()

        except Exception as exc:
            db.table("finance_lease_documents").update({
                "extraction_status": "failed",
                "extraction_error":  str(exc)[:1000],
            }).eq("id", doc_id).execute()
            # Do not raise — return partial result with error indicator
            return {
                "success":        False,
                "document_id":    doc_id,
                "extracted_data": None,
                "error":          str(exc),
            }

    return {
        "success":        True,
        "document_id":    doc_id,
        "extracted_data": extracted_data,
    }
