from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional


COLOR_TOKEN_KEYS = {
    "background",
    "surface",
    "surface_muted",
    "text",
    "text_muted",
    "border",
    "primary",
    "primary_text",
    "accent",
    "success",
    "warning",
    "danger",
}
TYPOGRAPHY_FAMILIES = {"system", "serif", "mono", "rounded"}
DENSITY_VALUES = {"compact", "comfortable", "spacious"}
RADIUS_VALUES = {"none", "sm", "md", "lg"}
SHADOW_VALUES = {"none", "sm", "md", "lg"}
HEADING_WEIGHTS = {400, 500, 600, 700}

HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class ThemeAccessError(Exception):
    """Raised when a user tries to select a theme they do not own."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(data: Any) -> Optional[dict]:
    return data[0] if data else None


def _normalise_colors(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}

    colors = {}
    for key, color in value.items():
        if key in COLOR_TOKEN_KEYS and isinstance(color, str) and HEX_COLOR_RE.match(color):
            colors[key] = color.lower()
    return colors


def _normalise_typography(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}

    typography = {}
    family = value.get("family")
    heading_weight = value.get("heading_weight")

    if family in TYPOGRAPHY_FAMILIES:
        typography["family"] = family
    if heading_weight in HEADING_WEIGHTS:
        typography["heading_weight"] = heading_weight

    return typography


def normalise_theme_tokens(tokens: Any) -> dict:
    """
    Keep theme data cosmetic by allowing only known design-token values.

    The frontend should map these tokens to CSS variables/classes. We do not
    serve arbitrary CSS, URLs, expressions, formulas, or JavaScript.
    """
    if not isinstance(tokens, dict):
        return {}

    normalised = {}

    colors = _normalise_colors(tokens.get("colors"))
    typography = _normalise_typography(tokens.get("typography"))
    density = tokens.get("density")
    radius = tokens.get("radius")
    shadow = tokens.get("shadow")

    if colors:
        normalised["colors"] = colors
    if typography:
        normalised["typography"] = typography
    if density in DENSITY_VALUES:
        normalised["density"] = density
    if radius in RADIUS_VALUES:
        normalised["radius"] = radius
    if shadow in SHADOW_VALUES:
        normalised["shadow"] = shadow

    return normalised


def serialise_theme(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "slug": row.get("slug"),
        "name": row.get("name"),
        "description": row.get("description"),
        "preview_image_url": row.get("preview_image_url"),
        "tokens": normalise_theme_tokens(row.get("tokens") or {}),
    }


def get_user_theme_preference(supabase, *, user_id: str) -> Optional[str]:
    row = _first(
        (
            supabase.table("user_theme_preferences")
            .select("active_theme_id")
            .eq("user_id", user_id)
            .execute()
        ).data
    )
    return row.get("active_theme_id") if row else None


def list_purchased_themes(supabase, *, user_id: str) -> dict:
    entitlements = (
        supabase.table("user_theme_entitlements")
        .select("theme_id")
        .eq("user_id", user_id)
        .execute()
    ).data or []
    theme_ids = [row["theme_id"] for row in entitlements if row.get("theme_id")]

    themes = []
    if theme_ids:
        rows = (
            supabase.table("themes")
            .select("id, slug, name, description, preview_image_url, tokens, is_active")
            .in_("id", theme_ids)
            .eq("is_active", True)
            .execute()
        ).data or []
        themes = [serialise_theme(row) for row in rows]

    purchased_ids = {theme["id"] for theme in themes}
    active_theme_id = get_user_theme_preference(supabase, user_id=user_id)
    if active_theme_id not in purchased_ids:
        active_theme_id = None

    return {
        "active_theme_id": active_theme_id,
        "themes": themes,
    }


def get_active_theme(supabase, *, user_id: str) -> Optional[dict]:
    purchased = list_purchased_themes(supabase, user_id=user_id)
    active_theme_id = purchased["active_theme_id"]
    if not active_theme_id:
        return None
    return next((theme for theme in purchased["themes"] if theme["id"] == active_theme_id), None)


def set_active_theme(supabase, *, user_id: str, theme_id: str) -> dict:
    purchased = list_purchased_themes(supabase, user_id=user_id)
    theme = next((theme for theme in purchased["themes"] if theme["id"] == theme_id), None)
    if not theme:
        raise ThemeAccessError("Theme is not available to this user")

    existing = _first(
        (
            supabase.table("user_theme_preferences")
            .select("user_id")
            .eq("user_id", user_id)
            .execute()
        ).data
    )
    payload = {
        "active_theme_id": theme_id,
        "updated_at": utc_now_iso(),
    }

    if existing:
        (
            supabase.table("user_theme_preferences")
            .update(payload)
            .eq("user_id", user_id)
            .execute()
        )
    else:
        (
            supabase.table("user_theme_preferences")
            .insert({
                "user_id": user_id,
                **payload,
            })
            .execute()
        )

    return theme
