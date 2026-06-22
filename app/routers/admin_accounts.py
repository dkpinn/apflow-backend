from __future__ import annotations

import csv
import io
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_platform_owner

router = APIRouter(prefix="/api/admin/standard-accounts", tags=["admin-accounts"])

VALID_TYPES = ("income", "expense", "asset", "liability", "equity", "other")
VALID_VAT = ("full", "blocked", "exempt", "zero_rated")


def _platform_db(auth: UserAuth):
    user_id, _ = auth
    ensure_platform_owner(user_id)
    return user_id, get_supabase_client()


class StandardAccountCreate(BaseModel):
    code: str
    name: str
    type: str
    group_name: Optional[str] = None
    description: Optional[str] = None
    vat_treatment: str = "full"
    display_order: int = 0


class StandardAccountUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    group_name: Optional[str] = None
    description: Optional[str] = None
    vat_treatment: Optional[str] = None
    display_order: Optional[int] = None
    active: Optional[bool] = None


@router.get("")
def list_standard_accounts(auth: UserAuth) -> dict:
    _user_id, db = _platform_db(auth)
    rows = (
        db.table("platform_standard_accounts")
        .select("*")
        .order("display_order")
        .execute()
        .data
        or []
    )
    return {"accounts": rows}


@router.post("")
def create_standard_account(payload: StandardAccountCreate, auth: UserAuth) -> dict:
    _user_id, db = _platform_db(auth)
    if payload.type not in VALID_TYPES:
        raise HTTPException(400, f"Invalid type '{payload.type}'. Must be one of: {', '.join(VALID_TYPES)}")
    if payload.vat_treatment not in VALID_VAT:
        raise HTTPException(400, f"Invalid vat_treatment '{payload.vat_treatment}'")
    row = {
        "code": payload.code.strip(),
        "name": payload.name.strip(),
        "type": payload.type,
        "group_name": payload.group_name,
        "description": payload.description,
        "vat_treatment": payload.vat_treatment,
        "is_system": False,
        "display_order": payload.display_order,
        "active": True,
    }
    res = db.table("platform_standard_accounts").insert(row).execute()
    data = res.data
    if not data:
        raise HTTPException(500, "Failed to create standard account")
    return {"account": data[0]}


# ---------------------------------------------------------------------------
# Template download  (must be before /{account_id} routes)
# ---------------------------------------------------------------------------

_TEMPLATE_CSV = """\
# Chart of Accounts Import Template
# Allowed values for 'type': income, expense, asset, liability, equity, other
# Allowed values for 'vat_treatment': full, blocked, exempt, zero_rated
# Allowed values for 'group_name': Revenue, Other Income, Direct Costs, Operating Expenses, Taxation, Current Assets, Bank, Inventory, Investments, Current Liabilities, Finance Agreements, Equity, Retained Income/Loss, Rounding
# 'display_order' controls sort order within each group (lower = first). Leave blank to default to 0.
# 'description' is optional.
code,name,type,group_name,vat_treatment,display_order,description
4000,Sales Revenue,income,Revenue,full,100,
4010,Service Revenue,income,Revenue,full,110,
4020,Other Income,income,Other Income,full,200,
4030,Interest Received,income,Other Income,zero_rated,210,
5000,Cost of Sales,expense,Direct Costs,full,300,
5010,Raw Materials & Consumables,expense,Direct Costs,full,310,
6000,Salaries and Wages,expense,Operating Expenses,exempt,400,
6010,Rent,expense,Operating Expenses,full,410,
6020,Utilities,expense,Operating Expenses,full,420,
6030,Insurance,expense,Operating Expenses,exempt,430,
6040,Bank Charges,expense,Operating Expenses,exempt,440,
6300,Income Tax Expense,expense,Taxation,exempt,600,
1200,Trade Debtors (Accounts Receivable),asset,Current Assets,full,700,
1300,Prepaid Expenses,asset,Current Assets,full,710,
1500,Inventory,asset,Inventory,full,800,
1600,Long-term Investments,asset,Investments,zero_rated,900,
2100,Trade Creditors (Accounts Payable),liability,Current Liabilities,full,1000,
2200,VAT Control Account,liability,Current Liabilities,full,1010,
2300,Accrued Expenses,liability,Current Liabilities,full,1020,
3000,Vehicle Finance,liability,Finance Agreements,exempt,1100,
7000,Share Capital / Owner's Equity,equity,Equity,exempt,1200,
7100,Retained Earnings / (Accumulated Loss),equity,Retained Income/Loss,exempt,1220,
9999,Rounding Adjustments,other,Rounding,exempt,9999,
"""


@router.get("/template")
def download_template(auth: UserAuth) -> Response:
    _platform_db(auth)  # auth check only
    return Response(
        content=_TEMPLATE_CSV,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="chart_of_accounts_template.csv"'},
    )


# ---------------------------------------------------------------------------
# File import  (must be before /{account_id} routes)
# ---------------------------------------------------------------------------

def _parse_rows(filename: str, content: bytes) -> list[dict]:
    ext = (filename or "").rsplit(".", 1)[-1].lower()

    if ext in ("csv", "txt"):
        text = content.decode("utf-8-sig")  # strip BOM if present
        reader = csv.DictReader(
            (line for line in text.splitlines() if not line.lstrip().startswith("#")),
        )
        return [dict(r) for r in reader]

    if ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h).strip() if h is not None else "" for h in rows[0]]
        return [
            {headers[i]: (str(v).strip() if v is not None else "") for i, v in enumerate(row)}
            for row in rows[1:]
            if any(v is not None for v in row)
        ]

    if ext == "xls":
        import xlrd
        wb = xlrd.open_workbook(file_contents=content)
        ws = wb.sheet_by_index(0)
        if ws.nrows == 0:
            return []
        headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]
        result = []
        for r in range(1, ws.nrows):
            row = {headers[c]: str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)}
            if any(v for v in row.values()):
                result.append(row)
        return result

    raise HTTPException(400, f"Unsupported file type '.{ext}'. Upload .csv, .xlsx, .xls, or .txt")


@router.post("/import")
def import_standard_accounts(
    auth: UserAuth,
    file: UploadFile = File(...),
) -> dict:
    _user_id, db = _platform_db(auth)

    content = file.file.read()
    try:
        rows = _parse_rows(file.filename or "", content)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Could not parse file: {exc}") from exc

    to_insert: list[dict] = []
    errors: list[dict] = []

    for i, row in enumerate(rows, start=2):  # row 1 = header
        code = row.get("code", "").strip()
        name = row.get("name", "").strip()
        acct_type = row.get("type", "").strip().lower()
        group_name = row.get("group_name", "").strip() or None
        vat = row.get("vat_treatment", "full").strip().lower() or "full"
        display_order_raw = row.get("display_order", "0").strip()
        description = row.get("description", "").strip() or None

        if not code:
            errors.append({"row": i, "reason": "Missing 'code'"})
            continue
        if not name:
            errors.append({"row": i, "reason": f"Row {i}: Missing 'name'"})
            continue
        if acct_type not in VALID_TYPES:
            errors.append({"row": i, "reason": f"Invalid type '{acct_type}' — must be one of: {', '.join(VALID_TYPES)}"})
            continue
        if vat not in VALID_VAT:
            errors.append({"row": i, "reason": f"Invalid vat_treatment '{vat}'"})
            continue

        try:
            display_order = int(float(display_order_raw)) if display_order_raw else 0
        except ValueError:
            display_order = 0

        to_insert.append({
            "code": code,
            "name": name,
            "type": acct_type,
            "group_name": group_name,
            "vat_treatment": vat,
            "display_order": display_order,
            "description": description,
            "is_system": False,
            "active": True,
        })

    imported = 0
    skipped = 0

    if to_insert:
        existing_codes: set[str] = set()
        try:
            existing_res = (
                db.table("platform_standard_accounts")
                .select("code")
                .execute()
            )
            existing_codes = {r["code"] for r in (existing_res.data or [])}
        except Exception:
            pass

        new_rows = [r for r in to_insert if r["code"] not in existing_codes]
        skipped = len(to_insert) - len(new_rows)

        if new_rows:
            try:
                res = db.table("platform_standard_accounts").insert(new_rows).execute()
                imported = len(res.data or [])
            except Exception as exc:
                raise HTTPException(500, f"Database insert failed: {exc}") from exc

    return {"imported": imported, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Single-account mutations  (parameterised — must come after specific routes)
# ---------------------------------------------------------------------------

@router.put("/{account_id}")
def update_standard_account(account_id: str, payload: StandardAccountUpdate, auth: UserAuth) -> dict:
    _user_id, db = _platform_db(auth)
    existing = (
        db.table("platform_standard_accounts")
        .select("is_system")
        .eq("id", account_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not existing:
        raise HTTPException(404, "Standard account not found")
    is_system = existing[0].get("is_system", False)

    patch: dict = {}
    if payload.code is not None:
        patch["code"] = payload.code.strip()
    if payload.name is not None:
        patch["name"] = payload.name.strip()
    if payload.group_name is not None:
        patch["group_name"] = payload.group_name
    if payload.description is not None:
        patch["description"] = payload.description
    if payload.vat_treatment is not None:
        if payload.vat_treatment not in VALID_VAT:
            raise HTTPException(400, f"Invalid vat_treatment '{payload.vat_treatment}'")
        patch["vat_treatment"] = payload.vat_treatment
    if payload.display_order is not None:
        patch["display_order"] = payload.display_order
    if payload.active is not None:
        if is_system and payload.active is False:
            raise HTTPException(400, "System accounts cannot be deactivated")
        patch["active"] = payload.active

    if not patch:
        raise HTTPException(400, "No fields to update")

    res = (
        db.table("platform_standard_accounts")
        .update(patch)
        .eq("id", account_id)
        .execute()
    )
    data = res.data
    if not data:
        raise HTTPException(500, "Update failed")
    return {"account": data[0]}


@router.delete("/{account_id}")
def deactivate_standard_account(account_id: str, auth: UserAuth) -> dict:
    _user_id, db = _platform_db(auth)
    existing = (
        db.table("platform_standard_accounts")
        .select("is_system, name")
        .eq("id", account_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not existing:
        raise HTTPException(404, "Standard account not found")
    if existing[0].get("is_system"):
        raise HTTPException(400, f"'{existing[0]['name']}' is a system account and cannot be removed from the template")
    db.table("platform_standard_accounts").update({"active": False}).eq("id", account_id).execute()
    return {"success": True}
