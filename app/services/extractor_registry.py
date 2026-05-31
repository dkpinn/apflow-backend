from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.services.extraction_foundation import detect_source_format


@dataclass(frozen=True)
class ExtractorProfile:
    key: str
    version: str
    domain: str
    parser_family: str
    implemented: bool


@dataclass(frozen=True)
class ExtractorSelection:
    profile: ExtractorProfile
    source_format: str
    parser_strategy: str


EXTRACTOR_PROFILES: dict[str, ExtractorProfile] = {
    "supplier_invoice_v1": ExtractorProfile("supplier_invoice", "v1", "supplier_invoice", "existing_supplier_invoice_pipeline", True),
    "supplier_statement_v1": ExtractorProfile("supplier_statement", "v1", "supplier_statement", "existing_supplier_statement_pipeline", True),
    "bank_statement_v1": ExtractorProfile("bank_statement", "v1", "bank_cash", "bank_statement_pipeline", True),
    "credit_card_statement_v1": ExtractorProfile("credit_card_statement", "v1", "bank_cash", "credit_card_statement_pipeline", True),
    "loan_statement_v1": ExtractorProfile("loan_statement", "v1", "bank_cash", "loan_statement_pipeline", False),
    "investment_statement_v1": ExtractorProfile("investment_statement", "v1", "bank_cash", "investment_statement_pipeline", False),
    "wallet_statement_v1": ExtractorProfile("wallet_statement", "v1", "bank_cash", "wallet_statement_pipeline", False),
}


ACCOUNT_TYPE_TO_PROFILE = {
    "bank": "bank_statement_v1",
    "cash": "bank_statement_v1",
    "foreign_bank": "bank_statement_v1",
    "credit_card": "credit_card_statement_v1",
    "loan": "loan_statement_v1",
    "mortgage": "loan_statement_v1",
    "vehicle_finance": "loan_statement_v1",
    "investment": "investment_statement_v1",
    "call_account": "investment_statement_v1",
    "money_market": "investment_statement_v1",
    "paypal": "wallet_statement_v1",
    "paygate": "wallet_statement_v1",
    "crypto": "wallet_statement_v1",
}


def select_bank_cash_extractor(*, account_type: Optional[str], filename: str, mime_type: Optional[str]) -> ExtractorSelection:
    profile_key = ACCOUNT_TYPE_TO_PROFILE.get((account_type or "bank").lower(), "bank_statement_v1")
    profile = EXTRACTOR_PROFILES[profile_key]
    source_format = detect_source_format(filename, mime_type)
    if source_format == "csv":
        parser_strategy = "deterministic_csv"
    elif source_format == "pdf":
        parser_strategy = "pdf_text_blocks_then_vlm"
    elif source_format == "image":
        parser_strategy = "vlm_image"
    else:
        parser_strategy = "vlm_unknown"
    return ExtractorSelection(profile=profile, source_format=source_format, parser_strategy=parser_strategy)
