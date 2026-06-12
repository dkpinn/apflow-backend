from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Callable, Optional

import httpx

from app.db.supabase_client import get_supabase_client
from app.services.integration_secrets import decrypt_secret
from app.services.integration_service import (
    DEFAULT_AI_POLICY_TASK,
    SYSTEM_INTEGRATIONS_TABLE,
    get_published_extraction_criteria,
    get_system_policy,
)
from app.services.invoice_extraction.vlm_parser import (
    DEFAULT_EXTRACTION_PROMPT,
    extract_with_gemini_diagnostic,
    normalise_vlm_json_response,
    preprocess_for_vlm,
    vlm_invoice_json_schema,
)

VLM_CAPABILITY = "vlm"
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-3-5-sonnet-latest",
    "openrouter": "google/gemini-2.5-flash",
}


def _env_gemini_fallback(file_bytes: bytes, mime_type: Optional[str]) -> dict:
    result = extract_with_gemini_diagnostic(file_bytes, mime_type)
    result["provider"] = "gemini"
    result["model"] = os.getenv("GEMINI_VLM_MODEL") or DEFAULT_MODELS["gemini"]
    result["source"] = "env"
    result["attempts"] = [
        {
            "provider": "gemini",
            "model": result["model"],
            "source": "env",
            "reason": result.get("reason"),
            "error": result.get("error"),
            "success": result.get("data") is not None,
        }
    ]
    return result


def _run_gemini_provider(
    *,
    integration: dict,
    api_key: str,
    file_bytes: bytes,
    mime_type: Optional[str],
    prompt: Optional[str],
) -> dict:
    return extract_with_gemini_diagnostic(
        file_bytes,
        mime_type,
        api_key=api_key,
        model=integration.get("model") or DEFAULT_MODELS["gemini"],
        prompt=prompt,
    )


def _data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_json_text(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def _normalise_provider_json(text: str) -> dict:
    return normalise_vlm_json_response(_extract_json_text(text))


def _openai_output_text(payload: dict) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    parts: list[str] = []
    for output in payload.get("output") or []:
        for content in output.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _openai_chat_output_text(payload: dict) -> str:
    """Parse a standard OpenAI/OpenRouter chat completions response."""
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        return ""


def _anthropic_output_text(payload: dict) -> str:
    parts = [
        str(item.get("text"))
        for item in payload.get("content") or []
        if item.get("type") == "text" and item.get("text")
    ]
    return "\n".join(parts)


def _run_openai_provider(
    *,
    integration: dict,
    api_key: str,
    file_bytes: bytes,
    mime_type: Optional[str],
    prompt: Optional[str],
) -> dict:
    effective_mime = (mime_type or "application/pdf").lower()
    page_parts = preprocess_for_vlm(file_bytes, effective_mime)
    schema = vlm_invoice_json_schema()
    content: list[dict] = [
        {
            "type": "input_text",
            "text": (
                f"{prompt or DEFAULT_EXTRACTION_PROMPT}\n\n"
                "Return only JSON matching the supplied schema. Do not include markdown."
            ).strip(),
        }
    ]
    for part_bytes, part_mime in page_parts:
        if "pdf" in part_mime:
            content.append({
                "type": "input_file",
                "filename": "invoice.pdf",
                "file_data": _data_url(part_bytes, "application/pdf"),
            })
        else:
            content.append({
                "type": "input_image",
                "image_url": _data_url(part_bytes, part_mime),
            })

    base_url = (integration.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    timeout = int((integration.get("config") or {}).get("timeout_seconds") or 60)
    response = httpx.post(
        f"{base_url}/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": integration.get("model") or DEFAULT_MODELS["openai"],
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "invoice_extraction",
                    "schema": schema,
                    "strict": False,
                }
            },
            "temperature": 0,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        return {"data": _normalise_provider_json(_openai_output_text(response.json())), "reason": None, "error": None}
    except Exception as exc:
        return {
            "data": None,
            "reason": "invalid_schema",
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:1000],
        }


def _run_anthropic_provider(
    *,
    integration: dict,
    api_key: str,
    file_bytes: bytes,
    mime_type: Optional[str],
    prompt: Optional[str],
) -> dict:
    effective_mime = (mime_type or "application/pdf").lower()
    page_parts = preprocess_for_vlm(file_bytes, effective_mime)
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"{prompt or DEFAULT_EXTRACTION_PROMPT}\n\n"
                "Return only JSON for the invoice extraction fields. "
                f"Use this JSON schema as the contract:\n{json.dumps(vlm_invoice_json_schema())}"
            ).strip(),
        }
    ]
    for part_bytes, part_mime in page_parts:
        if "pdf" in part_mime:
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(part_bytes).decode("ascii"),
                },
            })
        else:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part_mime,
                    "data": base64.b64encode(part_bytes).decode("ascii"),
                },
            })

    base_url = (integration.get("base_url") or "https://api.anthropic.com/v1").rstrip("/")
    timeout = int((integration.get("config") or {}).get("timeout_seconds") or 60)
    max_tokens = int((integration.get("config") or {}).get("max_tokens") or 8192)
    response = httpx.post(
        f"{base_url}/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": (integration.get("config") or {}).get("anthropic_version") or "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": integration.get("model") or DEFAULT_MODELS["anthropic"],
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        return {"data": _normalise_provider_json(_anthropic_output_text(response.json())), "reason": None, "error": None}
    except Exception as exc:
        return {
            "data": None,
            "reason": "invalid_schema",
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:1000],
        }


def _run_openrouter_provider(
    *,
    integration: dict,
    api_key: str,
    file_bytes: bytes,
    mime_type: Optional[str],
    prompt: Optional[str],
) -> dict:
    effective_mime = (mime_type or "application/pdf").lower()
    page_parts = preprocess_for_vlm(file_bytes, effective_mime)
    schema = vlm_invoice_json_schema()
    text_prompt = (
        f"{prompt or DEFAULT_EXTRACTION_PROMPT}\n\n"
        "Return only JSON matching this schema. No markdown.\n"
        f"Schema: {json.dumps(schema)}"
    ).strip()
    content: list[dict] = [{"type": "text", "text": text_prompt}]
    for part_bytes, part_mime in page_parts:
        encoded = base64.b64encode(part_bytes).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{part_mime};base64,{encoded}"},
        })
    base_url = (integration.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")
    timeout = int((integration.get("config") or {}).get("timeout_seconds") or 60)
    response = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": integration.get("model") or DEFAULT_MODELS["openrouter"],
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        return {
            "data": _normalise_provider_json(_openai_chat_output_text(response.json())),
            "reason": None,
            "error": None,
        }
    except Exception as exc:
        return {
            "data": None,
            "reason": "invalid_schema",
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:1000],
        }


def _provider_not_implemented(**_kwargs) -> dict:
    return {
        "data": None,
        "reason": "provider_adapter_not_implemented",
        "error": "Provider adapter is not implemented.",
    }


PROVIDER_RUNNERS: dict[str, Callable[..., dict]] = {
    "gemini": _run_gemini_provider,
    "openai": _run_openai_provider,
    "anthropic": _run_anthropic_provider,
    "openrouter": _run_openrouter_provider,
    "openai_compatible": _run_openrouter_provider,
}


def _active_system_integrations(db) -> list[dict]:
    rows = (
        db.table(SYSTEM_INTEGRATIONS_TABLE)
        .select("*")
        .eq("capability", VLM_CAPABILITY)
        .eq("enabled", True)
        .execute()
    ).data or []
    return rows


def _ordered_integrations(integrations: list[dict], policy: dict) -> list[dict]:
    configured_order = policy.get("ordered_integration_ids") or []
    by_id = {str(row.get("id")): row for row in integrations if row.get("id")}
    ordered = [by_id[item] for item in configured_order if item in by_id]
    ordered_ids = {str(row.get("id")) for row in ordered}
    ordered.extend([row for row in integrations if str(row.get("id")) not in ordered_ids])
    return ordered


def _criteria_prompt(db, task: str) -> Optional[str]:
    try:
        criteria = get_published_extraction_criteria(db, task)
    except Exception:
        return None
    if not criteria:
        return None
    return criteria.get("prompt_template")


def extract_with_vlm_fallback(
    file_bytes: bytes,
    mime_type: Optional[str] = None,
    *,
    organisation_id: Optional[str] = None,
    task: str = DEFAULT_AI_POLICY_TASK,
    supabase=None,
) -> dict:
    """
    Run platform-configured VLM providers in fallback order.

    If no database integration is available, keep the current env-backed Gemini
    behavior. That preserves existing deployments while the platform dashboard is
    being rolled out.
    """
    try:
        db = supabase or get_supabase_client()
        policy = get_system_policy(db, task)
        if policy.get("enabled") is False:
            return {
                "data": None,
                "reason": "system_policy_disabled",
                "error": f"{task} system policy is disabled.",
                "attempts": [],
            }
        integrations = _ordered_integrations(_active_system_integrations(db), policy)
    except Exception as exc:
        env_result = _env_gemini_fallback(file_bytes, mime_type)
        env_result["config_error"] = str(exc)[:500]
        return env_result

    if not integrations:
        env_result = _env_gemini_fallback(file_bytes, mime_type)
        if env_result.get("data") is not None:
            return env_result
        or_key = os.getenv("OPENROUTER_API_KEY")
        if or_key:
            or_model = os.getenv("OPENROUTER_VLM_MODEL") or DEFAULT_MODELS["openrouter"]
            try:
                or_result = _run_openrouter_provider(
                    integration={"model": or_model},
                    api_key=or_key,
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    prompt=None,
                )
                if or_result.get("data") is not None:
                    or_result.update({"provider": "openrouter", "model": or_model, "source": "env"})
                    return or_result
            except Exception as exc:
                env_result["openrouter_error"] = str(exc)[:500]
        return env_result

    prompt = _criteria_prompt(db, task)
    attempts: list[dict] = []
    last_result: dict = {
        "data": None,
        "reason": "no_provider_succeeded",
        "error": "No configured VLM provider returned usable extraction data.",
    }

    for integration in integrations:
        provider = (integration.get("provider") or "").lower()
        model = integration.get("model") or DEFAULT_MODELS.get(provider)
        runner = PROVIDER_RUNNERS.get(provider, _provider_not_implemented)
        started = time.monotonic()
        try:
            api_key = decrypt_secret(integration.get("encrypted_api_key"))
            if not api_key:
                result = {
                    "data": None,
                    "reason": "missing_api_key",
                    "error": f"{provider} integration has no API key.",
                }
            else:
                result = runner(
                    integration=integration,
                    api_key=api_key,
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    prompt=prompt,
                )
        except Exception as exc:
            result = {
                "data": None,
                "reason": "provider_exception",
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:1000],
            }

        elapsed_ms = int((time.monotonic() - started) * 1000)
        attempt = {
            "integration_id": integration.get("id"),
            "provider": provider,
            "model": model,
            "source": "system_integration",
            "success": result.get("data") is not None,
            "reason": result.get("reason"),
            "error": result.get("error"),
            "error_type": result.get("error_type"),
            "latency_ms": elapsed_ms,
        }
        attempts.append(attempt)
        last_result = dict(result)
        if result.get("data") is not None:
            last_result.update({
                "provider": provider,
                "model": model,
                "source": "system_integration",
                "integration_id": integration.get("id"),
                "attempts": attempts,
            })
            return last_result

    last_result.setdefault("provider", attempts[-1]["provider"] if attempts else None)
    last_result["attempts"] = attempts
    return last_result
