from __future__ import annotations

import re
import logging


logger = logging.getLogger(__name__)
SEARCH_SOURCES = (
    "suppliers",
    "customers",
    "supplier_invoices",
    "sales_invoices",
    "accounts",
    "bank_lines",
)


def _safe_q(q: str) -> str:
    """Strip chars that could break PostgREST ilike patterns."""
    return re.sub(r"[%_\\]", "", q.strip())[:100]


def _rows(query) -> list[dict]:
    return list(query.execute().data or [])


def search(db, organisation_id: str, query: str, limit: int = 6) -> dict:
    q = _safe_q(query)
    if len(q) < 2:
        return {
            "results": [],
            "query": q,
            "is_partial": False,
            "errors": [],
            "searched_sources": [],
            "failed_sources": [],
        }

    pat = f"%{q}%"
    results: list[dict] = []
    errors: list[dict[str, str]] = []
    searched_sources: list[str] = []

    def source_failed(source: str, label: str, exc: Exception) -> None:
        logger.warning("Global search source %s failed: %s", source, exc, exc_info=True)
        errors.append({
            "source": source,
            "code": "source_unavailable",
            "message": f"{label} search is temporarily unavailable",
        })

    # ── Suppliers ─────────────────────────────────────────────────────────────
    try:
        rows = _rows(
            db.table("suppliers")
            .select("id, supplier_name, trading_name, supplier_code")
            .eq("organisation_id", organisation_id)
            .or_(f"supplier_name.ilike.{pat},trading_name.ilike.{pat},supplier_code.ilike.{pat}")
            .limit(limit)
        )
        for r in rows:
            results.append({
                "type": "supplier",
                "id": r["id"],
                "title": r.get("supplier_name") or r.get("trading_name") or "Unnamed supplier",
                "subtitle": r.get("supplier_code"),
                "link": f"/suppliers/{r['id']}",
            })
        searched_sources.append("suppliers")
    except Exception as exc:
        source_failed("suppliers", "Supplier", exc)

    # ── Customers ─────────────────────────────────────────────────────────────
    try:
        rows = _rows(
            db.table("customers")
            .select("id, name, customer_code")
            .eq("organisation_id", organisation_id)
            .or_(f"name.ilike.{pat},customer_code.ilike.{pat}")
            .limit(limit)
        )
        for r in rows:
            results.append({
                "type": "customer",
                "id": r["id"],
                "title": r.get("name") or "Unnamed customer",
                "subtitle": r.get("customer_code"),
                "link": f"/customers/{r['id']}",
            })
        searched_sources.append("customers")
    except Exception as exc:
        source_failed("customers", "Customer", exc)

    # ── Supplier invoices ─────────────────────────────────────────────────────
    try:
        rows = _rows(
            db.table("invoices_extracted")
            .select("id, invoice_number, supplier_name_extracted, total_amount, invoice_date")
            .eq("organisation_id", organisation_id)
            .or_(f"invoice_number.ilike.{pat},supplier_name_extracted.ilike.{pat}")
            .order("invoice_date", desc=True)
            .limit(limit)
        )
        for r in rows:
            supplier = r.get("supplier_name_extracted") or "Unknown supplier"
            inv_num = r.get("invoice_number") or r["id"][:8]
            results.append({
                "type": "invoice",
                "id": r["id"],
                "title": f"Invoice {inv_num}",
                "subtitle": supplier,
                "link": f"/invoices/{r['id']}",
            })
        searched_sources.append("supplier_invoices")
    except Exception as exc:
        source_failed("supplier_invoices", "Supplier invoice", exc)

    # ── Sales invoices ────────────────────────────────────────────────────────
    try:
        rows = _rows(
            db.table("sales_invoices")
            .select("id, invoice_number, customer_name, total_amount, invoice_date")
            .eq("organisation_id", organisation_id)
            .or_(f"invoice_number.ilike.{pat},customer_name.ilike.{pat}")
            .order("invoice_date", desc=True)
            .limit(limit)
        )
        for r in rows:
            customer = r.get("customer_name") or "Unknown customer"
            inv_num = r.get("invoice_number") or r["id"][:8]
            results.append({
                "type": "sales_invoice",
                "id": r["id"],
                "title": f"Sales Invoice {inv_num}",
                "subtitle": customer,
                "link": f"/sales-invoices/{r['id']}",
            })
        searched_sources.append("sales_invoices")
    except Exception as exc:
        source_failed("sales_invoices", "Sales invoice", exc)

    # ── Accounts (chart of accounts) ──────────────────────────────────────────
    try:
        rows = _rows(
            db.table("accounts")
            .select("id, code, name, type")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
            .or_(f"name.ilike.{pat},code.ilike.{pat}")
            .limit(limit)
        )
        for r in rows:
            results.append({
                "type": "account",
                "id": r["id"],
                "title": r.get("name") or "Unnamed account",
                "subtitle": r.get("code"),
                "link": f"/reports/general-ledger?account_id={r['id']}",
            })
        searched_sources.append("accounts")
    except Exception as exc:
        source_failed("accounts", "Account", exc)

    # ── Bank statement lines ──────────────────────────────────────────────────
    try:
        rows = _rows(
            db.table("bank_statement_lines")
            .select("id, description, counterparty, signed_amount, line_date")
            .eq("organisation_id", organisation_id)
            .or_(f"description.ilike.{pat},counterparty.ilike.{pat}")
            .order("line_date", desc=True)
            .limit(limit)
        )
        for r in rows:
            desc = r.get("description") or r.get("counterparty") or "Bank line"
            results.append({
                "type": "bank_line",
                "id": r["id"],
                "title": desc[:60],
                "subtitle": r.get("line_date"),
                "link": "/bank",
            })
        searched_sources.append("bank_lines")
    except Exception as exc:
        source_failed("bank_lines", "Bank line", exc)

    return {
        "results": results,
        "query": q,
        "is_partial": bool(errors),
        "errors": errors,
        "searched_sources": searched_sources,
        "failed_sources": [error["source"] for error in errors],
    }
