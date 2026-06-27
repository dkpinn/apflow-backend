from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

ZERO = Decimal("0.00")
MONEY = Decimal("0.01")
CHUNK_SIZE = 200

SUSPENSE_KEYWORDS = ("suspense", "clearing", "unidentified", "unallocated")


def _rows(query) -> list[dict]:
    return list(query.execute().data or [])


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v or "0"))
    except Exception:
        return ZERO


def _f(v: Decimal) -> float:
    return float(v.quantize(MONEY, rounding=ROUND_HALF_UP))


def _days_old(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        d = date.fromisoformat(str(date_str)[:10])
        return (date.today() - d).days
    except (ValueError, TypeError):
        return None


def _is_suspense_account(name: str) -> bool:
    low = (name or "").lower()
    return any(kw in low for kw in SUSPENSE_KEYWORDS)


# ── 1. Suspense GL accounts ───────────────────────────────────────────────────

def get_suspense_accounts(db, organisation_id: str) -> list[dict]:
    """
    Return accounts whose name contains a suspense/clearing keyword and have a
    non-zero current GL balance.  Each row includes recent journal lines for
    context.
    """
    accounts = _rows(
        db.table("accounts")
        .select("id, code, name, type")
        .eq("organisation_id", organisation_id)
        .eq("active", True)
    )
    suspense_accounts = [a for a in accounts if _is_suspense_account(a.get("name", ""))]
    if not suspense_accounts:
        return []

    account_ids = [str(a["id"]) for a in suspense_accounts]

    # Fetch all posted journal lines for these accounts
    DEBIT_NORMAL = {"asset", "expense", "other"}
    balances: dict[str, Decimal] = {aid: ZERO for aid in account_ids}
    recent_lines: dict[str, list[dict]] = {aid: [] for aid in account_ids}

    # Fetch journal lines in chunks
    for i in range(0, len(account_ids), CHUNK_SIZE):
        chunk = account_ids[i : i + CHUNK_SIZE]
        lines = _rows(
            db.table("gl_journal_lines")
            .select("id, account_id, debit_amount, credit_amount, description, gl_journal_id")
            .eq("organisation_id", organisation_id)
            .in_("account_id", chunk)
        )
        # Fetch corresponding journal dates
        jids = list({str(l["gl_journal_id"]) for l in lines if l.get("gl_journal_id")})
        journal_dates: dict[str, str] = {}
        for ji in range(0, len(jids), CHUNK_SIZE):
            journals = _rows(
                db.table("gl_journals")
                .select("id, journal_date")
                .eq("organisation_id", organisation_id)
                .eq("status", "posted")
                .in_("id", jids[ji : ji + CHUNK_SIZE])
            )
            journal_dates.update({str(j["id"]): j["journal_date"] for j in journals})

        for line in lines:
            aid = str(line.get("account_id") or "")
            if aid not in balances:
                continue
            jid = str(line.get("gl_journal_id") or "")
            if jid not in journal_dates:
                continue  # not posted
            debit = _dec(line.get("debit_amount"))
            credit = _dec(line.get("credit_amount"))
            acct_type = next((a["type"] for a in suspense_accounts if str(a["id"]) == aid), "")
            if acct_type in DEBIT_NORMAL:
                balances[aid] += debit - credit
            else:
                balances[aid] += credit - debit
            recent_lines[aid].append({
                "journal_date": journal_dates[jid],
                "description": line.get("description"),
                "debit_amount": _f(debit),
                "credit_amount": _f(credit),
            })

    results = []
    for a in suspense_accounts:
        aid = str(a["id"])
        bal = balances[aid]
        if bal == ZERO:
            continue
        # Sort and take 5 most recent
        lines_sorted = sorted(recent_lines[aid], key=lambda x: x["journal_date"] or "", reverse=True)[:5]
        results.append({
            "account_id": aid,
            "code": a.get("code"),
            "name": a.get("name"),
            "type": a.get("type"),
            "balance": _f(bal),
            "recent_lines": lines_sorted,
        })

    return sorted(results, key=lambda x: abs(x["balance"]), reverse=True)


# ── 2. Unreconciled bank lines ────────────────────────────────────────────────

def get_unreconciled_bank_lines(db, organisation_id: str, limit: int = 200) -> list[dict]:
    """
    Bank statement lines that are not yet posted or not yet allocated,
    excluding ignored/deferred.
    """
    rows = _rows(
        db.table("bank_statement_lines")
        .select(
            "id, line_date, description, counterparty, debit_amount, credit_amount,"
            " signed_amount, posting_status, allocation_status, review_status,"
            " bank_account_id"
        )
        .eq("organisation_id", organisation_id)
        .not_.in_("review_status", ["ignored", "deferred"])
        .order("line_date", desc=False)
        .limit(limit)
    )

    results = []
    for row in rows:
        if (
            row.get("posting_status") == "posted"
            and row.get("allocation_status") in ("allocated", "split")
            and row.get("review_status") == "reviewed"
        ):
            continue
        age = _days_old(row.get("line_date"))
        results.append({
            "id": row["id"],
            "line_date": row.get("line_date"),
            "description": row.get("description"),
            "counterparty": row.get("counterparty"),
            "debit_amount": _f(_dec(row.get("debit_amount"))),
            "credit_amount": _f(_dec(row.get("credit_amount"))),
            "signed_amount": _f(_dec(row.get("signed_amount"))),
            "posting_status": row.get("posting_status", "unposted"),
            "allocation_status": row.get("allocation_status", "unallocated"),
            "review_status": row.get("review_status", "pending"),
            "age_days": age,
        })
    return results


# ── 3. Approved invoices not yet posted ──────────────────────────────────────

def get_approved_unposted_invoices(db, organisation_id: str, limit: int = 200) -> list[dict]:
    """
    Supplier invoices approved in the review queue but not yet posted to the GL.
    """
    rows = _rows(
        db.table("invoices_extracted")
        .select(
            "id, supplier_name_extracted, invoice_number,"
            " invoice_date, total_amount, review_status, posting_status"
        )
        .eq("organisation_id", organisation_id)
        .eq("review_status", "approved")
        .neq("posting_status", "posted")
        .order("invoice_date", desc=False)
        .limit(limit)
    )
    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "supplier_name": row.get("supplier_name_extracted"),
            "invoice_number": row.get("invoice_number"),
            "invoice_date": row.get("invoice_date"),
            "total_amount": _f(_dec(row.get("total_amount"))),
            "review_status": row.get("review_status"),
            "posting_status": row.get("posting_status", "draft"),
        })
    return results


# ── 4. Extraction failures ────────────────────────────────────────────────────

def get_extraction_failures(db, organisation_id: str, limit: int = 100) -> list[dict]:
    """
    Invoice raw records that failed extraction and need manual attention.
    """
    rows = _rows(
        db.table("invoices_raw")
        .select("id, file_name, uploaded_at, parse_status")
        .eq("organisation_id", organisation_id)
        .eq("parse_status", "failed")
        .order("uploaded_at", desc=True)
        .limit(limit)
    )
    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "filename": row.get("file_name"),
            "uploaded_at": row.get("uploaded_at"),
            "age_days": _days_old(row.get("uploaded_at")),
        })
    return results


# ── Summary ───────────────────────────────────────────────────────────────────

def get_suspense_summary(db, organisation_id: str) -> dict[str, Any]:
    """Lightweight counts for each exception category (no line-level data)."""

    # Suspense account count: accounts with suspense/clearing keywords
    try:
        accounts = _rows(
            db.table("accounts")
            .select("id, name")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
        )
        suspense_candidate_count = sum(
            1 for a in accounts if _is_suspense_account(a.get("name", ""))
        )
    except Exception:
        suspense_candidate_count = 0

    # Unreconciled bank lines — count total active minus fully-reconciled
    try:
        total_active = (
            db.table("bank_statement_lines")
            .select("id", count="exact")
            .eq("organisation_id", organisation_id)
            .not_.in_("review_status", ["ignored", "deferred"])
            .execute()
        ).count or 0

        fully_reconciled = (
            db.table("bank_statement_lines")
            .select("id", count="exact")
            .eq("organisation_id", organisation_id)
            .eq("posting_status", "posted")
            .in_("allocation_status", ["allocated", "split"])
            .eq("review_status", "reviewed")
            .execute()
        ).count or 0

        unreconciled_count = max(0, total_active - fully_reconciled)
    except Exception:
        unreconciled_count = 0

    # Approved unposted invoices
    try:
        approved_unposted = (
            db.table("invoices_extracted")
            .select("id", count="exact")
            .eq("organisation_id", organisation_id)
            .eq("review_status", "approved")
            .neq("posting_status", "posted")
            .execute()
        ).count or 0
    except Exception:
        approved_unposted = 0

    # Extraction failures
    try:
        failed_extractions = (
            db.table("invoices_raw")
            .select("id", count="exact")
            .eq("organisation_id", organisation_id)
            .eq("parse_status", "failed")
            .execute()
        ).count or 0
    except Exception:
        failed_extractions = 0

    return {
        "suspense_accounts": suspense_candidate_count,
        "unreconciled_bank_lines": unreconciled_count,
        "approved_unposted_invoices": approved_unposted,
        "extraction_failures": failed_extractions,
        "total_exceptions": unreconciled_count + approved_unposted + failed_extractions,
    }
