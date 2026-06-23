from __future__ import annotations

import json
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.supabase_client import get_fresh_supabase_client, get_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.models.schemas import BulkAllocateRequest, LineSkipRequest
from app.services.extraction_foundation import file_sha256
from app.services.bank_extraction_validation import validate_extracted_statement_quality
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
from app.services.bank_account_summary import build_bank_balance_summary
from app.services.organisation_module_settings import (
    required_tracking_dimensions,
    validate_bank_allocation_tracking,
)
from app.services.sales_invoices import post_customer_receipt

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


class BulkDeleteLinesRequest(BaseModel):
    organisation_id: UUID
    line_ids: list[UUID]


class BulkDeleteUploadsRequest(BaseModel):
    organisation_id: UUID
    upload_ids: list[UUID]


class BankBalanceSummary(BaseModel):
    bank_statement_balance: Optional[float] = None
    calculated_imported_balance: Optional[float] = None
    current_tb_balance: Optional[float] = None
    latest_statement_upload_id: Optional[str] = None
    statement_period_to: Optional[str] = None
    latest_transaction_date: Optional[str] = None
    bank_balance_status: Literal["available", "unavailable"]
    imported_balance_status: Literal["available", "unavailable"]
    tb_balance_status: Literal["available", "gl_account_not_linked"]


class ParsingRuleCreate(BaseModel):
    organisation_id: UUID
    institution_name: Optional[str] = None
    account_type: Optional[str] = None
    parsing_hint: str
    active: bool = True


class ParsingRuleUpdate(BaseModel):
    organisation_id: UUID
    institution_name: Optional[str] = None
    account_type: Optional[str] = None
    parsing_hint: Optional[str] = None
    active: Optional[bool] = None


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
        logger.exception("bank audit event insert failed: %s", payload)


def _rpc_data(result: Any) -> dict[str, Any]:
    data = getattr(result, "data", result)
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _database_error_parts(exc: Exception) -> tuple[str, Any]:
    message = str(exc)
    details: Any = None
    candidates = [getattr(exc, "message", None), getattr(exc, "details", None)]
    if exc.args and isinstance(exc.args[0], dict):
        payload = exc.args[0]
        candidates.extend([payload.get("message"), payload.get("details")])
    for candidate in candidates:
        if not candidate:
            continue
        if isinstance(candidate, str) and candidate.startswith("["):
            try:
                details = json.loads(candidate)
                continue
            except json.JSONDecodeError:
                pass
        if isinstance(candidate, str) and "blocked" in candidate.lower():
            message = candidate
        elif not isinstance(candidate, str):
            details = candidate
    return message, details


def _bank_delete_error(exc: Exception) -> HTTPException:
    message, blocked = _database_error_parts(exc)
    lowered = message.lower()
    if "blocked" in lowered or "posted or reversed" in lowered:
        return HTTPException(
            status_code=409,
            detail={
                "message": "Deletion blocked because selected bank data has posted or reversed journal history.",
                "blocked": blocked or [],
            },
        )
    if "not found" in lowered:
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=400, detail=message)


def _delete_bank_lines_rpc(
    db,
    *,
    organisation_id: str,
    line_ids: list[str],
    actor_user_id: str,
) -> dict[str, Any]:
    result = db.rpc(
        "delete_bank_statement_lines_atomic",
        {
            "p_org_id": organisation_id,
            "p_line_ids": line_ids,
            "p_actor_user_id": actor_user_id,
        },
    ).execute()
    return _rpc_data(result)


def _delete_bank_uploads_rpc(
    db,
    *,
    organisation_id: str,
    upload_ids: list[str],
    actor_user_id: str,
) -> dict[str, Any]:
    result = db.rpc(
        "delete_bank_statement_uploads_atomic",
        {
            "p_org_id": organisation_id,
            "p_upload_ids": upload_ids,
            "p_actor_user_id": actor_user_id,
        },
    ).execute()
    return _rpc_data(result)


def _remove_bank_upload_files(db, files: list[dict[str, Any]]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for file in files:
        bucket = str(file.get("storage_bucket") or "statement-files")
        path = str(file.get("storage_path") or "")
        if not path:
            continue
        try:
            db.storage.from_(bucket).remove([path])
        except Exception as exc:
            failures.append({"storage_bucket": bucket, "storage_path": path, "error": str(exc)})
    return failures


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


def lookup_parsing_hint(db, *, organisation_id: str, institution_name: Optional[str], account_type: Optional[str]) -> Optional[str]:
    try:
        rules = (
            db.table("bank_parsing_rules")
            .select("institution_name, account_type, parsing_hint")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
            .execute()
            .data
            or []
        )
    except Exception:
        return None

    institution = (institution_name or "").strip().lower()
    acct_type = (account_type or "").strip().lower()

    def specificity(rule: dict[str, Any]) -> int:
        rule_institution = (rule.get("institution_name") or "").strip().lower()
        rule_account_type = (rule.get("account_type") or "").strip().lower()
        institution_match = bool(rule_institution) and rule_institution == institution
        account_type_match = bool(rule_account_type) and rule_account_type == acct_type
        if institution_match and account_type_match:
            return 3
        if institution_match and not rule_account_type:
            return 2
        if account_type_match and not rule_institution:
            return 1
        if not rule_institution and not rule_account_type:
            return 0
        return -1

    candidates = [(specificity(rule), rule) for rule in rules]
    candidates = [(score, rule) for score, rule in candidates if score >= 0]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, best_rule = candidates[0]
    return best_rule.get("parsing_hint") or None


def is_unreconciled_bank_line(line: dict[str, Any]) -> bool:
    posting_status = str(line.get("posting_status") or "unposted").lower()
    allocation_status = str(line.get("allocation_status") or "unallocated").lower()
    review_status = str(line.get("review_status") or "pending").lower()
    return (
        posting_status != "posted"
        or allocation_status not in {"allocated", "split"}
        or review_status != "reviewed"
    )


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

    try:
        upload_rows = (
            db.table("bank_statement_uploads")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("bank_account_id", account_id)
            .execute()
            .data
            or []
        )
    except Exception:
        upload_rows = []
    uploads_by_id = {str(row.get("id")): row for row in upload_rows if row.get("id")}

    # Batch-fetch open suggestions to enrich lines with confidence + account hints
    top_suggestion: dict[str, dict] = {}
    if lines:
        line_id_strs = [str(l["id"]) for l in lines[:500]]
        sug_rows = (
            db.table("bank_transaction_suggestions")
            .select("bank_statement_line_id, confidence_score, suggested_account_id, suggested_tax_treatment, matched_invoice_number")
            .eq("organisation_id", organisation_id)
            .in_("bank_statement_line_id", line_id_strs)
            .eq("status", "open")
            .order("confidence_score", desc=True)
            .limit(5000)
            .execute()
            .data
            or []
        )
        for s in sug_rows:
            lid = str(s.get("bank_statement_line_id") or "")
            if lid and lid not in top_suggestion:
                top_suggestion[lid] = s

    enriched = []
    for line in lines:
        upload = uploads_by_id.get(str(line.get("bank_statement_upload_id"))) or {}
        sug = top_suggestion.get(str(line.get("id") or ""), {})
        confidence = float(sug.get("confidence_score") or (0.75 if line.get("match_status") == "suggested" else 0.5))
        enriched.append({
            **line,
            "upload_original_filename": upload.get("original_filename"),
            "upload_uploaded_at": upload.get("uploaded_at"),
            "recon_confidence": confidence,
            "recon_suggested_account_id": sug.get("suggested_account_id"),
            "recon_suggested_tax": sug.get("suggested_tax_treatment"),
            "recon_matched_invoice_ref": sug.get("matched_invoice_number") or line.get("matched_invoice_number"),
        })

    balances = BankBalanceSummary.model_validate(
        build_bank_balance_summary(
            db,
            organisation_id=organisation_id,
            account=account,
            lines=rows,
            uploads=upload_rows,
        )
    )
    return {
        "success": True,
        "account": account,
        "lines": enriched,
        "balances": balances.model_dump(),
    }


@router.post("/accounts")
def create_bank_account(payload: BankAccountCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    opening = float(money(payload.opening_balance))

    # Auto-create GL account in the 6200xxx range when none is provided
    gl_account_id: str | None = str(payload.gl_account_id) if payload.gl_account_id else None
    if not gl_account_id:
        code_rows = (
            db.table("accounts")
            .select("code")
            .eq("organisation_id", organisation_id)
            .like("code", "6200%")
            .execute()
            .data
            or []
        )
        used: set[int] = set()
        for r in code_rows:
            try:
                used.add(int(r["code"]))
            except (TypeError, ValueError):
                pass
        next_code = 6200001
        while next_code in used:
            next_code += 1
        if next_code > 6200999:
            raise HTTPException(status_code=400, detail="No bank GL codes available in range 6200001–6200999")
        gl_res = (
            db.table("accounts")
            .insert({
                "organisation_id": organisation_id,
                "code": str(next_code),
                "name": payload.name,
                "type": "asset",
                "group_name": "Bank",
                "vat_treatment": "full",
                "is_system": False,
                "active": True,
            })
            .execute()
        )
        if not gl_res.data:
            raise HTTPException(status_code=500, detail="Failed to create GL account for bank account")
        gl_account_id = str(gl_res.data[0]["id"])

    row = {
        "organisation_id": organisation_id,
        "name": payload.name,
        "institution_name": payload.institution_name,
        "account_type": payload.account_type,
        "currency": payload.currency,
        "account_number_mask": payload.account_number_mask,
        "account_number_hash": payload.account_number_hash,
        "gl_account_id": gl_account_id,
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


@router.get("/parsing-rules")
def list_parsing_rules(organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    res = (
        db.table("bank_parsing_rules")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("institution_name")
        .execute()
    )
    return {"success": True, "rules": res.data or []}


@router.post("/parsing-rules")
def create_parsing_rule(payload: ParsingRuleCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    row = {
        "organisation_id": organisation_id,
        "institution_name": payload.institution_name,
        "account_type": payload.account_type,
        "parsing_hint": payload.parsing_hint,
        "active": payload.active,
        "created_by": user_id,
    }
    res = db.table("bank_parsing_rules").insert(row).execute()
    rule = _one(res, "Parsing rule create failed")
    return {"success": True, "rule": rule}


@router.put("/parsing-rules/{rule_id}")
def update_parsing_rule(rule_id: str, payload: ParsingRuleUpdate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    _one(
        db.table("bank_parsing_rules").select("id").eq("id", rule_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Parsing rule not found",
    )
    patch: dict[str, Any] = {"updated_at": now_iso()}
    if payload.institution_name is not None:
        patch["institution_name"] = payload.institution_name
    if payload.account_type is not None:
        patch["account_type"] = payload.account_type
    if payload.parsing_hint is not None:
        patch["parsing_hint"] = payload.parsing_hint
    if payload.active is not None:
        patch["active"] = payload.active
    res = db.table("bank_parsing_rules").update(patch).eq("id", rule_id).eq("organisation_id", organisation_id).execute()
    rule = _one(res, "Parsing rule update failed")
    return {"success": True, "rule": rule}


@router.delete("/parsing-rules/{rule_id}")
def delete_parsing_rule(rule_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_write(user_id, organisation_id)
    _one(
        db.table("bank_parsing_rules").select("id").eq("id", rule_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Parsing rule not found",
    )
    db.table("bank_parsing_rules").delete().eq("id", rule_id).eq("organisation_id", organisation_id).execute()
    return {"success": True}
