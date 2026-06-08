from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


class IntegrationSecretError(ValueError):
    """Raised when integration secret encryption/decryption is not configured."""


def _fernet() -> Fernet:
    secret = os.getenv("INTEGRATION_SECRET_KEY") or os.getenv("PLATFORM_INTEGRATION_SECRET_KEY")
    if not secret:
        raise IntegrationSecretError("INTEGRATION_SECRET_KEY is required to store integration secrets")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise IntegrationSecretError("Stored integration secret could not be decrypted") from exc


def secret_fingerprint(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def mask_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"
