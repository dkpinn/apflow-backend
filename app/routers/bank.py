from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Query, Response

from app.routers._bank_common import (
    _auth,
    _bank_delete_error,
    _database_error_parts,
    _delete_bank_lines_rpc,
    _delete_bank_uploads_rpc,
    _one,
    _remove_bank_upload_files,
    _rpc_data,
    log_bank_event,
    now_iso,
    svc,
)
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.models.schemas import BulkAllocateRequest, LineSkipRequest
from app.schemas.bank import (
    BankAccountCreate,
    BankUploadCreate,
    BulkDeleteLinesRequest,
    BulkDeleteUploadsRequest,
    DraftJournalRequest,
    ExtractUploadRequest,
    ParsingRuleCreate,
    ParsingRuleUpdate,
    PostJournalRequest,
    ReviewLineRequest,
)
from app.services.extraction_foundation import file_sha256
from app.services.bank_extraction_validation import validate_extracted_statement_quality
from app.services.bank_statement_service import (
    default_rule_criteria_from_line,
    detect_line_duplicates,
    extract_statement,
    line_to_insert,
    new_uuid,
    normalize_rule_criteria,
    reversal_lines_for_journal,
    score_invoice_suggestions,
    score_rule_suggestions,
    validate_balances,
)
from app.services.bank.accounts import (
    bank_line_sort_key,
    create_bank_account_record,
    create_bank_supplier_if_missing,
    get_unreconciled_lines_payload,
    is_unreconciled_bank_line,
    list_accounts as list_bank_account_records,
)
from app.services.bank.parsing_rules import (
    create_rule as create_bank_parsing_rule_record,
    delete_rule as delete_bank_parsing_rule_record,
    list_rules as list_bank_parsing_rule_records,
    lookup_parsing_hint,
    update_rule as update_bank_parsing_rule_record,
)
from app.services.bank.journals import (
    account_labels,
    build_journal_rows_for_line,
    journal_preview_lines,
)
from app.services.sales_invoices import post_customer_receipt
from app.services.bank_statement_export import (
    generate_bank_statement_report,
    bank_statement_csv,
    bank_statement_xlsx,
)

router = APIRouter(prefix="/api/bank", tags=["bank"])

_BANK_COST_PER_MILLION: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.5-pro":   {"input": 1.25, "output": 10.00},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gpt-4o":                    {"input": 5.00, "output": 15.00},
    "gpt-4.1-mini":              {"input": 0.40, "output":  1.60},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output":  4.00},
}

def _calc_bank_cost(model: str | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    if not model or input_tokens is None:
        return None
    rates = next((r for k, r in _BANK_COST_PER_MILLION.items() if (model or "").startswith(k)), None)
    if not rates:
        return None
    return round((input_tokens or 0) * rates["input"] / 1_000_000 + (output_tokens or 0) * rates.get("output", 0) / 1_000_000, 8)


@router.get("/accounts")
def list_bank_accounts(organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    return {"success": True, "accounts": list_bank_account_records(db, organisation_id=organisation_id)}


@router.get("/accounts/{account_id}/unreconciled-lines")
def list_bank_account_unreconciled_lines(account_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    payload = get_unreconciled_lines_payload(
        db,
        organisation_id=organisation_id,
        account_id=account_id,
    )
    return {"success": True, **payload}


@router.get("/accounts/{account_id}/statement/export")
def export_bank_statement(
    account_id: str,
    organisation_id: str,
    auth: UserAuth,
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    export_format: str = Query(..., alias="format", pattern="^(xlsx|csv)$"),
):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    try:
        report = generate_bank_statement_report(
            db,
            account_id=account_id,
            organisation_id=organisation_id,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    acct_slug = report["account_name"].replace(" ", "-").lower() or account_id[:8]
    if date_from and date_to:
        filename_base = f"bank-statement-{acct_slug}-{date_from}-to-{date_to}"
    else:
        filename_base = f"bank-statement-{acct_slug}-all"

    if export_format == "csv":
        content = bank_statement_csv(report)
        media_type = "text/csv; charset=utf-8"
        extension = "csv"
    else:
        try:
            content = bank_statement_xlsx(report)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        extension = "xlsx"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.{extension}"'},
    )


@router.post("/accounts")
def create_bank_account(payload: BankAccountCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    account = create_bank_account_record(
        db,
        payload=payload,
        organisation_id=organisation_id,
    )
    log_bank_event(db, organisation_id=organisation_id, event_type="bank_account_created", actor_user_id=user_id, bank_account_id=account["id"])
    create_bank_supplier_if_missing(db, organisation_id=organisation_id, payload=payload)
    return {"success": True, "account": account}


@router.get("/parsing-rules")
def list_parsing_rules(organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    return {"success": True, "rules": list_bank_parsing_rule_records(db, organisation_id=organisation_id)}


@router.post("/parsing-rules")
def create_parsing_rule(payload: ParsingRuleCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    rule = create_bank_parsing_rule_record(
        db,
        payload=payload,
        organisation_id=organisation_id,
        user_id=user_id,
    )
    return {"success": True, "rule": rule}


@router.put("/parsing-rules/{rule_id}")
def update_parsing_rule(rule_id: str, payload: ParsingRuleUpdate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    rule = update_bank_parsing_rule_record(
        db,
        rule_id=rule_id,
        payload=payload,
        organisation_id=organisation_id,
        updated_at=now_iso(),
    )
    return {"success": True, "rule": rule}


@router.delete("/parsing-rules/{rule_id}")
def delete_parsing_rule(rule_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_write(user_id, organisation_id)
    delete_bank_parsing_rule_record(db, rule_id=rule_id, organisation_id=organisation_id)
    return {"success": True}
