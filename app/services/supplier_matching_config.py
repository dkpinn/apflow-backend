from __future__ import annotations

from math import isfinite
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


DEFAULT_AUTO_LINK_MIN_MATCHES = 2


class AutoLinkAmountTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_amount: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    required_matches: int = Field(ge=1, le=4)


def safe_match_count(value: object, default: int = DEFAULT_AUTO_LINK_MIN_MATCHES) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    return min(4, max(1, parsed))


def normalise_amount_tiers(value: Any) -> list[AutoLinkAmountTier]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ValueError("auto_link_amount_tiers must be a list")

    tiers = [AutoLinkAmountTier.model_validate(item) for item in value]
    catch_all_count = sum(tier.max_amount is None for tier in tiers)
    if catch_all_count > 1:
        raise ValueError("auto_link_amount_tiers may contain only one catch-all tier")

    finite_limits = [tier.max_amount for tier in tiers if tier.max_amount is not None]
    if any(not isfinite(limit) for limit in finite_limits):
        raise ValueError("auto-link tier amounts must be finite")
    if len(finite_limits) != len(set(finite_limits)):
        raise ValueError("auto-link tier max_amount values must be unique")

    return sorted(
        tiers,
        key=lambda tier: tier.max_amount if tier.max_amount is not None else float("inf"),
    )


def serialise_amount_tiers(value: Any) -> list[dict]:
    return [tier.model_dump(mode="json") for tier in normalise_amount_tiers(value)]


def resolve_match_threshold(
    tiers: Any,
    invoice_total: float | None,
    fallback_min: int,
) -> int:
    fallback = safe_match_count(fallback_min)
    if invoice_total is None:
        return fallback

    try:
        total = float(invoice_total)
        if not isfinite(total):
            return fallback
        parsed_tiers = normalise_amount_tiers(tiers)
    except (TypeError, ValueError):
        return fallback

    for tier in parsed_tiers:
        if tier.max_amount is None or total <= tier.max_amount:
            return tier.required_matches
    return fallback


def fetch_org_matching_config(supabase, org_id: str) -> dict:
    try:
        result = (
            supabase.table("organisations")
            .select("supplier_auto_link_min_matches, auto_link_amount_tiers")
            .eq("id", org_id)
            .limit(1)
            .execute()
        )
        row = result.data[0] if result.data else {}
        try:
            tiers = serialise_amount_tiers(row.get("auto_link_amount_tiers") or [])
        except ValueError:
            tiers = []
        return {
            "min_matches": safe_match_count(row.get("supplier_auto_link_min_matches")),
            "amount_tiers": tiers,
        }
    except Exception:
        try:
            result = (
                supabase.table("organisations")
                .select("supplier_auto_link_min_matches")
                .eq("id", org_id)
                .limit(1)
                .execute()
            )
            row = result.data[0] if result.data else {}
            return {
                "min_matches": safe_match_count(row.get("supplier_auto_link_min_matches")),
                "amount_tiers": [],
            }
        except Exception:
            return {
                "min_matches": DEFAULT_AUTO_LINK_MIN_MATCHES,
                "amount_tiers": [],
            }
