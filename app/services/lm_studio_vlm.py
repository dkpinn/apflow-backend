from __future__ import annotations

import base64
import os
from typing import Any, Optional

import logging
import httpx

logger = logging.getLogger(__name__)

DEFAULT_LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_LM_STUDIO_MODEL = "local-model"
DEFAULT_LM_STUDIO_TIMEOUT_SECONDS = 120
DEFAULT_LM_STUDIO_MAX_TOKENS = 8192
LM_STUDIO_PAUSED_ENV = "LM_STUDIO_VLM_PAUSED"


def env_truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def lm_studio_enabled() -> bool:
    if env_truthy(LM_STUDIO_PAUSED_ENV, default=True):
        return False
    return env_truthy("LM_STUDIO_VLM_ENABLED")


def lm_studio_base_url(integration: Optional[dict[str, Any]] = None) -> str:
    return (
        (integration or {}).get("base_url")
        or os.getenv("LM_STUDIO_BASE_URL")
        or DEFAULT_LM_STUDIO_BASE_URL
    ).rstrip("/")


def lm_studio_timeout_seconds(config: Optional[dict[str, Any]] = None) -> int:
    return int((config or {}).get("timeout_seconds") or os.getenv("LM_STUDIO_TIMEOUT_SECONDS") or DEFAULT_LM_STUDIO_TIMEOUT_SECONDS)


def lm_studio_max_tokens(config: Optional[dict[str, Any]] = None) -> int:
    return int((config or {}).get("max_tokens") or os.getenv("LM_STUDIO_MAX_TOKENS") or DEFAULT_LM_STUDIO_MAX_TOKENS)


def lm_studio_api_key(api_key: Optional[str] = None) -> str:
    return api_key or os.getenv("LM_STUDIO_API_KEY", "lm-studio")


def data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def resolve_lm_studio_model(
    *,
    base_url: str,
    configured_model: Optional[str] = None,
    timeout: int = DEFAULT_LM_STUDIO_TIMEOUT_SECONDS,
) -> str:
    if configured_model:
        return configured_model
    try:
        response = httpx.get(f"{base_url}/models", timeout=min(timeout, 5))
        response.raise_for_status()
        models = response.json().get("data") or []
        first_model = next((row.get("id") for row in models if row.get("id")), None)
        if first_model:
            return str(first_model)
    except Exception:
        pass
    return DEFAULT_LM_STUDIO_MODEL


def lm_studio_chat_text(
    *,
    messages: list[dict[str, Any]],
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = DEFAULT_LM_STUDIO_TIMEOUT_SECONDS,
    max_tokens: int = DEFAULT_LM_STUDIO_MAX_TOKENS,
) -> dict[str, Any]:
    effective_base_url = (base_url or DEFAULT_LM_STUDIO_BASE_URL).rstrip("/")
    effective_model = resolve_lm_studio_model(
        base_url=effective_base_url,
        configured_model=model,
        timeout=timeout,
    )
    headers = {"Authorization": f"Bearer {lm_studio_api_key(api_key)}", "Content-Type": "application/json"}
    response = httpx.post(
        f"{effective_base_url}/chat/completions",
        headers=headers,
        json={
            "model": effective_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    logger.info(
        "[LM Studio] HTTP %s, body preview: %r",
        getattr(response, "status_code", "?"),
        str(getattr(response, "text", ""))[:300],
    )
    payload = response.json()
    try:
        text = payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        logger.warning("[LM Studio] Unexpected response shape, keys=%s, error=%r", list(payload.keys()), str(payload.get("error", ""))[:300])
        text = ""
    return {
        "text": text,
        "model": effective_model,
        "base_url": effective_base_url,
        "raw_response": payload,
    }


def lm_studio_vision_text(
    *,
    prompt: str,
    page_parts: list[tuple[bytes, str]],
    pdf_text_block: Optional[str] = None,
    pdf_text_intro: Optional[str] = None,
    integration: Optional[dict[str, Any]] = None,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    integration = integration or {}
    config = integration.get("config") or {}
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if pdf_text_block:
        content.append({
            "type": "text",
            "text": f"{pdf_text_intro or 'PDF TEXT:'}\n\n{pdf_text_block}",
        })
    for part_bytes, part_mime in page_parts:
        content.append({
            "type": "image_url",
            "image_url": {"url": data_url(part_bytes, part_mime)},
        })

    base_url = lm_studio_base_url(integration)
    timeout = lm_studio_timeout_seconds(config)
    model = integration.get("model") or os.getenv("LM_STUDIO_VLM_MODEL") or None
    return lm_studio_chat_text(
        messages=[{"role": "user", "content": content}],
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_tokens=lm_studio_max_tokens(config),
    )


def lm_studio_health() -> dict[str, Any]:
    base_url = lm_studio_base_url()
    timeout = lm_studio_timeout_seconds()
    configured_model = os.getenv("LM_STUDIO_VLM_MODEL") or None
    result: dict[str, Any] = {
        "enabled": lm_studio_enabled(),
        "base_url": base_url,
        "configured_model": configured_model,
        "detected_model": None,
        "models": [],
        "ok": False,
        "error": None,
    }
    try:
        response = httpx.get(f"{base_url}/models", timeout=min(timeout, 5))
        response.raise_for_status()
        models = response.json().get("data") or []
        model_ids = [str(row.get("id")) for row in models if row.get("id")]
        result["models"] = model_ids
        result["detected_model"] = configured_model or (model_ids[0] if model_ids else None)
        result["ok"] = bool(result["detected_model"])
    except Exception as exc:
        result["error_type"] = exc.__class__.__name__
        result["error"] = str(exc)[:1000]
    return result
