from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

from app.services.extraction_foundation import extraction_metadata, warning
from app.services.extractor_registry import select_bank_cash_extractor

from .common import MONEY_ZERO, money
from .csv_parser import parse_csv_statement
from .models import ParsedBankLine
from .pdf_parser import parse_text_statement
from .vlm_parser import parse_vlm_statement
from .xlsx_parser import parse_xlsx_statement


LEGACY_XLS_ERROR = "Legacy .xls bank statements are not supported; export the statement as .xlsx or .csv"


def _line_value(line: ParsedBankLine, key: str, default: Any = None) -> Any:
    return getattr(line, key, default)


def _balance_status(header: dict[str, Any], lines: list[ParsedBankLine]) -> str:
    opening = header.get("opening_balance")
    closing = header.get("closing_balance")
    if opening is None or closing is None:
        return "missing_balance"
    expected = money(opening) + sum((money(_line_value(line, "signed_amount", MONEY_ZERO)) for line in lines), MONEY_ZERO)
    return "balanced" if abs(expected - money(closing)) <= Decimal("0.01") else "closing_mismatch"


def _running_balance_status(header: dict[str, Any], lines: list[ParsedBankLine]) -> str:
    if not lines:
        return "no_lines"
    previous = money(header.get("opening_balance")) if header.get("opening_balance") is not None else None
    for line in lines:
        balance = _line_value(line, "balance_amount")
        if balance is None:
            continue
        current = money(balance)
        if previous is not None:
            expected = previous + money(_line_value(line, "signed_amount", MONEY_ZERO))
            if abs(expected - current) > Decimal("0.02"):
                return "balance_walk_failed"
        previous = current
    return "balanced"


def _pdf_rescue_reason(header: dict[str, Any], lines: list[ParsedBankLine]) -> Optional[str]:
    if not lines:
        return "no_transaction_lines"
    balance_status = _balance_status(header, lines)
    if balance_status != "balanced":
        return balance_status
    running_status = _running_balance_status(header, lines)
    if running_status != "balanced":
        return running_status
    return None


def _pdf_candidate_score(header: dict[str, Any], lines: list[ParsedBankLine]) -> int:
    score = min(len(lines), 200)
    if _balance_status(header, lines) == "balanced":
        score += 500
    if _running_balance_status(header, lines) == "balanced":
        score += 300
    if header.get("closing_balance") is not None:
        score += 50
    return score


def _record_pdf_rescue_metadata(
    header: dict[str, Any],
    *,
    attempted: bool,
    reason: Optional[str],
    selected: str,
    error: Optional[str] = None,
    deterministic_score: Optional[int] = None,
    vlm_score: Optional[int] = None,
) -> dict[str, Any]:
    metadata = {
        "attempted": attempted,
        "reason": reason,
        "selected": selected,
        "deterministic_score": deterministic_score,
        "vlm_score": vlm_score,
    }
    if error:
        metadata["error"] = error
    header["pdf_rescue"] = metadata
    raw_extraction = header.get("raw_extraction")
    if not isinstance(raw_extraction, dict):
        raw_extraction = {}
        header["raw_extraction"] = raw_extraction
    raw_extraction["pdf_rescue"] = metadata
    return header


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
            logger.info("[EXTRACT] Text parser found no lines — falling back to VLM")
            header, lines = parse_vlm_statement(
                file_bytes,
                mime_type=mime_type or "application/pdf",
                bank_account_id=bank_account_id,
                currency=currency,
                parsing_hint=parsing_hint,
            )
            header["parser_strategy"] = "pdf_text_blocks_then_vlm"
            _record_pdf_rescue_metadata(
                header,
                attempted=True,
                reason="no_transaction_lines",
                selected="vlm",
            )
        else:
            rescue_reason = _pdf_rescue_reason(header, lines)
            if rescue_reason:
                deterministic_header = header
                deterministic_lines = lines
                deterministic_score = _pdf_candidate_score(deterministic_header, deterministic_lines)
                try:
                    logger.info(
                        "[EXTRACT] Text parser extracted %d lines but needs rescue (%s); trying VLM",
                        len(lines),
                        rescue_reason,
                    )
                    vlm_header, vlm_lines = parse_vlm_statement(
                        file_bytes,
                        mime_type=mime_type or "application/pdf",
                        bank_account_id=bank_account_id,
                        currency=currency,
                        parsing_hint=parsing_hint,
                    )
                    vlm_score = _pdf_candidate_score(vlm_header, vlm_lines)
                    if vlm_lines and vlm_score > deterministic_score:
                        header, lines = vlm_header, vlm_lines
                        header["parser_strategy"] = "pdf_text_blocks_then_vlm"
                        _record_pdf_rescue_metadata(
                            header,
                            attempted=True,
                            reason=rescue_reason,
                            selected="vlm",
                            deterministic_score=deterministic_score,
                            vlm_score=vlm_score,
                        )
                    else:
                        header, lines = deterministic_header, deterministic_lines
                        _record_pdf_rescue_metadata(
                            header,
                            attempted=True,
                            reason=rescue_reason,
                            selected="deterministic",
                            deterministic_score=deterministic_score,
                            vlm_score=vlm_score,
                        )
                except Exception as exc:
                    header, lines = deterministic_header, deterministic_lines
                    warnings = list(header.get("extraction_warnings") or [])
                    warnings.append(
                        warning(
                            "pdf_vlm_rescue_failed",
                            "Deterministic PDF extraction needed VLM rescue, but no VLM provider completed successfully.",
                            reason=rescue_reason,
                            error=str(exc),
                        )
                    )
                    header["extraction_warnings"] = warnings
                    _record_pdf_rescue_metadata(
                        header,
                        attempted=True,
                        reason=rescue_reason,
                        selected="deterministic",
                        error=str(exc),
                        deterministic_score=deterministic_score,
                    )
            else:
                _record_pdf_rescue_metadata(
                    header,
                    attempted=False,
                    reason=None,
                    selected="deterministic",
                    deterministic_score=_pdf_candidate_score(header, lines),
                )
                logger.info("[EXTRACT] Text parser extracted %d reconciled lines", len(lines))
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
