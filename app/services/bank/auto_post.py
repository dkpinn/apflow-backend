"""Auto-post bank lines matched by rules with auto_post=True.

Called after lines are inserted during statement extraction.
Failures per-line are caught and logged; they never abort the upload.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.services.bank_statement_service import bank_rule_matches, money, new_uuid

logger = logging.getLogger(__name__)

MONEY_ZERO = Decimal("0")


def _compute_split_amounts(
    split_allocations: list[dict[str, Any]],
    total: Decimal,
) -> list[tuple[dict[str, Any], Decimal]]:
    """Return (split, resolved_amount) pairs, ensuring they sum to total."""
    results: list[tuple[dict[str, Any], Decimal]] = []
    remainder_split: dict[str, Any] | None = None
    allocated = MONEY_ZERO

    for split in split_allocations:
        split_type = split.get("type", "remainder")
        if split_type == "remainder":
            remainder_split = split
            continue
        value = Decimal(str(split.get("value") or 0))
        if split_type == "percent":
            amount = (total * value / Decimal("100")).quantize(Decimal("0.01"))
        else:  # "fixed"
            amount = value.quantize(Decimal("0.01"))
        allocated += amount
        results.append((split, amount))

    leftover = total - allocated
    if remainder_split is not None:
        results.append((remainder_split, leftover))
    elif results:
        # No explicit remainder — adjust last split to absorb rounding
        last_split, last_amt = results[-1]
        results[-1] = (last_split, last_amt + leftover)

    return results


def _journal_lines_for_split(
    *,
    organisation_id: str,
    bank_account_gl_id: str,
    splits_with_amounts: list[tuple[dict[str, Any], Decimal]],
    signed_amount: Decimal,
    description: str,
) -> list[dict[str, Any]]:
    """Build balanced GL journal lines for a split-allocation rule."""
    absolute = abs(signed_amount)
    is_inflow = signed_amount >= 0

    lines: list[dict[str, Any]] = []

    # Bank GL line (full amount)
    lines.append({
        "organisation_id": organisation_id,
        "account_id": bank_account_gl_id,
        "description": description,
        "debit_amount": float(absolute) if is_inflow else 0.0,
        "credit_amount": 0.0 if is_inflow else float(absolute),
        "tracking": {},
        "sort_order": 0,
    })

    # Allocation split lines
    for idx, (split, amount) in enumerate(splits_with_amounts, start=1):
        lines.append({
            "organisation_id": organisation_id,
            "account_id": str(split["account_id"]),
            "description": split.get("label") or description,
            "debit_amount": 0.0 if is_inflow else float(abs(amount)),
            "credit_amount": float(abs(amount)) if is_inflow else 0.0,
            "tracking": split.get("tracking") or {},
            "sort_order": idx,
        })

    return lines


def _journal_lines_single(
    *,
    organisation_id: str,
    bank_account_gl_id: str,
    allocation_account_id: str,
    signed_amount: Decimal,
    description: str,
    tracking: dict[str, Any],
) -> list[dict[str, Any]]:
    absolute = abs(signed_amount)
    is_inflow = signed_amount >= 0
    return [
        {
            "organisation_id": organisation_id,
            "account_id": bank_account_gl_id if is_inflow else allocation_account_id,
            "description": description,
            "debit_amount": float(absolute),
            "credit_amount": 0.0,
            "tracking": {} if is_inflow else tracking,
            "sort_order": 0,
        },
        {
            "organisation_id": organisation_id,
            "account_id": allocation_account_id if is_inflow else bank_account_gl_id,
            "description": description,
            "debit_amount": 0.0,
            "credit_amount": float(absolute),
            "tracking": tracking if is_inflow else {},
            "sort_order": 1,
        },
    ]


def auto_post_matched_lines(
    db,
    *,
    organisation_id: str,
    bank_account_id: str,
    line_ids: list[str],
) -> dict[str, Any]:
    """Auto-post bank lines that match rules with auto_post=True.

    Returns a summary dict with posted_count and skipped_count.
    Never raises — failures per line are caught and logged.
    """
    if not line_ids:
        return {"posted_count": 0, "skipped_count": 0}

    # Fetch auto-post rules for this org (sorted by priority ascending)
    try:
        rules = (
            db.table("bank_transaction_rules")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
            .eq("auto_post", True)
            .order("priority", desc=False)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.exception("auto_post: failed to fetch rules for org=%s", organisation_id)
        return {"posted_count": 0, "skipped_count": len(line_ids)}

    if not rules:
        return {"posted_count": 0, "skipped_count": 0}

    # Fetch the bank account's GL account ID (needed for the bank journal line)
    try:
        account_row = (
            db.table("bank_accounts")
            .select("gl_account_id")
            .eq("id", bank_account_id)
            .limit(1)
            .execute()
            .data
            or [{}]
        )[0]
        bank_gl_id = account_row.get("gl_account_id")
        if not bank_gl_id:
            logger.warning("auto_post: bank account %s has no gl_account_id — skipping all", bank_account_id)
            return {"posted_count": 0, "skipped_count": len(line_ids)}
    except Exception:
        logger.exception("auto_post: could not fetch bank account %s", bank_account_id)
        return {"posted_count": 0, "skipped_count": len(line_ids)}

    # Fetch the inserted lines
    try:
        lines = (
            db.table("bank_statement_lines")
            .select("*")
            .eq("organisation_id", organisation_id)
            .in_("id", line_ids)
            .eq("posting_status", "unposted")
            .eq("duplicate_status", "clear")
            .execute()
            .data
            or []
        )
    except Exception:
        logger.exception("auto_post: failed to fetch lines for org=%s", organisation_id)
        return {"posted_count": 0, "skipped_count": len(line_ids)}

    posted_count = 0
    skipped_count = 0
    block_messages: list[str] = []

    for line in lines:
        line_id = str(line["id"])
        try:
            # Find first matching auto-post rule
            matched_rule = next(
                (r for r in rules if bank_rule_matches(r, bank_account_id=bank_account_id, line=line)),
                None,
            )
            if not matched_rule:
                skipped_count += 1
                continue

            amount = money(line.get("signed_amount"))
            if abs(amount) == MONEY_ZERO:
                skipped_count += 1
                continue

            description = (line.get("description") or "Bank transaction").strip()
            split_allocations = matched_rule.get("split_allocations")

            if split_allocations and isinstance(split_allocations, list) and len(split_allocations) > 0:
                splits_with_amounts = _compute_split_amounts(split_allocations, abs(amount))
                journal_lines = _journal_lines_for_split(
                    organisation_id=organisation_id,
                    bank_account_gl_id=str(bank_gl_id),
                    splits_with_amounts=splits_with_amounts,
                    signed_amount=amount,
                    description=description,
                )
            elif matched_rule.get("gl_account_id"):
                journal_lines = _journal_lines_single(
                    organisation_id=organisation_id,
                    bank_account_gl_id=str(bank_gl_id),
                    allocation_account_id=str(matched_rule["gl_account_id"]),
                    signed_amount=amount,
                    description=description,
                    tracking=matched_rule.get("tracking") or {},
                )
            else:
                skipped_count += 1
                continue

            journal_id = new_uuid()
            total_debit = sum(Decimal(str(jl["debit_amount"])) for jl in journal_lines)
            total_credit = sum(Decimal(str(jl["credit_amount"])) for jl in journal_lines)

            journal = {
                "id": journal_id,
                "organisation_id": organisation_id,
                "source_type": "bank_transaction",
                "source_id": line_id,
                "journal_date": line.get("line_date"),
                "description": description,
                "status": "posted",
                "total_debit": float(total_debit),
                "total_credit": float(total_credit),
                "posted_at": None,  # populated by DB default or trigger if needed
            }
            db.table("gl_journals").insert(journal).execute()
            db.table("gl_journal_lines").insert(
                [{**jl, "gl_journal_id": journal_id} for jl in journal_lines]
            ).execute()

            line_patch = {
                "posting_status": "posted",
                "gl_journal_id": journal_id,
                "accepted_rule_id": str(matched_rule["id"]),
                "allocation_status": "allocated" if not split_allocations else "split",
                "review_status": "reviewed",
            }
            if matched_rule.get("supplier_id"):
                line_patch["supplier_id"] = str(matched_rule["supplier_id"])

            db.table("bank_statement_lines").update(line_patch).eq("id", line_id).execute()
            posted_count += 1
            logger.info(
                "auto_post: posted line %s via rule %s (%s)",
                line_id,
                matched_rule.get("id"),
                matched_rule.get("name"),
            )
        except Exception as exc:
            logger.exception("auto_post: failed to post line %s", line_id)
            skipped_count += 1
            # Collect unique human-readable block reasons from DB trigger errors.
            # postgrest APIError stores the payload dict as args[0].
            raw = exc.args[0] if exc.args else None
            if isinstance(raw, dict):
                msg = raw.get("message") or ""
            else:
                msg = str(exc)
            if msg and msg not in block_messages:
                block_messages.append(msg)

    return {"posted_count": posted_count, "skipped_count": skipped_count, "block_messages": block_messages}
