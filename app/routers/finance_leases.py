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
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.accounting_locks import assert_accounting_period_unlocked
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
FINANCE_LEASE_DOCUMENT_BUCKET = "finance-lease-docs"


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
    storage_bucket:    str = FINANCE_LEASE_DOCUMENT_BUCKET
    storage_path:      str
    file_size_bytes:   Optional[int] = None
    extract:           bool = True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _svc():
    return get_supabase_client()


def _validated_document_storage_path(
    *,
    organisation_id: str,
    storage_bucket: str,
    storage_path: str,
) -> str:
    """Validate the client-uploaded object reference before service-role access."""
    if storage_bucket != FINANCE_LEASE_DOCUMENT_BUCKET:
        raise HTTPException(
            status_code=422,
            detail=f"Finance lease documents must use the {FINANCE_LEASE_DOCUMENT_BUCKET} bucket",
        )

    path = storage_path.strip()
    decoded_path = unquote(path)
    parts = decoded_path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in decoded_path
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] != organisation_id
    ):
        raise HTTPException(
            status_code=422,
            detail="Finance lease document path must be an organisation-owned object path",
        )
    return path


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
        assert_accounting_period_unlocked(
            db,
            organisation_id=payload.organisation_id,
            transaction_date=j_date,
            action="Post finance lease inception",
        )
    except ValueError as exc:
        db.table("finance_lease_schedule").delete().eq("lease_id", lease_id).execute()
        db.table("finance_leases").delete().eq("id", lease_id).execute()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    """Atomically post a payment and its optional depreciation journal."""
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
    depreciation_lines: list[dict] = []
    depreciation_amount = 0.0
    depreciation_desc: str | None = None
    if payload.post_depreciation:
        depreciation_amount = compute_monthly_depreciation(lease)
        depreciation_desc = (
            f"ROU Asset Depreciation – Period {period_number} – {lease['asset_description']}"
        )
        depreciation_lines = build_depreciation_lines(
            lease,
            monthly_depreciation=depreciation_amount,
            description=depreciation_desc,
        )

    try:
        assert_balanced(payment_lines)
        if depreciation_lines:
            assert_balanced(depreciation_lines)
        assert_accounting_period_unlocked(
            db,
            organisation_id=payload.organisation_id,
            transaction_date=payload.journal_date,
            action="Post finance lease payment",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        rpc_result = db.rpc(
            "post_lease_payment_atomic",
            {
                "p_org_id": payload.organisation_id,
                "p_lease_id": lease_id,
                "p_period_number": period_number,
                "p_user_id": user_id,
                "p_journal_date": payload.journal_date.isoformat(),
                "p_payment_description": payment_desc,
                "p_payment_lines": json.dumps(payment_lines),
                "p_post_depreciation": payload.post_depreciation,
                "p_depreciation_description": depreciation_desc,
                "p_depreciation_amount": depreciation_amount,
                "p_depreciation_lines": json.dumps(depreciation_lines),
            },
        ).execute()
    except Exception as exc:
        message = str(exc)
        status_code = 409 if "already been posted" in message.lower() else 400
        raise HTTPException(status_code=status_code, detail=message) from exc

    result = rpc_result.data or {}

    return {
        "success": True,
        "payment_journal_id": result.get("payment_journal_id"),
        "depreciation_journal_id": result.get("depreciation_journal_id"),
        "period_number": result.get("period_number", period_number),
        "lease_status": result.get("lease_status"),
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
        assert_accounting_period_unlocked(
            db,
            organisation_id=payload.organisation_id,
            transaction_date=payload.period_end_date,
            action="Post finance lease period-end reclassification",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    storage_path = _validated_document_storage_path(
        organisation_id=payload.organisation_id,
        storage_bucket=payload.storage_bucket,
        storage_path=payload.storage_path,
    )

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
        "storage_bucket":    FINANCE_LEASE_DOCUMENT_BUCKET,
        "storage_path":      storage_path,
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
            file_res = db.storage.from_(FINANCE_LEASE_DOCUMENT_BUCKET).download(storage_path)
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
