from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class SupplierCreateRequest(BaseModel):
    organisation_id: str
    supplier_name: str
    supplier_code: Optional[str] = None
    account_number: Optional[str] = None
    tax_number: Optional[str] = None
    registration_number: Optional[str] = None
    currency: Optional[str] = None
    default_email: Optional[str] = None
    phone: Optional[str] = None
    vat_number: Optional[str] = None
    company_registration_number: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_branch_code: Optional[str] = None
    bank_swift_code: Optional[str] = None
    bank_country: Optional[str] = None
    delivery_address: Optional[str] = None
    postal_address: Optional[str] = None
    accounting_email: Optional[str] = None
    fax: Optional[str] = None
    cell: Optional[str] = None
    website: Optional[str] = None
    parse_line_items: Optional[bool] = None
    line_items_include_vat: Optional[bool] = None
    track_inventory: Optional[bool] = None
    use_uom_from_description: Optional[bool] = None
    default_expense_account: Optional[str] = None
    default_tracking: dict[str, str] = Field(default_factory=dict)
    default_vat_rate: Optional[float] = None
    invoice_extracted_id: Optional[str] = None
    invoice_raw_id: Optional[str] = None
    link_invoice: bool = True


class SupplierFromInvoiceRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: Optional[str] = None
    supplier_name: Optional[str] = None
    parse_line_items: Optional[bool] = None
    line_items_include_vat: Optional[bool] = None
    track_inventory: Optional[bool] = None
    use_uom_from_description: Optional[bool] = None
    default_expense_account: Optional[str] = None
    default_tracking: dict[str, str] = Field(default_factory=dict)
    default_vat_rate: Optional[float] = None
    link_invoice: bool = True


class SupplierLinkRequest(BaseModel):
    supplier_id: str
    invoice_extracted_id: Optional[str] = None
    invoice_raw_id: Optional[str] = None
    organisation_id: Optional[str] = None


class SupplierMatchProfileRequest(BaseModel):
    organisation_id: str
    invoice_total: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    supplier_name: Optional[str] = None
    vat_number: Optional[str] = None
    company_registration_number: Optional[str] = None
    account_number: Optional[str] = None
    bank_account_number: Optional[str] = None
    phone: Optional[str] = None
    default_email: Optional[str] = None
    accounting_email: Optional[str] = None


class SupplierStpSettingsRequest(BaseModel):
    organisation_id: str
    stp_enabled: bool
    stp_max_amount: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)


class SupplierStpSettingsResponse(BaseModel):
    supplier_id: str
    organisation_id: str
    stp_enabled: bool
    stp_max_amount: Optional[float] = None


class SupplierAllocationSettingsRequest(BaseModel):
    organisation_id: str
    default_expense_account: Optional[str] = None
    default_tracking: Optional[dict[str, str]] = None


class SupplierAllocationSettingsResponse(BaseModel):
    supplier_id: str
    organisation_id: str
    default_expense_account: Optional[str] = None
    default_tracking: dict[str, str] = Field(default_factory=dict)


class SupplierBranchCreateRequest(BaseModel):
    organisation_id: str
    supplier_id: str
    branch_name: str
    branch_code: Optional[str] = None
    vat_number: Optional[str] = None
    tax_number: Optional[str] = None
    company_registration_number: Optional[str] = None
    phone: Optional[str] = None
    default_email: Optional[str] = None
    website: Optional[str] = None
    delivery_address: Optional[str] = None
    postal_address: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_branch_code: Optional[str] = None
    bank_swift_code: Optional[str] = None
    invoice_extracted_id: Optional[str] = None
    link_invoice: bool = True


class SupplierBranchFromInvoiceRequest(BaseModel):
    invoice_extracted_id: str
    supplier_id: str
    branch_name: Optional[str] = None
    link_invoice: bool = True


class SupplierBranchLinkRequest(BaseModel):
    invoice_extracted_id: str
    supplier_branch_id: str
    supplier_id: Optional[str] = None
    organisation_id: Optional[str] = None


class SupplierBranchUnlinkRequest(BaseModel):
    invoice_extracted_id: str
    supplier_id: Optional[str] = None


class SupplierAllocationRuleSplitRequest(BaseModel):
    expense_account: Optional[str] = None
    tracking: dict[str, Any] = {}
    percent: float = 100
    note: Optional[str] = None
    sort_order: int = 0


class SupplierAllocationRuleRequest(BaseModel):
    organisation_id: str
    supplier_id: str
    name: str
    active: bool = True
    priority: int = 100
    document_scope: str = "all"
    match_type: str = "all_lines"
    match_field: str = "description"
    pattern: Optional[str] = None
    notes: Optional[str] = None
    source_invoice_extracted_id: Optional[str] = None
    splits: list[SupplierAllocationRuleSplitRequest] = []


class SupplierAllocationRuleUpdateRequest(BaseModel):
    name: Optional[str] = None
    active: Optional[bool] = None
    priority: Optional[int] = None
    document_scope: Optional[str] = None
    match_type: Optional[str] = None
    match_field: Optional[str] = None
    pattern: Optional[str] = None
    notes: Optional[str] = None
    splits: Optional[list[SupplierAllocationRuleSplitRequest]] = None


class SupplierAllocationRulesFromInvoiceRequest(BaseModel):
    invoice_extracted_id: str
    supplier_id: str
    line_item_ids: Optional[list[str]] = None
    document_scope: str = "all"
    priority: int = 100


class SupplierKycRequestCreate(BaseModel):
    organisation_id: str
    trigger_type: str = "new_supplier"
    status: str = "draft"
    notes: Optional[str] = None
    submitted_at: Optional[str] = None


class SupplierKycRequestUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    reviewer_notes: Optional[str] = None


class SupplierKycDocumentCreate(BaseModel):
    organisation_id: Optional[str] = None
    document_type: str
    document_label: Optional[str] = None
    storage_path: str
    file_name: str
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    notes: Optional[str] = None

