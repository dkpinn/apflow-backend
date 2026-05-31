from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ExtractionWarning:
    code: str
    message: str
    severity: str = "warning"
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.evidence:
            payload["evidence"] = self.evidence
        return payload


def file_sha256(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def detect_source_format(filename: str, mime_type: Optional[str]) -> str:
    lower = (filename or "").lower()
    effective_mime = (mime_type or "").lower()
    if lower.endswith(".csv") or "csv" in effective_mime or effective_mime == "application/vnd.ms-excel":
        return "csv"
    if lower.endswith(".pdf") or effective_mime == "application/pdf":
        return "pdf"
    if effective_mime.startswith("image/") or lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "image"
    return "unknown"


def warning(code: str, message: str, *, severity: str = "warning", **evidence: Any) -> dict[str, Any]:
    return ExtractionWarning(code=code, message=message, severity=severity, evidence=evidence).as_dict()


def extraction_metadata(
    *,
    extractor_type: str,
    extractor_version: str,
    source_format: str,
    parser_strategy: str,
    confidence_score: Optional[float],
    warnings: list[dict[str, Any]],
    raw_preview: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "extractor_type": extractor_type,
        "extractor_version": extractor_version,
        "source_format": source_format,
        "parser_strategy": parser_strategy,
        "confidence_score": confidence_score,
        "warnings": warnings,
    }
    if raw_preview:
        payload["raw_preview"] = raw_preview[:4000]
    if extra:
        payload.update(extra)
    return payload
