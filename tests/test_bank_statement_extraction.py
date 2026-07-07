from datetime import date, datetime
from io import BytesIO
import sys
import types

import pytest
from openpyxl import Workbook

import app.services.bank_statement_extraction as extraction
from app.services.bank_statement_extraction import pipeline as extraction_pipeline
import app.services.bank_statement_service as facade
from app.services.bank_statement_extraction import vlm_parser as bank_vlm_parser
from app.services.bank_statement_extraction.vlm_parser import (
    _parse_vlm_json_payload,
    _parse_vlm_transaction_date,
    _vlm_statement_period_dates,
)
from app.services.invoice_extraction import vlm_parser as invoice_vlm_parser
from app.services.extraction_foundation import detect_source_format
from app.services.extractor_registry import select_bank_cash_extractor


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _workbook_bytes(workbook: Workbook) -> bytes:
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_xlsx_debit_credit_rows_support_native_dates_numbers_and_hashes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Transactions"
    sheet.append(["Account statement"])
    sheet.append([])
    sheet.append(["Date", "Value Date", "Description", "Reference", "Debit", "Credit", "Balance"])
    sheet.append([date(2026, 1, 1), datetime(2026, 1, 2, 9, 30), "Supplier payment", "INV-100", 100, None, 900])
    sheet.append([date(2026, 1, 3), None, "Customer receipt", "RCPT-1", None, 250.5, 1150.5])

    header, lines = extraction.parse_xlsx_statement(
        _workbook_bytes(workbook),
        bank_account_id="bank-1",
        currency="ZAR",
    )

    assert header["source_format"] == "xlsx"
    assert header["parser_strategy"] == "deterministic_xlsx"
    assert header["opening_balance"] == 1000.0
    assert header["closing_balance"] == 1150.5
    assert header["raw_extraction"]["sheet_name"] == "Transactions"
    assert header["raw_extraction"]["header_row"] == 3
    assert lines[0].line_date == "2026-01-01"
    assert lines[0].value_date == "2026-01-02"
    assert lines[0].signed_amount == facade.money("-100")
    assert lines[1].signed_amount == facade.money("250.50")
    assert lines[0].transaction_hash


def test_xlsx_signed_amount_reuses_csv_normalization_and_fingerprint():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Date", "Description", "Reference", "Amount", "Balance"])
    sheet.append([date(2026, 2, 1), "Monthly fee", "FEE-1", -50, 950])
    xlsx_bytes = _workbook_bytes(workbook)
    csv_bytes = b"Date,Description,Reference,Amount,Balance\n2026-02-01,Monthly fee,FEE-1,-50,950\n"

    _xlsx_header, xlsx_lines = extraction.parse_xlsx_statement(xlsx_bytes, bank_account_id="bank-1")
    _csv_header, csv_lines = extraction.parse_csv_statement(csv_bytes, bank_account_id="bank-1")

    assert xlsx_lines[0].debit_amount == facade.money("50")
    assert xlsx_lines[0].credit_amount == facade.money("0")
    assert xlsx_lines[0].transaction_hash == csv_lines[0].transaction_hash


def test_xlsx_selects_strongest_visible_sheet_and_uses_workbook_order_for_ties():
    workbook = Workbook()
    hidden = workbook.active
    hidden.title = "Hidden"
    hidden.sheet_state = "hidden"
    hidden.append(["Date", "Description", "Reference", "Debit", "Credit", "Balance", "Currency"])
    hidden.append([date(2026, 1, 1), "Hidden row", "H-1", None, 1, 1, "ZAR"])

    first = workbook.create_sheet("First")
    first.append(["Date", "Description", "Amount"])
    first.append([date(2026, 1, 1), "Weak row", 10])

    second = workbook.create_sheet("Second")
    second.append(["Statement title"])
    second.append(["Date", "Description", "Reference", "Debit", "Credit", "Balance"])
    second.append([date(2026, 1, 2), "Selected row", "S-1", None, 20, 1020])

    tied = workbook.create_sheet("Tied")
    tied.append(["Date", "Description", "Reference", "Debit", "Credit", "Balance"])
    tied.append([date(2026, 1, 3), "Later tied row", "T-1", None, 30, 1050])

    header, lines = extraction.parse_xlsx_statement(
        _workbook_bytes(workbook),
        bank_account_id="bank-1",
    )

    assert header["raw_extraction"]["sheet_name"] == "Second"
    assert [line.description for line in lines] == ["Selected row"]


def test_xlsx_skips_empty_rows_and_rejects_invalid_workbooks():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Date", "Description", "Amount"])
    sheet.append([])
    sheet.append([date(2026, 3, 1), "Deposit", 125])

    _header, lines = extraction.parse_xlsx_statement(
        _workbook_bytes(workbook),
        bank_account_id="bank-1",
    )
    assert len(lines) == 1

    with pytest.raises(ValueError, match="Could not read XLSX"):
        extraction.parse_xlsx_statement(b"not an xlsx workbook", bank_account_id="bank-1")

    no_header = Workbook()
    no_header.active.append(["Monthly bank statement"])
    no_header.active.append(["Nothing", "Recognizable"])
    with pytest.raises(ValueError, match="recognizable transaction header"):
        extraction.parse_xlsx_statement(_workbook_bytes(no_header), bank_account_id="bank-1")


def test_xlsx_detection_routing_and_legacy_xls_rejection():
    assert detect_source_format("statement.xlsx", None) == "xlsx"
    assert detect_source_format("statement", XLSX_MIME) == "xlsx"
    assert detect_source_format("statement.xls", "application/vnd.ms-excel") == "xls"
    assert detect_source_format("statement.csv", "application/vnd.ms-excel") == "csv"

    selection = select_bank_cash_extractor(
        account_type="bank",
        filename="statement.xlsx",
        mime_type=XLSX_MIME,
    )
    assert selection.source_format == "xlsx"
    assert selection.parser_strategy == "deterministic_xlsx"

    with pytest.raises(ValueError, match=r"export the statement as \.xlsx or \.csv"):
        extraction.extract_statement(
            b"legacy workbook",
            filename="statement.xls",
            mime_type="application/vnd.ms-excel",
            bank_account_id="bank-1",
        )


def test_extract_statement_routes_xlsx_without_changing_response_shape():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Date", "Description", "Amount"])
    sheet.append([date(2026, 4, 1), "Receipt", 75])

    header, lines = facade.extract_statement(
        _workbook_bytes(workbook),
        filename="statement.xlsx",
        mime_type=XLSX_MIME,
        bank_account_id="bank-1",
        currency="ZAR",
    )

    assert header["source_format"] == "xlsx"
    assert header["parser_strategy"] == "deterministic_xlsx"
    assert header["extractor_type"] == "bank_statement"
    assert len(lines) == 1


def test_pdf_extract_uses_vlm_rescue_when_text_candidate_fails_balance(monkeypatch):
    bad_header, bad_lines = facade.parse_csv_statement(
        (
            "Date,Description,Amount,Balance\n"
            "2026-01-01,Payment,-100.00,900.00\n"
            "2026-01-02,Shifted amount,-999.00,850.00\n"
        ).encode(),
        bank_account_id="bank-1",
    )
    bad_header["parser_strategy"] = "pdf_text_blocks"
    good_header, good_lines = facade.parse_csv_statement(
        (
            "Date,Description,Amount,Balance\n"
            "2026-01-01,Payment,-100.00,900.00\n"
            "2026-01-02,Correct amount,-50.00,850.00\n"
        ).encode(),
        bank_account_id="bank-1",
    )
    good_header["parser_strategy"] = "vlm"

    monkeypatch.setattr(extraction_pipeline, "parse_text_statement", lambda *_a, **_kw: (bad_header, bad_lines))
    monkeypatch.setattr(extraction_pipeline, "parse_vlm_statement", lambda *_a, **_kw: (good_header, good_lines))

    header, lines = extraction_pipeline.extract_statement(
        b"pdf",
        filename="statement.pdf",
        mime_type="application/pdf",
        bank_account_id="bank-1",
    )

    assert header["parser_strategy"] == "pdf_text_blocks_then_vlm"
    assert header["pdf_rescue"]["selected"] == "vlm"
    assert header["pdf_rescue"]["reason"] == "closing_mismatch"
    assert [line.signed_amount for line in lines] == [facade.money("-100"), facade.money("-50")]


def test_pdf_extract_keeps_balanced_text_candidate_without_vlm(monkeypatch):
    header_fixture, line_fixture = facade.parse_csv_statement(
        (
            "Date,Description,Amount,Balance\n"
            "2026-01-01,Payment,-100.00,900.00\n"
            "2026-01-02,Correct amount,-50.00,850.00\n"
        ).encode(),
        bank_account_id="bank-1",
    )
    header_fixture["parser_strategy"] = "pdf_text_blocks"

    monkeypatch.setattr(extraction_pipeline, "parse_text_statement", lambda *_a, **_kw: (header_fixture, line_fixture))
    monkeypatch.setattr(
        extraction_pipeline,
        "parse_vlm_statement",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("VLM should not be called")),
    )

    header, lines = extraction_pipeline.extract_statement(
        b"pdf",
        filename="statement.pdf",
        mime_type="application/pdf",
        bank_account_id="bank-1",
    )

    assert header["parser_strategy"] == "pdf_text_blocks"
    assert header["pdf_rescue"]["attempted"] is False
    assert len(lines) == 2


def test_vlm_json_payload_accepts_markdown_fenced_json():
    payload = _parse_vlm_json_payload(
        """Here is the extracted statement:
        ```json
        {"transactions": [], "confidence_score": 0.91}
        ```
        """,
        provider="Test VLM",
    )

    assert payload == {"transactions": [], "confidence_score": 0.91}


def test_vlm_json_payload_rejects_invalid_response_with_preview():
    with pytest.raises(ValueError, match="Test VLM returned invalid JSON"):
        _parse_vlm_json_payload("I could not extract this statement", provider="Test VLM")


def test_bank_vlm_lm_studio_failure_falls_back_to_gemini(monkeypatch):
    class _FakeResponse:
        text = """
        {
          "statement_period_from": "2026-06-01",
          "statement_period_to": "2026-06-30",
          "currency": "ZAR",
          "confidence_score": 0.92,
          "transactions": [
            {
              "line_date": "2026-06-15",
              "description": "Supplier payment",
              "reference": "PAY-1",
              "debit_amount": 100,
              "credit_amount": 0,
              "balance_amount": 900
            }
          ]
        }
        """
        usage_metadata = types.SimpleNamespace(prompt_token_count=10, candidates_token_count=20)

    class _FakeModels:
        def __init__(self):
            self.calls = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return _FakeResponse()

    fake_models = _FakeModels()
    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = lambda api_key: types.SimpleNamespace(models=fake_models)
    fake_genai_types = types.SimpleNamespace(
        GenerateContentConfig=lambda **kwargs: kwargs,
        Part=types.SimpleNamespace(from_bytes=lambda **kwargs: kwargs),
    )
    fake_genai.types = fake_genai_types
    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_genai_types)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-gemini-key")
    monkeypatch.setenv("LM_STUDIO_VLM_ENABLED", "true")
    monkeypatch.setenv("LM_STUDIO_VLM_PAUSED", "false")
    monkeypatch.setattr(invoice_vlm_parser, "preprocess_for_vlm", lambda *_args, **_kwargs: [(b"png", "image/png")])
    monkeypatch.setattr(bank_vlm_parser, "_call_lm_studio_bank_vlm", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("local timeout")))

    header, lines = bank_vlm_parser.parse_vlm_statement(
        b"fake-pdf",
        mime_type="application/pdf",
        bank_account_id="bank-1",
        currency="ZAR",
        parsing_hint="Extract statement lines.",
    )

    assert len(fake_models.calls) == 1
    assert header["extraction_model"] == "gemini-2.5-flash"
    assert len(lines) == 1
    assert lines[0].description == "Supplier payment"
    assert lines[0].debit_amount == facade.money("100")


def test_fnb_vlm_yearless_transaction_date_uses_statement_period_year():
    statement_from, statement_to = _vlm_statement_period_dates(
        {"transactions": [], "confidence_score": 0.9},
        "Gold Business Account\nStatement Period : 3 April 2024 to 3 May 2024\n",
    )

    assert statement_from == "2024-04-03"
    assert statement_to == "2024-05-03"
    assert _parse_vlm_transaction_date(
        "15 Apr",
        statement_period_from=statement_from,
        statement_period_to=statement_to,
    ) == "2024-04-15"
    assert _parse_vlm_transaction_date(
        "2 May",
        statement_period_from=statement_from,
        statement_period_to=statement_to,
    ) == "2024-05-02"


def test_vlm_full_transaction_date_is_unchanged():
    assert _parse_vlm_transaction_date(
        "2024-04-15",
        statement_period_from="2024-04-03",
        statement_period_to="2024-05-03",
    ) == "2024-04-15"


def test_compatibility_facade_reexports_extraction_public_api():
    assert facade.ParsedBankLine is extraction.ParsedBankLine
    assert facade.extract_statement is extraction.extract_statement
    assert facade.parse_csv_statement is extraction.parse_csv_statement
    assert facade.parse_xlsx_statement is extraction.parse_xlsx_statement
    assert facade.money is extraction.money


# ---------------------------------------------------------------------------
# Year-less date format tests
# ---------------------------------------------------------------------------

from decimal import Decimal

from app.services.bank_statement_extraction.common import parse_date
from app.services.bank_statement_extraction.pdf_parser import (
    DATE_ANCHOR_RE,
    _TAIL_RE,
    _infer_statement_year,
    parse_text_statement_from_text,
)


def test_parse_date_year_less_dd_mon():
    assert parse_date("28 Jan", year=2024) == "2024-01-28"


def test_parse_date_year_less_mon_dd():
    assert parse_date("Jan 28", year=2024) == "2024-01-28"


def test_parse_date_year_less_case_insensitive():
    assert parse_date("28 JAN", year=2024) == "2024-01-28"
    assert parse_date("28 jan", year=2024) == "2024-01-28"


def test_parse_date_year_less_full_month():
    assert parse_date("28 January", year=2024) == "2024-01-28"


def test_parse_date_without_year_returns_none():
    assert parse_date("28 Jan") is None


def test_parse_date_full_date_unaffected_by_year_kwarg():
    assert parse_date("28/01/2024", year=2024) == "2024-01-28"


def test_parse_date_accepts_valid_leap_day():
    assert parse_date("29/02/2024") == "2024-02-29"
    assert parse_date("2024-02-29") == "2024-02-29"


def test_parse_date_rejects_impossible_dates():
    # A 29 Feb on a non-leap year (and other impossible dates) must be rejected,
    # not passed through as a broken string that fails the DB insert.
    assert parse_date("2023-02-29") is None
    assert parse_date("2024-02-30") is None
    assert parse_date("29/02/2023") is None


def test_vlm_leap_day_wrong_year_is_anchored_to_period_leap_year():
    # The VLM commonly tags a 29 Feb line with a non-leap year; it must be
    # re-anchored to the leap year actually in the statement period.
    frm, to = "2024-02-01", "2024-02-29"
    for value in ("2025-02-29", "29/02/2025", "29 Feb", "29/02"):
        assert _parse_vlm_transaction_date(
            value, statement_period_from=frm, statement_period_to=to
        ) == "2024-02-29"


def test_infer_statement_year_finds_first_year():
    text = "Standard Bank\nStatement Period: 01 Jan 2024 to 31 Jan 2024\nPage 1"
    assert _infer_statement_year(text) == 2024


def test_infer_statement_year_ignores_text_beyond_2000_chars():
    assert _infer_statement_year(" " * 2001 + "2024") is None


def test_date_anchor_re_matches_dd_mon():
    m = DATE_ANCHOR_RE.match("28 Jan Stop Order")
    assert m is not None
    assert m.group("date") == "28 Jan"


def test_date_anchor_re_matches_mon_dd():
    m = DATE_ANCHOR_RE.match("Jan 28 Stop Order")
    assert m is not None
    assert m.group("date") == "Jan 28"


def test_date_anchor_re_existing_numeric_still_matches():
    m = DATE_ANCHOR_RE.match("01/04/2024 Stop Order")
    assert m is not None
    assert m.group("date") == "01/04/2024"


def test_parse_text_statement_from_text_year_less_dates():
    text = (
        "Standard Bank Prestige Account\n"
        "Statement Period: 01 Jan 2024 to 28 Feb 2024\n"
        "\n"
        "28 Jan Stop Order Headoffice * 4250.00 95750.00\n"
        "15 Feb Immediate Trf Cr Nk 7000.00 102750.00\n"
    )
    _header, lines = parse_text_statement_from_text(text, bank_account_id="bank-1")
    assert len(lines) == 2
    assert lines[0].line_date == "2024-01-28"
    assert lines[1].line_date == "2024-02-15"


def test_parse_text_statement_from_text_dec_jan_year_rollover():
    text = (
        "Standard Bank Prestige Account\n"
        "Statement Period: 01 Dec 2024 to 03 Jan 2025\n"
        "\n"
        "28 Dec Stop Order Headoffice * 4250.00 95750.00\n"
        "03 Jan Immediate Trf Cr Nk 7000.00 102750.00\n"
    )
    _header, lines = parse_text_statement_from_text(text, bank_account_id="bank-1")
    assert len(lines) == 2
    assert lines[0].line_date == "2024-12-28"
    assert lines[1].line_date == "2025-01-03"


# ---------------------------------------------------------------------------
# Embedded-date (description-first) format tests
# ---------------------------------------------------------------------------


def test_parse_date_numeric_year_less_dd_mm():
    assert parse_date("28/01", year=2024) == "2024-01-28"
    assert parse_date("01/28", year=2024) == "2024-01-28"  # MM/DD fallback


def test_parse_date_numeric_year_less_dd_dash_mm():
    assert parse_date("28-01", year=2024) == "2024-01-28"


def test_tail_re_finds_date_and_balance_at_end_of_line():
    m = _TAIL_RE.search("Stop Order 1,523.00 28/01 95,750.00")
    assert m is not None
    assert m.group("date") == "28/01"
    assert m.group("balance") == "95,750.00"


def test_date_anchor_re_matches_numeric_without_year():
    m = DATE_ANCHOR_RE.match("28/01 Some description text")
    assert m is not None
    assert m.group("date") == "28/01"


def test_parse_text_statement_from_text_date_at_end_format():
    text = (
        "Standard Bank Prestige Account\n"
        "Statement Period: 01 Jan 2024 to 28 Feb 2024\n"
        "\n"
        "Stop Order Premier Insurance 1,523.00 28/01 95,750.00\n"
        "Premier Insurance Group\n"
        "Immediate Trf Cr 7,000.00 15/02 102,750.00\n"
        "John Smith Settlement\n"
    )
    _header, lines = parse_text_statement_from_text(text, bank_account_id="bank-1")
    assert len(lines) == 2
    assert lines[0].line_date == "2024-01-28"
    assert "Premier Insurance Group" in lines[0].description
    assert lines[1].line_date == "2024-02-15"
    assert "John Smith Settlement" in lines[1].description


def test_parse_text_statement_from_text_mixed_formats():
    text = (
        "Standard Bank\n"
        "Statement date: 28 Feb 2024\n"
        "\n"
        "28 Jan Stop Order 1,523.00 95,750.00\n"
        "FEE Immediate Payment 23.50 15/02 102,750.00\n"
    )
    _header, lines = parse_text_statement_from_text(text, bank_account_id="bank-1")
    assert len(lines) == 2
    assert lines[0].line_date == "2024-01-28"
    assert lines[1].line_date == "2024-02-15"


def test_parse_text_statement_from_text_single_line_transaction():
    text = (
        "Standard Bank\n"
        "Statement Period: 01 Jan 2024 to 31 Jan 2024\n"
        "\n"
        "FEE IMMEDIATE PAYMENT 23.50 28/01 95,726.50\n"
    )
    _header, lines = parse_text_statement_from_text(text, bank_account_id="bank-1")
    assert len(lines) == 1
    assert lines[0].line_date == "2024-01-28"
    assert lines[0].description


# ---------------------------------------------------------------------------
# FNB Gold Business Account — three-token (amount + balance Cr/Dr + bank charges)
# ---------------------------------------------------------------------------


def test_parse_text_statement_from_text_fnb_three_token_cr_balance():
    # Balance 473,674.11 is followed immediately by "Cr" in the text.
    # Without the fix, the parser picks balance→amount (473,674.11) and
    # bank-charges→balance (15.00). With the fix, amount=15,000.00 and
    # balance=473,674.11. Use debit+credit sum to test the extracted
    # amount magnitude without depending on direction inference.
    text = (
        "FNB Gold Business Account\n"
        "Statement Period: 02 Feb 2026 to 28 Feb 2026\n"
        "\n"
        "02 Feb FNB App Rtc Pmt To Nicole Mia Salary 15,000.00 473,674.11Cr 15.00\n"
        "03 Feb Payshap Account Off-Us Tgs Softw 823.87 472,850.24Cr 3.00\n"
    )
    _header, lines = parse_text_statement_from_text(text, bank_account_id="bank-1")
    assert len(lines) == 2
    assert lines[0].balance_amount == Decimal("473674.11")
    assert lines[0].debit_amount + lines[0].credit_amount == Decimal("15000.00")
    assert lines[1].balance_amount == Decimal("472850.24")
    # Second transaction has a prior balance — movement matches → direction inferred
    assert lines[1].debit_amount == Decimal("823.87")


def test_parse_text_statement_from_text_fnb_two_token_line_unaffected():
    """2-token lines (no bank charges column) must continue to work correctly."""
    text = (
        "FNB Gold Business Account\n"
        "Statement Period: 02 Feb 2026 to 28 Feb 2026\n"
        "\n"
        "23 Feb FNB App Payment To Feb Mia Salary 54,012.04 149,428.04Cr\n"
        "17 Feb Magtape Credit Capitec N Jacobsen 8,000.00Cr 361,137.33Cr\n"
    )
    _header, lines = parse_text_statement_from_text(text, bank_account_id="bank-1")
    assert len(lines) == 2
    assert lines[0].balance_amount == Decimal("149428.04")
    assert lines[0].debit_amount == Decimal("54012.04")
    assert lines[1].balance_amount == Decimal("361137.33")
    assert lines[1].credit_amount == Decimal("8000.00")
