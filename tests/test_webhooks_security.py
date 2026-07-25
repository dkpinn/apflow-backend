from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.routers import webhooks


class _Request:
    def __init__(self, *, form=None, body: bytes = b"{}", headers=None):
        self._form = form or {}
        self._body = body
        self.headers = headers or {}
        self.form_accessed = False
        self.body_accessed = False

    async def form(self):
        self.form_accessed = True
        return self._form

    async def body(self):
        self.body_accessed = True
        return self._body


def test_email_inbound_fails_closed_when_signing_key_missing(monkeypatch):
    monkeypatch.delenv("MAILGUN_WEBHOOK_SIGNING_KEY", raising=False)
    request = _Request()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.email_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 503
    assert request.form_accessed is False


def test_email_inbound_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("MAILGUN_WEBHOOK_SIGNING_KEY", "configured-secret")
    monkeypatch.setattr(webhooks, "verify_mailgun_signature", lambda *_args: False)
    request = _Request(form={"token": "token", "timestamp": "1", "signature": "bad"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.email_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 401


def test_whatsapp_inbound_fails_closed_when_app_secret_missing(monkeypatch):
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    request = _Request()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.whatsapp_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 503
    assert request.body_accessed is False


def test_whatsapp_inbound_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "configured-secret")
    monkeypatch.setattr(webhooks, "verify_meta_signature", lambda *_args: False)
    request = _Request(headers={"X-Hub-Signature-256": "sha256=bad"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.whatsapp_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 403
