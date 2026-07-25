from __future__ import annotations

import os


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


MIB = 1024 * 1024

MAILGUN_MAX_REQUEST_BYTES = _positive_int_env("MAILGUN_MAX_REQUEST_BYTES", 30 * MIB)
MAILGUN_MAX_ATTACHMENT_COUNT = _positive_int_env("MAILGUN_MAX_ATTACHMENT_COUNT", 10)
MAILGUN_MAX_ATTACHMENT_BYTES = _positive_int_env("MAILGUN_MAX_ATTACHMENT_BYTES", 10 * MIB)
MAILGUN_MAX_TOTAL_ATTACHMENT_BYTES = _positive_int_env(
    "MAILGUN_MAX_TOTAL_ATTACHMENT_BYTES", 25 * MIB
)
WHATSAPP_MAX_REQUEST_BYTES = _positive_int_env("WHATSAPP_MAX_REQUEST_BYTES", 1 * MIB)


WEBHOOK_BODY_LIMITS = {
    "/api/webhooks/email-inbound": MAILGUN_MAX_REQUEST_BYTES,
    "/api/webhooks/mailgun-events": 1 * MIB,
    "/api/webhooks/whatsapp": WHATSAPP_MAX_REQUEST_BYTES,
}
