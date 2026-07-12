from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


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


class ApproveExtractionRequest(BaseModel):
    organisation_id: UUID
    source_document_checked: bool = False
    transaction_count_checked: bool = False
    amounts_and_dates_checked: bool = False
    balances_checked: bool = False
    reviewer_note: Optional[str] = Field(default=None, max_length=1000)


class CreateGoldFileFromUploadRequest(BaseModel):
    organisation_id: UUID
    gold_json: dict[str, Any]
    document_id: Optional[str] = None
    bank: Optional[str] = None
    account_type: Optional[str] = None
    document_variant: Optional[str] = None


class ReviewLineRequest(BaseModel):
    organisation_id: UUID
    suggestion_id: Optional[UUID] = None
    gl_account_id: Optional[UUID] = None
    tracking: dict[str, Any] = Field(default_factory=dict)
    tax_treatment: Optional[str] = None
    supplier_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    narration: Optional[str] = Field(default=None, max_length=2000)
    create_rule: bool = False
    rule_name: Optional[str] = None
    rule_criteria: list[dict[str, Any]] = Field(default_factory=list)
    criteria_mode: str = "and"

    @model_validator(mode="after")
    def _one_contact_only(self) -> "ReviewLineRequest":
        # A line can be tagged to a supplier OR a customer, never both.
        if self.supplier_id is not None and self.customer_id is not None:
            raise ValueError("supplier_id and customer_id are mutually exclusive")
        return self


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
