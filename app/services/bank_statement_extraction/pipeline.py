from __future__ import annotations

from typing import Any, Optional

from app.services.extraction_foundation import extraction_metadata
from app.services.extractor_registry import select_bank_cash_extractor

from .csv_parser import parse_csv_statement
from .models import ParsedBankLine
from .pdf_parser import parse_text_statement
from .vlm_parser import parse_vlm_statement
from .xlsx_parser import parse_xlsx_statement


LEGACY_XLS_ERROR = "Legacy .xls bank statements are not supported; export the statement as .xlsx or .csv"


def stamp_extractor_selection(
    header: dict[str, Any],
    *,
    extractor_type: str,
    extractor_version: str,
    source_format: str,
    parser_strategy: str,
) -> dict[str, Any]:
    warnings = list(header.get("extraction_warnings") or [])
    header["extractor"] = extractor_type
    header["extractor_type"] = extractor_type
    header["extractor_version"] = extractor_version
    header["source_format"] = source_format
    header["parser_strategy"] = header.get("parser_strategy") or parser_strategy
    header["extraction_warnings"] = warnings
    raw_extraction = header.get("raw_extraction")
    if not raw_extraction:
        raw_extraction = extraction_metadata(
            extractor_type=extractor_type,
            extractor_version=extractor_version,
            source_format=source_format,
            parser_strategy=header["parser_strategy"],
            confidence_score=header.get("confidence_score"),
            warnings=warnings,
        )
    raw_extraction["extractor_type"] = extractor_type
    raw_extraction["extractor_version"] = extractor_version
    raw_extraction["source_format"] = source_format
    raw_extraction["parser_strategy"] = header["parser_strategy"]
    header["raw_extraction"] = raw_extraction
    return header


def extract_statement(
    file_bytes: bytes,
    *,
    filename: str,
    mime_type: str,
    bank_account_id: str,
    currency: Optional[str] = None,
    account_type: Optional[str] = None,
    parsing_hint: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    selection = select_bank_cash_extractor(
        account_type=account_type,
        filename=filename,
        mime_type=mime_type,
    )
    if not selection.profile.implemented:
        raise ValueError(
            f"Extractor profile {selection.profile.key}_{selection.profile.version} is registered but not implemented yet"
        )
    if selection.source_format == "xls":
        raise ValueError(LEGACY_XLS_ERROR)

    if selection.source_format == "csv":
        header, lines = parse_csv_statement(
            file_bytes,
            bank_account_id=bank_account_id,
            currency=currency,
        )
    elif selection.source_format == "xlsx":
        header, lines = parse_xlsx_statement(
            file_bytes,
            bank_account_id=bank_account_id,
            currency=currency,
        )
    elif selection.source_format == "pdf":
        header, lines = parse_text_statement(
            file_bytes,
            bank_account_id=bank_account_id,
            currency=currency,
        )
        if not lines:
            header, lines = parse_vlm_statement(
                file_bytes,
                mime_type=mime_type or "application/pdf",
                bank_account_id=bank_account_id,
                currency=currency,
                parsing_hint=parsing_hint,
            )
    else:
        header, lines = parse_vlm_statement(
            file_bytes,
            mime_type=mime_type or "application/pdf",
            bank_account_id=bank_account_id,
            currency=currency,
            parsing_hint=parsing_hint,
        )

    return stamp_extractor_selection(
        header,
        extractor_type=selection.profile.key,
        extractor_version=selection.profile.version,
        source_format=selection.source_format,
        parser_strategy=selection.parser_strategy,
    ), lines
