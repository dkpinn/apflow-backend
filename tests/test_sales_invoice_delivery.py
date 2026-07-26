"""Tests for channel-agnostic sales-invoice delivery (email + WhatsApp)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import authenticated_user
from app.routers import sales_invoices_actions as actions_router
from app.services import sales_invoice_delivery as delivery
from app.services import whatsapp_outbound


class _Insert:
    def __init__(self, sink, row):
        self.sink = sink
        self.row = row

    def execute(self):
        self.sink.append(self.row)
        return type("R", (), {"data": [self.row]})()


class _Table:
    def __init__(self, sink):
        self.sink = sink

    def insert(self, row):
        return _Insert(self.sink, row)


class _DB:
    def __init__(self):
        self.events = []

    def table(self, name):
        assert name == "sales_invoice_delivery_events"
        return _Table(self.events)


INVOICE = {
    "id": "inv-1",
    "organisation_id": "org-1",
    "invoice_number": "INV-0001",
    "document_type": "invoice",
}


def _record(store, key, value):
    store[key] = value
    return {"success": True}


def test_dispatch_routes_email_channel(monkeypatch):
    calls = {}
    monkeypatch.setattr(delivery, "render_sales_invoice_pdf", lambda inv, lines: b"%PDF-")
    monkeypatch.setattr(
        delivery,
        "send_sales_invoice_email",
        lambda db, **kw: _record(calls, "email", kw),
    )
    result = delivery.dispatch_sales_invoice(
        _DB(),
        invoice=INVOICE,
        lines=[],
        channel="email",
        recipient="buyer@example.com",
        actor_user_id="user-1",
    )
    assert result == {"success": True}
    assert calls["email"]["recipient_email"] == "buyer@example.com"
    assert calls["email"]["pdf_bytes"] == b"%PDF-"


def test_dispatch_routes_whatsapp_channel(monkeypatch):
    calls = {}
    monkeypatch.setattr(delivery, "render_sales_invoice_pdf", lambda inv, lines: b"%PDF-")
    monkeypatch.setattr(
        delivery,
        "send_sales_invoice_whatsapp",
        lambda db, **kw: _record(calls, "wa", kw),
    )
    delivery.dispatch_sales_invoice(
        _DB(),
        invoice=INVOICE,
        lines=[],
        channel="whatsapp",
        recipient="+27821234567",
        actor_user_id="user-1",
    )
    assert calls["wa"]["recipient_phone"] == "+27821234567"


def test_send_whatsapp_records_queued_then_sent(monkeypatch):
    monkeypatch.setenv("META_WHATSAPP_PHONE_NUMBER_ID", "pnid")
    monkeypatch.setenv("META_WHATSAPP_ACCESS_TOKEN", "token")
    monkeypatch.setattr(delivery, "upload_whatsapp_media", lambda *a, **k: "media-1")
    monkeypatch.setattr(delivery, "send_whatsapp_document", lambda *a, **k: "wamid.123")

    db = _DB()
    result = delivery.send_sales_invoice_whatsapp(
        db,
        invoice=INVOICE,
        pdf_bytes=b"%PDF-",
        recipient_phone="+27 82 123 4567",
        actor_user_id="user-1",
    )
    assert result["provider_message_id"] == "wamid.123"
    assert [e["event_type"] for e in db.events] == ["queued", "sent"]
    assert db.events[0]["channel"] == "whatsapp"
    assert db.events[0]["recipient_phone"] == "27821234567"  # digits only for Meta


def test_send_whatsapp_records_failure_then_reraises(monkeypatch):
    monkeypatch.setenv("META_WHATSAPP_PHONE_NUMBER_ID", "pnid")
    monkeypatch.setenv("META_WHATSAPP_ACCESS_TOKEN", "token")

    def _boom(*a, **k):
        raise RuntimeError("meta down")

    monkeypatch.setattr(delivery, "upload_whatsapp_media", _boom)

    db = _DB()
    with pytest.raises(RuntimeError):
        delivery.send_sales_invoice_whatsapp(
            db,
            invoice=INVOICE,
            pdf_bytes=b"%PDF-",
            recipient_phone="+27821234567",
            actor_user_id="user-1",
        )
    assert [e["event_type"] for e in db.events] == ["queued", "failed"]


def test_whatsapp_config_requires_env(monkeypatch):
    monkeypatch.delenv("META_WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("META_WHATSAPP_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError):
        whatsapp_outbound.whatsapp_outbound_config()


def _send_client(monkeypatch, detail):
    app = FastAPI()
    app.include_router(actions_router.router)
    app.dependency_overrides[authenticated_user] = lambda: ("user-1", _DB())
    monkeypatch.setattr(actions_router, "ensure_org_write", lambda *a: None)
    monkeypatch.setattr(actions_router, "_detail", lambda *a, **k: detail)
    return TestClient(app)


def test_send_endpoint_rejects_missing_whatsapp_phone(monkeypatch):
    detail = {"id": "inv-1", "status": "issued", "customer": {"phone": None}, "lines": []}
    client = _send_client(monkeypatch, detail)
    resp = client.post(
        "/api/sales-invoices/inv-1/send",
        json={"organisation_id": "org-1", "channel": "whatsapp"},
    )
    assert resp.status_code == 400
    assert "WhatsApp" in resp.json()["detail"]


def test_send_endpoint_rejects_missing_email(monkeypatch):
    detail = {"id": "inv-1", "status": "issued", "customer": {"default_email": None}, "lines": []}
    client = _send_client(monkeypatch, detail)
    resp = client.post(
        "/api/sales-invoices/inv-1/send",
        json={"organisation_id": "org-1", "channel": "email"},
    )
    assert resp.status_code == 400


def test_send_endpoint_rejects_non_issued(monkeypatch):
    detail = {"id": "inv-1", "status": "draft", "customer": {}, "lines": []}
    client = _send_client(monkeypatch, detail)
    resp = client.post(
        "/api/sales-invoices/inv-1/send",
        json={"organisation_id": "org-1", "channel": "email"},
    )
    assert resp.status_code == 409


def test_send_endpoint_whatsapp_happy_path(monkeypatch):
    detail = {"id": "inv-1", "status": "issued", "customer": {"phone": "+27821234567"}, "lines": []}
    client = _send_client(monkeypatch, detail)
    monkeypatch.setattr(
        actions_router,
        "dispatch_sales_invoice",
        lambda db, **kw: {"success": True, "provider_message_id": "wamid.9", "channel": kw["channel"]},
    )
    resp = client.post(
        "/api/sales-invoices/inv-1/send",
        json={"organisation_id": "org-1", "channel": "whatsapp"},
    )
    assert resp.status_code == 200
    assert resp.json()["channel"] == "whatsapp"
