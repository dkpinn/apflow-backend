from __future__ import annotations

import re


def _safe_q(q: str) -> str:
    """Strip chars that could break PostgREST ilike patterns."""
    return re.sub(r"[%_\\]", "", q.strip())[:100]


def _rows(query) -> list[dict]:
    return list(query.execute().data or [])


def search(db, organisation_id: str, query: str, limit: int = 6) -> dict:
    q = _safe_q(query)
    if len(q) < 2:
        return {"results": [], "query": q}

    pat = f"%{q}%"
    results: list[dict] = []

    # ── Suppliers ─────────────────────────────────────────────────────────────
    try:
        rows = _rows(
            db.table("suppliers")
            .select("id, name, supplier_code")
            .eq("organisation_id", organisation_id)
            .or_(f"name.ilike.{pat},supplier_code.ilike.{pat}")
            .limit(limit)
        )
        for r in rows:
            results.append({
                "type": "supplier",
                "id": r["id"],
                "title": r.get("name") or "Unnamed supplier",
                "subtitle": r.get("supplier_code"),
                "link": f"/suppliers/{r['id']}",
            })
    except Exception:
        pass

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
    except Exception:
        pass

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
    except Exception:
        pass

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
    except Exception:
        pass

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
    except Exception:
        pass

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
    except Exception:
        pass

    return {"results": results, "query": q}
