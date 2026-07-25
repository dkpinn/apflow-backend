from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.request_size_limit import RequestSizeLimitMiddleware
from app.routers import webhooks


class _Request:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


class _Upload:
    def __init__(self, data: bytes, *, size=None, filename="invoice.pdf"):
        self.data = data
        self.size = len(data) if size is None else size
        self.filename = filename
        self.content_type = "application/pdf"
        self.offset = 0

    async def read(self, size=-1):
        if size < 0:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


def _signed(monkeypatch):
    monkeypatch.setenv("MAILGUN_WEBHOOK_SIGNING_KEY", "secret")
    monkeypatch.setattr(webhooks, "verify_mailgun_signature", lambda *_args: True)


def test_mailgun_rejects_malformed_attachment_count(monkeypatch):
    _signed(monkeypatch)
    request = _Request({"attachment-count": "not-a-number"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.email_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 400


def test_mailgun_rejects_too_many_attachments_before_database_access(monkeypatch):
    _signed(monkeypatch)
    monkeypatch.setattr(webhooks, "MAILGUN_MAX_ATTACHMENT_COUNT", 2)
    monkeypatch.setattr(
        webhooks,
        "get_supabase_client",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be accessed")),
    )
    request = _Request({"attachment-count": "3"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.email_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 413


def test_mailgun_rejects_declared_oversized_attachment(monkeypatch):
    _signed(monkeypatch)
    monkeypatch.setattr(webhooks, "MAILGUN_MAX_ATTACHMENT_BYTES", 4)
    upload = _Upload(b"x", size=5)
    request = _Request({"attachment-count": "1", "attachment-1": upload})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.email_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 413
    assert upload.offset == 0


def test_mailgun_stream_limit_does_not_trust_declared_size(monkeypatch):
    _signed(monkeypatch)
    monkeypatch.setattr(webhooks, "MAILGUN_MAX_ATTACHMENT_BYTES", 4)
    upload = _Upload(b"12345", size=1)
    request = _Request({"attachment-count": "1", "attachment-1": upload})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.email_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 413


def test_mailgun_cumulative_limit_prevents_partial_ingestion(monkeypatch):
    _signed(monkeypatch)
    monkeypatch.setattr(webhooks, "MAILGUN_MAX_ATTACHMENT_BYTES", 10)
    monkeypatch.setattr(webhooks, "MAILGUN_MAX_TOTAL_ATTACHMENT_BYTES", 5)
    ingested = []
    monkeypatch.setattr(webhooks, "ingest_email_attachment", lambda *_args, **_kwargs: ingested.append(True))
    request = _Request({
        "attachment-count": "2",
        "attachment-1": _Upload(b"123"),
        "attachment-2": _Upload(b"456"),
    })

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(webhooks.email_inbound(request, BackgroundTasks()))

    assert exc_info.value.status_code == 413
    assert ingested == []


def test_valid_mailgun_attachment_is_ingested(monkeypatch):
    _signed(monkeypatch)
    monkeypatch.setattr(webhooks, "get_supabase_client", lambda: object())
    monkeypatch.setattr(webhooks, "resolve_org_id", lambda *_args: "org-1")
    monkeypatch.setattr(webhooks, "resolve_member_user_id", lambda *_args: "user-1")

    calls = []

    async def ingest(*_args, **kwargs):
        calls.append(kwargs)
        return "raw-1"

    monkeypatch.setattr(webhooks, "ingest_email_attachment", ingest)
    request = _Request({
        "token": "t",
        "timestamp": "1",
        "signature": "s",
        "recipient": "org@example.com",
        "sender": "user@example.com",
        "attachment-count": "1",
        "attachment-1": _Upload(b"pdf"),
    })

    result = asyncio.run(webhooks.email_inbound(request, BackgroundTasks()))

    assert result["processed"] == 1
    assert calls[0]["file_bytes"] == b"pdf"


async def _run_middleware(*, limit, headers, bodies):
    messages = []
    incoming = [
        {"type": "http.request", "body": body, "more_body": i < len(bodies) - 1}
        for i, body in enumerate(bodies)
    ]

    async def receive():
        return incoming.pop(0)

    async def send(message):
        messages.append(message)

    async def downstream(_scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestSizeLimitMiddleware(
        downstream,
        limits={"/api/webhooks/email-inbound": limit},
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/webhooks/email-inbound",
        "headers": headers,
    }
    await middleware(scope, receive, send)
    return messages


def test_request_limit_rejects_oversized_content_length_before_reading_body():
    messages = asyncio.run(_run_middleware(
        limit=4,
        headers=[(b"content-length", b"5")],
        bodies=[b"12345"],
    ))
    assert messages[0]["status"] == 413


def test_request_limit_rejects_chunked_body_that_exceeds_limit():
    messages = asyncio.run(_run_middleware(
        limit=4,
        headers=[],
        bodies=[b"12", b"345"],
    ))
    assert messages[0]["status"] == 413


def test_request_limit_allows_body_at_limit():
    messages = asyncio.run(_run_middleware(
        limit=4,
        headers=[],
        bodies=[b"12", b"34"],
    ))
    assert messages[0]["status"] == 204
