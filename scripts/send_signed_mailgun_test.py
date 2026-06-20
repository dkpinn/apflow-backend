#!/usr/bin/env python3
"""send_signed_mailgun_test.py

Generate a Mailgun-style HMAC signature and POST a multipart/form-data
webhook payload to a Mailgun inbound webhook endpoint.

Usage:
  python scripts/send_signed_mailgun_test.py
  python scripts/send_signed_mailgun_test.py --recipient inv-222222222222@mail.apflow.com
  python scripts/send_signed_mailgun_test.py --target-url https://rebirth-sporting-tying.ngrok-free.dev/api/webhooks/email-inbound
  python scripts/send_signed_mailgun_test.py --recipient inv-222222222222@mail.apflow.com --attach-file sample.pdf
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import mimetypes
from pathlib import Path
import os
import random
import string
import sys
import time
import uuid
import http.client
from urllib.parse import urlparse


DEFAULT_TARGET_URL = "http://127.0.0.1:8000/api/webhooks/email-inbound"
DEFAULT_RECIPIENT = "test@example.com"
DEFAULT_SENDER = "alice@example.com"


def read_signing_key(env_path: str = ".env") -> str | None:
    if "MAILGUN_WEBHOOK_SIGNING_KEY" in os.environ:
        return os.environ["MAILGUN_WEBHOOK_SIGNING_KEY"]
    env_file = Path(env_path)
    if not env_file.exists():
        return None
    with env_file.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("MAILGUN_WEBHOOK_SIGNING_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def make_signature(key: str, timestamp: str, token: str) -> str:
    return hmac.new(key.encode("utf-8"), (timestamp + token).encode("utf-8"), hashlib.sha256).hexdigest()


def _header_for_filename(filename: str) -> str:
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or "application/octet-stream"


def build_multipart(fields: dict[str, str], files: list[tuple[str, str, bytes, str]], boundary: str) -> bytes:
    CRLF = b"\r\n"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}".encode("utf-8"))
        body.extend(CRLF)
        body.extend(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"))
        body.extend(CRLF)
        body.extend(CRLF)
        body.extend(value.encode("utf-8"))
        body.extend(CRLF)

    for field_name, filename, file_bytes, content_type in files:
        body.extend(f"--{boundary}".encode("utf-8"))
        body.extend(CRLF)
        body.extend(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode("utf-8")
        )
        body.extend(CRLF)
        body.extend(f"Content-Type: {content_type}".encode("utf-8"))
        body.extend(CRLF)
        body.extend(CRLF)
        body.extend(file_bytes)
        body.extend(CRLF)

    body.extend(f"--{boundary}--".encode("utf-8"))
    body.extend(CRLF)
    return bytes(body)


def prepare_payload(recipient: str, sender: str, attach_file: str | None) -> tuple[dict[str, str], list[tuple[str, str, bytes, str]]]:
    token = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
    timestamp = str(int(time.time()))

    return_token = token
    return_timestamp = timestamp
    fields = {
        "recipient": recipient,
        "sender": sender,
        "subject": "Signed webhook test",
        "body-plain": "This is a signed webhook test.",
        "timestamp": timestamp,
        "token": token,
        "signature": "",
        "attachment-count": "0",
    }

    files: list[tuple[str, str, bytes, str]] = []
    if attach_file:
        with open(attach_file, "rb") as fh:
            file_bytes = fh.read()
        filename = os.path.basename(attach_file)
        content_type = _header_for_filename(filename)
        files.append(("attachment-1", filename, file_bytes, content_type))
        fields["attachment-count"] = "1"

    return fields, files


def parse_target_url(target_url: str) -> tuple[str, int, str, bool]:
    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("target-url must use http:// or https://")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return parsed.scheme == "https", port, host, path


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a signed Mailgun webhook payload for testing.")
    parser.add_argument("--target-url", default=DEFAULT_TARGET_URL, help="Webhook endpoint URL")
    parser.add_argument("--recipient", default=DEFAULT_RECIPIENT, help="Recipient address to match inbound_email")
    parser.add_argument("--sender", default=DEFAULT_SENDER, help="Sender address for member resolution")
    parser.add_argument("--attach-file", help="Optional attachment file path")
    args = parser.parse_args()

    signing_key = read_signing_key()
    if not signing_key:
        print("ERROR: MAILGUN_WEBHOOK_SIGNING_KEY not found in environment or .env", file=sys.stderr)
        return 2

    fields, files = prepare_payload(args.recipient, args.sender, args.attach_file)
    fields["signature"] = make_signature(signing_key, fields["timestamp"], fields["token"])

    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    body = build_multipart(fields, files, boundary)

    secure, port, host, path = parse_target_url(args.target_url)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "signed-mailgun-test/1.0",
    }

    try:
        if secure:
            conn = http.client.HTTPSConnection(host, port, timeout=20)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=20)
        conn.request("POST", path, body, headers)
        resp = conn.getresponse()
        print(f"STATUS: {resp.status} {resp.reason}")
        data = resp.read()
        try:
            print(data.decode("utf-8"))
        except Exception:
            print(data)
        conn.close()
        return 0
    except Exception as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
