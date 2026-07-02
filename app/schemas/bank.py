from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BankAccountCreate(BaseModel):
    organisation_id: UUID
    name: str
    institution_name: Optional[str] = None
    account_type: str = "bank"
    currency: str = "ZAR"
    account_number_mask: Optional[str] = None
    account_number_hash: Optional[str] = None
    gl_account_id: Optional[UUID] = None
    opening_balance: Optional[float] = None
    opening_balance_date: Optional[str] = None


class BankUploadCreate(BaseModel):
    organisation_id: UUID
    bank_account_id: UUID
    original_filename: str
    mime_type: Optional[str] = None
    storage_bucket: str = "statement-files"
    storage_path: str


class ExtractUploadRequest(BaseModel):
    organisation_id: UUID


class ReviewLineRequest(BaseModel):
    organisation_id: UUID
    suggestion_id: Optional[UUID] = None
    gl_account_id: Optional[UUID] = None
    tracking: dict[str, Any] = Field(default_factory=dict)
    tax_treatment: Optional[str] = None
    supplier_id: Optional[UUID] = None
    create_rule: bool = False
    rule_name: Optional[str] = None
    rule_criteria: list[dict[str, Any]] = Field(default_factory=list)
    criteria_mode: str = "and"


class DraftJournalRequest(BaseModel):
    organisation_id: UUID
    gl_account_id: UUID
    tracking: dict[str, Any] = Field(default_factory=dict)
    vat_rate: Optional[float] = None
    vat_account_id: Optional[UUID] = None
    description_override: Optional[str] = Field(default=None, max_length=2000)


class PostJournalRequest(BaseModel):
    organisation_id: UUID


class BulkDeleteLinesRequest(BaseModel):
    organisation_id: UUID
    line_ids: list[UUID]


class BulkDeleteUploadsRequest(BaseModel):
    organisation_id: UUID
    upload_ids: list[UUID]


class BankBalanceSummary(BaseModel):
    bank_statement_balance: Optional[float] = None
    calculated_imported_balance: Optional[float] = None
    current_tb_balance: Optional[float] = None
    latest_statement_upload_id: Optional[str] = None
    statement_period_to: Optional[str] = None
    latest_transaction_date: Optional[str] = None
    bank_balance_status: Literal["available", "unavailable"]
    imported_balance_status: Literal["available", "unavailable"]
    tb_balance_status: Literal["available", "gl_account_not_linked"]


class ParsingRuleCreate(BaseModel):
    organisation_id: UUID
    institution_name: Optional[str] = None
    account_type: Optional[str] = None
    parsing_hint: str
    active: bool = True


class ParsingRuleUpdate(BaseModel):
    organisation_id: UUID
    institution_name: Optional[str] = None
    account_type: Optional[str] = None
    parsing_hint: Optional[str] = None
    active: Optional[bool] = None
