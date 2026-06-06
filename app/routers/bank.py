from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.extraction_foundation import file_sha256
from app.services.bank_statement_service import (
    default_rule_criteria_from_line,
    dec_to_float,
    detect_line_duplicates,
    extract_statement,
    journal_lines_for_bank_transaction,
    line_to_insert,
    money,
    new_uuid,
    normalize_rule_criteria,
    reversal_lines_for_journal,
    score_invoice_suggestions,
    score_rule_suggestions,
    validate_balances,
)
from app.services.organisation_module_settings import (
    required_tracking_dimensions,
    validate_bank_allocation_tracking,
)

router = APIRouter(prefix="/api/bank", tags=["bank"])

_BANK_COST_PER_MILLION: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.5-pro":   {"input": 1.25, "output": 10.00},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gpt-4o":           {"input": 5.00, "output": 15.00},
    "gpt-4.1-mini":     {"input": 0.40, "output": 1.60},
}

def _calc_bank_cost(model: str | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    if not model or input_tokens is None:
        return None
    rates = next((r for k, r in _BANK_COST_PER_MILLION.items() if (model or "").startswith(k)), None)
    if not rates:
        return None
    return round((input_tokens or 0) * rates["input"] / 1_000_000 + (output_tokens or 0) * rates.get("output", 0) / 1_000_000, 8)


class BankAccountCreate(BaseModel):
    organisation_id: UUID
    name: str
    institution_name: Optional[str] = None
    account_type: str = "bank"
    currency: str = "ZAR"
    account_number_mask: Optional[str] = None
    account_number_hash: Optional[str] = None
    gl_account_id: Optional[UUID] = None
    opening_balance: float = 0


class BankUploadCreate(BaseModel):
    organisation_id: UUID
    bank_account_id: UUID
    original_filename: str
    mime_type: Optional[str] = None
    storage_bucket: str = "statement-files"
    storage_path: str


class ExtractUploadRequest(BaseModel):
    organisation_id: UUID


class ReviewLineRequest(BaseModel):
    organisation_id: UUID
    suggestion_id: Optional[UUID] = None
    gl_account_id: Optional[UUID] = None
    tracking: dict[str, Any] = Field(default_factory=dict)
    tax_treatment: Optional[str] = None
    supplier_id: Optional[UUID] = None
    create_rule: bool = False
    rule_name: Optional[str] = None
    rule_criteria: list[dict[str, Any]] = Field(default_factory=list)
    criteria_mode: str = "and"


class DraftJournalRequest(BaseModel):
    organisation_id: UUID
    gl_account_id: UUID
    tracking: dict[str, Any] = Field(default_factory=dict)
    vat_rate: Optional[float] = None
    vat_account_id: Optional[UUID] = None


class PostJournalRequest(BaseModel):
    organisation_id: UUID


def svc():
    try:
        return get_supabase_client()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Supabase credentials missing") from exc


def _auth(auth: UserAuth) -> tuple[str, Any]:
    user_id, _user_db = auth
    return str(user_id), svc()


def _one(res, detail: str):
    if not res.data:
        raise HTTPException(status_code=404, detail=detail)
    return res.data[0] if isinstance(res.data, list) else res.data


def log_bank_event(db, *, organisation_id: str, event_type: str, actor_user_id: str, **details: Any) -> None:
    payload = {
        "organisation_id": organisation_id,
        "event_type": event_type,
        "actor_user_id": actor_user_id,
        "actor_type": "user",
        "bank_account_id": details.pop("bank_account_id", None),
        "bank_statement_upload_id": details.pop("bank_statement_upload_id", None),
        "bank_statement_line_id": details.pop("bank_statement_line_id", None),
        "gl_journal_id": details.pop("gl_journal_id", None),
        "details": details,
    }
    try:
        db.table("bank_audit_events").insert(payload).execute()
    except Exception as exc:  # pragma: no cover
        print("BANK AUDIT INSERT FAILED:", str(exc), payload)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def account_labels(db, organisation_id: str, account_ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = [account_id for account_id in dict.fromkeys(account_ids) if account_id]
    if not ids:
        return {}
    try:
        rows = (
            db.table("accounts")
            .select("id, code, name")
            .eq("organisation_id", organisation_id)
            .in_("id", ids)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    return {str(row["id"]): row for row in rows if row.get("id")}


def journal_preview_lines(db, organisation_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = account_labels(db, organisation_id, [str(row.get("account_id") or "") for row in rows])
    preview: list[dict[str, Any]] = []
    for row in rows:
        account_id = str(row.get("account_id") or "")
        account = labels.get(account_id) or {}
        preview.append({
            **row,
            "account_code": account.get("code"),
            "account_name": account.get("name") or account_id,
        })
    return preview


def build_journal_rows_for_line(
    db,
    *,
    organisation_id: str,
    line: dict[str, Any],
    gl_account_id: str,
    tracking: dict[str, Any],
    vat_rate: Optional[float] = None,
    vat_account_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    try:
        validate_bank_allocation_tracking(
            tracking=tracking,
            required_dimensions=required_tracking_dimensions(
                db,
                organisation_id=organisation_id,
                module_key="bank_cash",
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    account = _one(
        db.table("bank_accounts").select("*").eq("id", line["bank_account_id"]).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank account not found",
    )
    bank_gl = account.get("gl_account_id")
    if not bank_gl:
        raise HTTPException(status_code=400, detail="Bank account needs a linked GL account before posting")
    rows = journal_lines_for_bank_transaction(
        organisation_id=organisation_id,
        bank_account_gl_id=str(bank_gl),
        allocation_account_id=str(gl_account_id),
        amount=money(line.get("signed_amount")),
        description=line.get("description") or "Bank transaction",
        tracking=tracking,
    )
    if vat_rate and vat_account_id and len(rows) >= 2:
        # rows[0] = allocation/expense side (SPLIT into net + VAT)
        # rows[1] = bank GL side (UNCHANGED — stays at full amount)
        alloc = rows[0]
        total_alloc = money(alloc.get("debit_amount") or alloc.get("credit_amount"))
        vat_amount = (total_alloc * Decimal(str(vat_rate)) / (100 + Decimal(str(vat_rate)))).quantize(Decimal("0.01"))
        net_amount = total_alloc - vat_amount
        if alloc.get("debit_amount"):
            rows[0] = {**alloc, "debit_amount": dec_to_float(net_amount), "credit_amount": 0}
            vat_line = {**alloc, "account_id": vat_account_id, "debit_amount": dec_to_float(vat_amount), "credit_amount": 0, "tracking": {}, "sort_order": 2}
        else:
            rows[0] = {**alloc, "credit_amount": dec_to_float(net_amount), "debit_amount": 0}
            vat_line = {**alloc, "account_id": vat_account_id, "credit_amount": dec_to_float(vat_amount), "debit_amount": 0, "tracking": {}, "sort_order": 2}
        rows.append(vat_line)
    return rows


def is_unreconciled_bank_line(line: dict[str, Any]) -> bool:
    posting_status = str(line.get("posting_status") or "unposted").lower()
    allocation_status = str(line.get("allocation_status") or "unallocated").lower()
    review_status = str(line.get("review_status") or "pending").lower()
    return posting_status != "posted" or allocation_status != "allocated" or review_status != "reviewed"


def bank_line_sort_key(line: dict[str, Any]) -> tuple[str, int, str]:
    row_index = line.get("source_row_index")
    try:
        row_order = int(row_index) if row_index is not None else 999999
    except (TypeError, ValueError):
        row_order = 999999
    return (str(line.get("line_date") or ""), row_order, str(line.get("id") or ""))


@router.get("/accounts")
def list_bank_accounts(organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    res = (
        db.table("bank_accounts")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("name")
        .execute()
    )
    return {"success": True, "accounts": res.data or []}


@router.get("/accounts/{account_id}/unreconciled-lines")
def list_bank_account_unreconciled_lines(account_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    account = _one(
        db.table("bank_accounts").select("*").eq("id", account_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank account not found",
    )
    rows = (
        db.table("bank_statement_lines")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_account_id", account_id)
        .limit(5000)
        .execute()
        .data
        or []
    )
    lines = sorted((row for row in rows if is_unreconciled_bank_line(row)), key=bank_line_sort_key)

    upload_ids = [
        str(line.get("bank_statement_upload_id"))
        for line in lines
        if line.get("bank_statement_upload_id")
    ]
    uploads_by_id: dict[str, dict[str, Any]] = {}
    if upload_ids:
        try:
            upload_rows = (
                db.table("bank_statement_uploads")
                .select("id, original_filename, uploaded_at")
                .eq("organisation_id", organisation_id)
                .in_("id", list(dict.fromkeys(upload_ids)))
                .execute()
                .data
                or []
            )
            uploads_by_id = {str(row.get("id")): row for row in upload_rows if row.get("id")}
        except Exception:
            uploads_by_id = {}

    enriched = []
    for line in lines:
        upload = uploads_by_id.get(str(line.get("bank_statement_upload_id"))) or {}
        enriched.append({
            **line,
            "upload_original_filename": upload.get("original_filename"),
            "upload_uploaded_at": upload.get("uploaded_at"),
        })

    return {"success": True, "account": account, "lines": enriched}


@router.post("/accounts")
def create_bank_account(payload: BankAccountCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    opening = float(money(payload.opening_balance))
    row = {
        "organisation_id": organisation_id,
        "name": payload.name,
        "institution_name": payload.institution_name,
        "account_type": payload.account_type,
        "currency": payload.currency,
        "account_number_mask": payload.account_number_mask,
        "account_number_hash": payload.account_number_hash,
        "gl_account_id": str(payload.gl_account_id) if payload.gl_account_id else None,
        "opening_balance": opening,
        "current_reconciled_balance": opening,
        "active": True,
    }
    res = db.table("bank_accounts").insert(row).execute()
    account = _one(res, "Bank account create failed")
    log_bank_event(db, organisation_id=organisation_id, event_type="bank_account_created", actor_user_id=user_id, bank_account_id=account["id"])
    try:
        db.table("suppliers").insert({
            "organisation_id": organisation_id,
            "supplier_name": payload.institution_name or payload.name,
            "bank_name": payload.institution_name,
            "active": True,
            "line_items_include_vat": True,
        }).execute()
    except Exception:
        pass  # non-fatal
    return {"success": True, "account": account}


@router.get("/uploads")
def list_bank_uploads(auth: UserAuth, organisation_id: str, bank_account_id: Optional[str] = None):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    q = db.table("bank_statement_uploads").select("*").eq("organisation_id", organisation_id)
    if bank_account_id:
        q = q.eq("bank_account_id", bank_account_id)
    res = q.order("uploaded_at", desc=True).limit(200).execute()
    return {"success": True, "uploads": res.data or []}


@router.post("/uploads")
def create_bank_upload(payload: BankUploadCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    account = _one(
        db.table("bank_accounts").select("*").eq("id", str(payload.bank_account_id)).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank account not found",
    )
    row = {
        "organisation_id": organisation_id,
        "bank_account_id": account["id"],
        "original_filename": payload.original_filename,
        "mime_type": payload.mime_type,
        "storage_bucket": payload.storage_bucket,
        "storage_path": payload.storage_path,
        "source_type": "upload",
        "extraction_status": "uploaded",
        "uploaded_by": user_id,
    }
    res = db.table("bank_statement_uploads").insert(row).execute()
    upload = _one(res, "Bank statement upload create failed")
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_statement_uploaded",
        actor_user_id=user_id,
        bank_account_id=account["id"],
        bank_statement_upload_id=upload["id"],
        original_filename=payload.original_filename,
    )
    return {"success": True, "upload": upload}


@router.post("/uploads/{upload_id}/extract")
def extract_bank_upload(upload_id: str, payload: ExtractUploadRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)

    upload = _one(
        db.table("bank_statement_uploads").select("*").eq("id", upload_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement upload not found",
    )
    account = _one(
        db.table("bank_accounts").select("*").eq("id", upload["bank_account_id"]).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank account not found",
    )
    db.table("bank_statement_uploads").update({"extraction_status": "processing"}).eq("id", upload_id).execute()
    try:
        file_bytes = db.storage.from_(upload.get("storage_bucket") or "statement-files").download(upload["storage_path"])
        file_hash = file_sha256(file_bytes)
        duplicate_file = (
            db.table("bank_statement_uploads")
            .select("id")
            .eq("organisation_id", organisation_id)
            .eq("bank_account_id", account["id"])
            .eq("file_sha256", file_hash)
            .neq("id", upload_id)
            .limit(1)
            .execute()
            .data
            or []
        )

        header, lines = extract_statement(
            file_bytes,
            filename=upload["original_filename"],
            mime_type=upload.get("mime_type") or "application/octet-stream",
            bank_account_id=account["id"],
            currency=account.get("currency"),
            account_type=account.get("account_type"),
        )
        line_wrappers, duplicate_summary = detect_line_duplicates(
            db=db,
            organisation_id=organisation_id,
            bank_account_id=account["id"],
            lines=lines,
        )
        if duplicate_file:
            duplicate_summary["duplicate_status"] = "duplicate_file"
        balance_summary = validate_balances(
            account_current_balance=money(account.get("current_reconciled_balance")),
            header=header,
            lines=lines,
        )

        db.table("bank_statement_lines").delete().eq("bank_statement_upload_id", upload_id).execute()
        inserts = [
            line_to_insert(
                wrapper["line"],
                organisation_id=organisation_id,
                bank_account_id=account["id"],
                upload_id=upload_id,
                duplicate_status=wrapper["duplicate_status"],
            )
            for wrapper in line_wrappers
        ]
        if inserts:
            db.table("bank_statement_lines").insert(inserts).execute()

        closing = header.get("closing_balance")
        upload_patch = {
            "file_sha256": file_hash,
            "statement_period_from": header.get("statement_period_from"),
            "statement_period_to": header.get("statement_period_to"),
            "opening_balance": header.get("opening_balance"),
            "closing_balance": closing,
            "extracted_line_count": len(inserts),
            "duplicate_line_count": duplicate_summary.get("duplicate_line_count", 0),
            "duplicate_status": duplicate_summary.get("duplicate_status", "clear"),
            "balance_status": balance_summary["balance_status"],
            "confidence_score": header.get("confidence_score"),
            "duplicate_summary": {**duplicate_summary, **balance_summary},
            "extractor_type": header.get("extractor_type") or header.get("extractor") or "bank_statement",
            "extractor_version": header.get("extractor_version") or "v1",
            "source_format": header.get("source_format"),
            "raw_extraction": header.get("raw_extraction") or {},
            "extraction_warnings": header.get("extraction_warnings") or [],
            "extraction_evidence": {
                "extractor": header.get("extractor"),
                "extractor_type": header.get("extractor_type"),
                "extractor_version": header.get("extractor_version"),
                "source_format": header.get("source_format"),
                "parser_strategy": header.get("parser_strategy"),
                "line_count": len(inserts),
                "warnings": header.get("extraction_warnings") or [],
            },
            "extraction_status": "extracted",
            "extracted_at": now_iso(),
            "extraction_input_tokens": header.get("extraction_input_tokens"),
            "extraction_output_tokens": header.get("extraction_output_tokens"),
            "extraction_model": header.get("extraction_model"),
            "extraction_cost_usd": _calc_bank_cost(
                header.get("extraction_model"),
                header.get("extraction_input_tokens"),
                header.get("extraction_output_tokens"),
            ),
        }
        db.table("bank_statement_uploads").update(upload_patch).eq("id", upload_id).execute()

        if balance_summary["balance_status"] == "balanced" and closing is not None:
            db.table("bank_accounts").update({
                "current_reconciled_balance": closing,
                "last_statement_upload_id": upload_id,
            }).eq("id", account["id"]).execute()

        log_bank_event(
            db,
            organisation_id=organisation_id,
            event_type="bank_statement_extracted",
            actor_user_id=user_id,
            bank_account_id=account["id"],
            bank_statement_upload_id=upload_id,
            line_count=len(inserts),
            duplicate_summary=duplicate_summary,
            balance_summary=balance_summary,
        )
        return {
            "success": True,
            "upload_id": upload_id,
            "line_count": len(inserts),
            "duplicate_summary": duplicate_summary,
            "balance_summary": balance_summary,
        }
    except Exception as exc:
        db.table("bank_statement_uploads").update({"extraction_status": "failed"}).eq("id", upload_id).execute()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/uploads/{upload_id}/lines")
def list_bank_lines(upload_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    res = (
        db.table("bank_statement_lines")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_upload_id", upload_id)
        .order("line_date")
        .limit(2000)
        .execute()
    )
    return {"success": True, "lines": res.data or []}


@router.post("/lines/{line_id}/suggest")
def suggest_bank_line(line_id: str, payload: ExtractUploadRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines").select("*").eq("id", line_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement line not found",
    )
    suggestions = score_invoice_suggestions(db, organisation_id=organisation_id, line=line)
    suggestions += score_rule_suggestions(
        db,
        organisation_id=organisation_id,
        bank_account_id=line["bank_account_id"],
        line=line,
    )
    db.table("bank_transaction_suggestions").delete().eq("bank_statement_line_id", line_id).eq("status", "open").execute()
    inserts = [{**s, "organisation_id": organisation_id, "bank_statement_line_id": line_id} for s in suggestions]
    if inserts:
        db.table("bank_transaction_suggestions").insert(inserts).execute()
        db.table("bank_statement_lines").update({"match_status": "suggested"}).eq("id", line_id).execute()
    return {"success": True, "suggestions": inserts}


@router.post("/lines/{line_id}/review")
def review_bank_line(line_id: str, payload: ReviewLineRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines").select("*").eq("id", line_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement line not found",
    )
    suggestion = None
    if payload.suggestion_id:
        suggestion = _one(
            db.table("bank_transaction_suggestions").select("*").eq("id", str(payload.suggestion_id)).eq("organisation_id", organisation_id).limit(1).execute(),
            "Suggestion not found",
        )
        db.table("bank_transaction_suggestions").update({"status": "accepted"}).eq("id", suggestion["id"]).execute()
    accepted_rule_id = None
    if payload.create_rule:
        criteria_mode = (payload.criteria_mode or "and").lower()
        if criteria_mode not in {"and", "or", "only"}:
            raise HTTPException(status_code=400, detail="Rule criteria mode must be and, or, or only")
        criteria = normalize_rule_criteria(payload.rule_criteria)
        if not criteria:
            criteria = [] if criteria_mode == "only" else default_rule_criteria_from_line(line)
        if not criteria:
            raise HTTPException(status_code=400, detail="Add at least one rule condition")
        rule_name = payload.rule_name or f"Rule from {line.get('description') or 'bank transaction'}"
        res = db.table("bank_transaction_rules").insert({
            "organisation_id": organisation_id,
            "bank_account_id": line["bank_account_id"],
            "name": rule_name,
            "amount_direction": "money_in" if money(line.get("signed_amount")) >= 0 else "money_out",
            "match_type": "contains",
            "criteria": criteria,
            "criteria_mode": criteria_mode,
            "description_pattern": None if criteria_mode == "only" else (line.get("description") or "")[:80],
            "reference_pattern": None if criteria_mode == "only" else line.get("reference"),
            "counterparty_pattern": None if criteria_mode == "only" else line.get("counterparty"),
            "gl_account_id": str(payload.gl_account_id) if payload.gl_account_id else (suggestion or {}).get("suggested_account_id"),
            "tracking": payload.tracking or (suggestion or {}).get("suggested_tracking") or {},
            "tax_treatment": payload.tax_treatment or (suggestion or {}).get("suggested_tax_treatment"),
            "source_bank_statement_line_id": line_id,
            "created_by": user_id,
        }).execute()
        accepted_rule_id = _one(res, "Rule create failed")["id"]
        log_bank_event(
            db,
            organisation_id=organisation_id,
            event_type="bank_rule_created",
            actor_user_id=user_id,
            bank_account_id=line["bank_account_id"],
            bank_statement_upload_id=line["bank_statement_upload_id"],
            bank_statement_line_id=line_id,
            created_rule_id=accepted_rule_id,
            criteria=criteria,
            criteria_mode=criteria_mode,
        )
    patch = {
        "accepted_suggestion_id": str(payload.suggestion_id) if payload.suggestion_id else None,
        "accepted_rule_id": accepted_rule_id,
        "supplier_id": str(payload.supplier_id) if payload.supplier_id else None,
        "match_status": "matched" if suggestion and suggestion.get("matched_invoice_id") else "suggested",
        "allocation_status": "allocated" if (payload.gl_account_id or suggestion) else "unallocated",
        "review_status": "reviewed",
        "reviewed_by": user_id,
        "reviewed_at": now_iso(),
    }
    try:
        db.table("bank_statement_lines").update(patch).eq("id", line_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save review: {exc}") from exc
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_line_reviewed",
        actor_user_id=user_id,
        bank_account_id=line["bank_account_id"],
        bank_statement_upload_id=line["bank_statement_upload_id"],
        bank_statement_line_id=line_id,
        suggestion_id=str(payload.suggestion_id) if payload.suggestion_id else None,
        created_rule_id=accepted_rule_id,
    )
    return {"success": True}


@router.post("/lines/{line_id}/journal-preview")
def preview_bank_journal(line_id: str, payload: DraftJournalRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_read(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines").select("*").eq("id", line_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement line not found",
    )
    rows = build_journal_rows_for_line(
        db,
        organisation_id=organisation_id,
        line=line,
        gl_account_id=str(payload.gl_account_id),
        tracking=payload.tracking,
        vat_rate=payload.vat_rate,
        vat_account_id=str(payload.vat_account_id) if payload.vat_account_id else None,
    )
    return {
        "success": True,
        "lines": journal_preview_lines(db, organisation_id, rows),
        "total_debit": dec_to_float(sum(money(row["debit_amount"]) for row in rows)),
        "total_credit": dec_to_float(sum(money(row["credit_amount"]) for row in rows)),
    }


@router.post("/lines/{line_id}/draft-journal")
def draft_bank_journal(line_id: str, payload: DraftJournalRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines").select("*").eq("id", line_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement line not found",
    )
    if line.get("posting_status") == "posted":
        raise HTTPException(status_code=400, detail="Posted bank transactions must be unposted before redrafting")
    if line.get("posting_status") == "draft" and line.get("gl_journal_id"):
        journal = _one(
            db.table("gl_journals").select("*").eq("id", line["gl_journal_id"]).eq("organisation_id", organisation_id).limit(1).execute(),
            "Draft journal not found",
        )
        existing_lines = (
            db.table("gl_journal_lines")
            .select("*")
            .eq("gl_journal_id", journal["id"])
            .order("sort_order")
            .execute()
            .data
            or []
        )
        return {"success": True, "journal": journal, "lines": journal_preview_lines(db, organisation_id, existing_lines)}
    journal_id = new_uuid()
    description = line.get("description") or "Bank transaction"
    journal_lines = build_journal_rows_for_line(
        db,
        organisation_id=organisation_id,
        line=line,
        gl_account_id=str(payload.gl_account_id),
        tracking=payload.tracking,
        vat_rate=payload.vat_rate,
        vat_account_id=str(payload.vat_account_id) if payload.vat_account_id else None,
    )
    total_debit = sum(money(row["debit_amount"]) for row in journal_lines)
    total_credit = sum(money(row["credit_amount"]) for row in journal_lines)
    journal = {
        "id": journal_id,
        "organisation_id": organisation_id,
        "source_type": "bank_transaction",
        "source_id": line_id,
        "journal_date": line.get("line_date"),
        "description": description,
        "status": "draft",
        "total_debit": dec_to_float(total_debit),
        "total_credit": dec_to_float(total_credit),
        "created_by": user_id,
    }
    db.table("gl_journals").insert(journal).execute()
    db.table("gl_journal_lines").insert([{**row, "gl_journal_id": journal_id} for row in journal_lines]).execute()
    db.table("bank_statement_lines").update({"posting_status": "draft", "gl_journal_id": journal_id}).eq("id", line_id).execute()
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_journal_drafted",
        actor_user_id=user_id,
        bank_account_id=line["bank_account_id"],
        bank_statement_upload_id=line["bank_statement_upload_id"],
        bank_statement_line_id=line_id,
        gl_journal_id=journal_id,
    )
    return {"success": True, "journal": journal, "lines": journal_preview_lines(db, organisation_id, journal_lines)}


@router.post("/journals/{journal_id}/post")
def post_bank_journal(journal_id: str, payload: PostJournalRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    journal = _one(
        db.table("gl_journals").select("*").eq("id", journal_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Journal not found",
    )
    if journal.get("status") != "draft":
        raise HTTPException(status_code=400, detail="Only draft journals can be posted")
    journal_lines = (
        db.table("gl_journal_lines")
        .select("account_id, tracking, sort_order")
        .eq("gl_journal_id", journal_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    if journal.get("source_type") == "bank_transaction" and journal.get("source_id"):
        source_line = _one(
            db.table("bank_statement_lines")
            .select("bank_account_id")
            .eq("id", journal["source_id"])
            .eq("organisation_id", organisation_id)
            .limit(1)
            .execute(),
            "Bank statement line not found",
        )
        bank_account = _one(
            db.table("bank_accounts")
            .select("gl_account_id")
            .eq("id", source_line["bank_account_id"])
            .eq("organisation_id", organisation_id)
            .limit(1)
            .execute(),
            "Bank account not found",
        )
        allocation_line = next(
            (
                line
                for line in journal_lines
                if str(line.get("account_id")) != str(bank_account.get("gl_account_id"))
            ),
            None,
        )
        try:
            validate_bank_allocation_tracking(
                tracking=(allocation_line or {}).get("tracking") or {},
                required_dimensions=required_tracking_dimensions(
                    db,
                    organisation_id=organisation_id,
                    module_key="bank_cash",
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.table("gl_journals").update({"status": "posted", "posted_by": user_id, "posted_at": now_iso()}).eq("id", journal_id).execute()
    if journal.get("source_type") == "bank_transaction" and journal.get("source_id"):
        db.table("bank_statement_lines").update({"posting_status": "posted"}).eq("id", journal["source_id"]).execute()
    log_bank_event(db, organisation_id=organisation_id, event_type="bank_journal_posted", actor_user_id=user_id, gl_journal_id=journal_id)
    return {"success": True}


@router.get("/journals/{journal_id}/lines")
def list_bank_journal_lines(journal_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    _one(
        db.table("gl_journals").select("id").eq("id", journal_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Journal not found",
    )
    rows = (
        db.table("gl_journal_lines")
        .select("*")
        .eq("gl_journal_id", journal_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    return {"success": True, "lines": journal_preview_lines(db, organisation_id, rows)}


@router.post("/journals/{journal_id}/unpost")
def unpost_bank_journal(journal_id: str, payload: PostJournalRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    journal = _one(
        db.table("gl_journals").select("*").eq("id", journal_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Journal not found",
    )
    if journal.get("status") != "posted":
        raise HTTPException(status_code=400, detail="Only posted journals can be unposted")
    source_line_id = journal.get("source_id") if journal.get("source_type") == "bank_transaction" else None
    if not source_line_id:
        raise HTTPException(status_code=400, detail="Only bank transaction journals can be unposted here")
    existing_reversal = (
        db.table("gl_journals")
        .select("id")
        .eq("organisation_id", organisation_id)
        .eq("reversal_of_journal_id", journal_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing_reversal:
        raise HTTPException(status_code=400, detail="This journal has already been reversed")

    original_lines = (
        db.table("gl_journal_lines")
        .select("*")
        .eq("gl_journal_id", journal_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    if not original_lines:
        raise HTTPException(status_code=400, detail="Journal has no lines to reverse")

    reversal_id = new_uuid()
    description = f"Reversal: {journal.get('description') or 'Bank journal'}"
    reversal_rows = reversal_lines_for_journal(original_lines, description=description)
    total_debit = sum(money(row["debit_amount"]) for row in reversal_rows)
    total_credit = sum(money(row["credit_amount"]) for row in reversal_rows)
    reversal_journal = {
        "id": reversal_id,
        "organisation_id": organisation_id,
        "source_type": "bank_transaction_reversal",
        "source_id": source_line_id,
        "reversal_of_journal_id": journal_id,
        "journal_date": journal.get("journal_date"),
        "description": description,
        "status": "posted",
        "total_debit": dec_to_float(total_debit),
        "total_credit": dec_to_float(total_credit),
        "created_by": user_id,
        "posted_by": user_id,
        "posted_at": now_iso(),
    }
    db.table("gl_journals").insert(reversal_journal).execute()
    db.table("gl_journal_lines").insert([{**row, "gl_journal_id": reversal_id} for row in reversal_rows]).execute()
    db.table("gl_journals").update({"status": "reversed", "reversed_by": user_id, "reversed_at": now_iso()}).eq("id", journal_id).execute()
    db.table("bank_statement_lines").update({
        "posting_status": "unposted",
        "allocation_status": "unallocated",
        "match_status": "unmatched",
        "review_status": "pending",
        "accepted_suggestion_id": None,
        "accepted_rule_id": None,
        "gl_journal_id": None,
        "reviewed_by": None,
        "reviewed_at": None,
    }).eq("id", source_line_id).eq("organisation_id", organisation_id).execute()
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_journal_unposted",
        actor_user_id=user_id,
        bank_statement_line_id=source_line_id,
        gl_journal_id=journal_id,
        reversal_journal_id=reversal_id,
    )
    return {
        "success": True,
        "journal": journal,
        "reversal_journal": reversal_journal,
        "lines": journal_preview_lines(db, organisation_id, reversal_rows),
    }
