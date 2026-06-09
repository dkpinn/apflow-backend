from __future__ import annotations

import base64
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import UserAuth, ensure_org_read
from app.services.document_autofill.field_detection import detect_fields_with_gemini

router = APIRouter(prefix="/api/document-autofill", tags=["document-autofill"])


class DetectFieldsRequest(BaseModel):
    organisation_id: str
    page_index: int = Field(ge=0)
    image_base64: str = Field(min_length=10)
    mime_type: str = Field(default="image/jpeg")
    text_hint: Optional[str] = Field(default=None, max_length=4000)


class DetectFieldsResponse(BaseModel):
    fields: list[Any] = Field(default_factory=list)
    error: Optional[str] = None


@router.post("/detect-fields", response_model=DetectFieldsResponse)
async def detect_fields(payload: DetectFieldsRequest, auth: UserAuth):
    user_id, _db = auth
    ensure_org_read(str(user_id), payload.organisation_id)

    try:
        image_bytes = base64.b64decode(payload.image_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="image_base64 is not valid base64")

    result = await detect_fields_with_gemini(
        image_bytes,
        mime_type=payload.mime_type,
        text_hint=payload.text_hint,
    )
    return result
