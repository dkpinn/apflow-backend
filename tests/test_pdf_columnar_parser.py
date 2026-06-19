from decimal import Decimal

from app.services.bank_statement_extraction.pdf_parser import (
    _build_statement_from_blocks,
    parse_columnar_transaction_blocks,
    parse_text_statement,
    parse_transaction_blocks,
)


def _word(x0: float, y0: float, text: str) -> tuple:
    return (x0, y0, x0 + len(text) * 6, y0 + 10.4, text, 0, 0, 0)


# Coordinates below mirror a real ABSA "Print to PDF" cheque account statement,
# where get_text("text") yields dates/descriptions/amounts in separate
# column-major blocks rather than row by row.
HEADER_ROW_Y = 493.1
_HEADER_WORDS = [
    _word(28.9, HEADER_ROW_Y, "Date"),
    _word(86.3, HEADER_ROW_Y, "Transaction"),
    _word(132.3, HEADER_ROW_Y, "Description"),
    _word(265.1, HEADER_ROW_Y, "Charge"),
    _word(345.0, HEADER_ROW_Y, "Debit"),
    _word(367.0, HEADER_ROW_Y, "Amount"),
    _word(427.3, HEADER_ROW_Y, "Credit"),
    _word(451.9, HEADER_ROW_Y, "Amount"),
    _word(537.1, HEADER_ROW_Y, "Balance"),
]


def _page1_words() -> list[tuple]:
    return _HEADER_WORDS + [
        # "Bal Brought Forward" opening balance row
        _word(28.0, 508.5, "1/02/2024"),
        _word(85.7, 508.5, "Bal"),
        _word(100.3, 508.5, "Brought"),
        _word(133.2, 508.5, "Forward"),
        _word(524.2, 508.5, "283"),
        _word(540.7, 508.5, "459.19"),
        # debit row, with a continuation line (beneficiary) on the next row
        _word(28.0, 518.4, "1/02/2024"),
        _word(85.3, 518.4, "Ext"),
        _word(100.0, 518.4, "Stop"),
        _word(117.9, 518.4, "Order"),
        _word(144.4, 518.4, "To"),
        _word(193.0, 518.4, "Settlement"),
        _word(364.0, 518.4, "4"),
        _word(370.7, 518.4, "250.00"),
        _word(524.2, 518.4, "279"),
        _word(540.8, 518.4, "209.19"),
        _word(99.8, 528.4, "Firstrand"),
        _word(135.0, 528.4, "A"),
        _word(143.2, 528.4, "J"),
        _word(150.4, 528.4, "Garrett"),
        # credit row
        _word(28.2, 687.5, "1/02/2024"),
        _word(84.9, 687.5, "Acb"),
        _word(102.6, 687.5, "Credit"),
        _word(193.0, 687.5, "Settlement"),
        _word(448.6, 687.5, "1"),
        _word(455.6, 687.5, "560.64"),
        _word(524.1, 687.5, "275"),
        _word(540.7, 687.5, "066.90"),
    ]


def test_parse_columnar_transaction_blocks_reconstructs_rows_from_word_coordinates():
    blocks = parse_columnar_transaction_blocks([_page1_words()])

    assert len(blocks) == 3

    opening = blocks[0]
    assert opening["date"] == "1/02/2024"
    assert opening["debit"] == Decimal("0.00")
    assert opening["credit"] == Decimal("0.00")
    assert opening["balance"] == Decimal("283459.19")
    assert "Bal Brought Forward" in opening["prefix"]

    debit_block = blocks[1]
    assert debit_block["date"] == "1/02/2024"
    assert debit_block["debit"] == Decimal("4250.00")
    assert debit_block["credit"] == Decimal("0.00")
    assert debit_block["balance"] == Decimal("279209.19")
    assert debit_block["continuation_lines"] == ["Firstrand A J Garrett"]

    credit_block = blocks[2]
    assert credit_block["date"] == "1/02/2024"
    assert credit_block["debit"] == Decimal("0.00")
    assert credit_block["credit"] == Decimal("1560.64")
    assert credit_block["balance"] == Decimal("275066.90")


def test_parse_text_statement_uses_columnar_blocks_when_text_layer_is_column_major():
    # get_text("text") for this layout groups all dates, then descriptions, then
    # amounts together -- no line contains both a date and >=2 money tokens, so
    # the row-major parser finds zero blocks.
    column_major_text = (
        "1/02/2024\n1/02/2024\n1/02/2024\n"
        "Bal Brought Forward\nExt Stop Order To Settlement\nFirstrand A J Garrett\n"
        "Acb Credit Settlement\n4 250.00\n1 560.64\n283 459.19\n279 209.19\n275 066.90\n"
    )
    assert parse_transaction_blocks(column_major_text) == []

    columnar_blocks = parse_columnar_transaction_blocks([_page1_words()])
    header, lines = _build_statement_from_blocks(
        columnar_blocks, "pdf_columnar_blocks", column_major_text, bank_account_id="bank-1", currency="ZAR"
    )

    assert header["parser_strategy"] == "pdf_columnar_blocks"
    assert len(lines) == 3
    assert lines[1].debit_amount == Decimal("4250.00")
    assert lines[1].credit_amount == Decimal("0.00")
    assert lines[1].signed_amount == Decimal("-4250.00")
    assert "Firstrand A J Garrett" in lines[1].description
    assert lines[2].credit_amount == Decimal("1560.64")
    assert lines[2].signed_amount == Decimal("1560.64")
    assert header["opening_balance"] == 283459.19
    assert header["closing_balance"] == 275066.90


def test_parse_columnar_transaction_blocks_returns_empty_without_header():
    assert parse_columnar_transaction_blocks([[_word(28.0, 508.5, "1/02/2024")]]) == []


def _page_with_absa_bank_continuation() -> list[tuple]:
    return _HEADER_WORDS + [
        # "Digital Payment Dt Settlement" debit row, beneficiary's bank is
        # "Absa Bank" -- the continuation line below must not be mistaken
        # for "ABSA Bank Limited" footer boilerplate and dropped.
        _word(28.0, 508.5, "8/02/2024"),
        _word(85.7, 508.5, "Digital"),
        _word(111.9, 508.5, "Payment"),
        _word(147.7, 508.5, "Dt"),
        _word(193.0, 508.5, "Settlement"),
        _word(364.0, 508.5, "3"),
        _word(370.8, 508.5, "000.00"),
        _word(524.2, 508.5, "154"),
        _word(540.8, 508.5, "620.06"),
        _word(99.2, 518.4, "Absa"),
        _word(121.6, 518.4, "Bank"),
        _word(143.5, 518.4, "Bryan"),
        _word(168.1, 518.4, "Hellmann"),
    ]


def test_parse_columnar_transaction_blocks_keeps_absa_bank_continuation_line():
    blocks = parse_columnar_transaction_blocks([_page_with_absa_bank_continuation()])

    assert len(blocks) == 1
    block = blocks[0]
    assert block["transaction_type"] == "Digital Payment Dt"
    assert block["continuation_lines"] == ["Absa Bank Bryan Hellmann"]


def test_build_statement_from_blocks_no_missing_continuation_warning_for_absa_bank_beneficiary():
    blocks = parse_columnar_transaction_blocks([_page_with_absa_bank_continuation()])
    header, lines = _build_statement_from_blocks(
        blocks, "pdf_columnar_blocks", "", bank_account_id="bank-1", currency="ZAR"
    )

    assert header["extraction_warnings"] == []
    assert "Absa Bank Bryan Hellmann" in lines[0].description
    assert lines[0].counterparty == "Absa Bank Bryan Hellmann"


STANDARD_HEADER_WORDS = [
    _word(42.0, 402.8, "Details"),
    _word(214.0, 402.8, "Service"),
    _word(221.0, 412.1, "Fee"),
    _word(276.0, 407.5, "Debits"),
    _word(339.0, 407.5, "Credits"),
    _word(391.0, 402.8, "Date"),
    _word(483.0, 402.8, "Balance"),
]


def _standard_bank_words() -> list[tuple]:
    return STANDARD_HEADER_WORDS + [
        _word(42.0, 425.9, "BALANCE"),
        _word(82.0, 425.9, "BROUGHT"),
        _word(124.0, 425.9, "FORWARD"),
        _word(391.0, 425.9, "10"),
        _word(404.0, 425.9, "05"),
        _word(473.0, 425.9, "38,292.14"),
        _word(42.0, 436.3, "CELLPHONE"),
        _word(93.0, 436.3, "INSTANTMON"),
        _word(148.0, 436.3, "CASH"),
        _word(172.0, 436.3, "TO"),
        _word(295.0, 436.3, "350.00-"),
        _word(391.0, 436.3, "10"),
        _word(404.0, 436.3, "09"),
        _word(473.0, 436.3, "37,942.14"),
        _word(42.0, 446.7, "0849549395"),
        _word(89.0, 446.7, "16H15"),
        _word(114.0, 446.7, "236320221"),
        _word(42.0, 455.5, "FEE"),
        _word(60.0, 455.5, "-"),
        _word(65.0, 455.5, "INSTANT"),
        _word(101.0, 455.5, "MONEY"),
        _word(224.0, 455.5, "##"),
        _word(305.0, 455.5, "9.50-"),
        _word(391.0, 455.5, "10"),
        _word(404.0, 455.5, "09"),
        _word(473.0, 455.5, "37,932.64"),
        _word(42.0, 466.3, "0849549395"),
        _word(89.0, 466.3, "16H15"),
        _word(114.0, 466.3, "236320221"),
        _word(42.0, 553.9, "CREDIT"),
        _word(74.0, 553.9, "TRANSFER"),
        _word(339.0, 553.9, "11,690.00"),
        _word(391.0, 553.9, "10"),
        _word(404.0, 553.9, "15"),
        _word(473.0, 553.9, "39,311.14"),
        _word(42.0, 564.3, "GLOSS"),
        _word(42.0, 690.6, "VAT"),
        _word(63.0, 690.6, "Summary"),
        _word(44.0, 707.0, "Total"),
        _word(66.0, 707.0, "charge"),
        _word(96.0, 707.0, "amount"),
        _word(481.0, 707.0, "227.36-"),
    ]


def test_parse_columnar_transaction_blocks_reconstructs_standard_bank_split_header():
    blocks = parse_columnar_transaction_blocks([_standard_bank_words()])

    assert len(blocks) == 4

    opening = blocks[0]
    assert opening["date"] == "10 05"
    assert opening["date_format"] == "month_day"
    assert opening["balance"] == Decimal("38292.14")

    debit_block = blocks[1]
    assert debit_block["prefix"] == "CELLPHONE INSTANTMON CASH TO"
    assert debit_block["debit"] == Decimal("350.00")
    assert debit_block["credit"] == Decimal("0.00")
    assert debit_block["continuation_lines"] == ["0849549395 16H15 236320221"]

    fee_block = blocks[2]
    assert fee_block["prefix"] == "FEE - INSTANT MONEY"
    assert fee_block["debit"] == Decimal("9.50")
    assert "##" not in fee_block["prefix"]
    assert fee_block["continuation_lines"] == ["0849549395 16H15 236320221"]

    credit_block = blocks[3]
    assert credit_block["credit"] == Decimal("11690.00")
    assert credit_block["continuation_lines"] == ["GLOSS"]


def test_build_statement_from_standard_bank_blocks_parses_month_day_dates():
    blocks = parse_columnar_transaction_blocks([_standard_bank_words()])
    header, lines = _build_statement_from_blocks(
        blocks,
        "pdf_columnar_blocks",
        "Statement from 05 October 2024 to 05 November 2024",
        bank_account_id="bank-1",
        currency="ZAR",
        statement_year=2024,
    )

    assert header["parser_strategy"] == "pdf_columnar_blocks"
    assert [line.line_date for line in lines] == [
        "2024-10-05",
        "2024-10-09",
        "2024-10-09",
        "2024-10-15",
    ]
    assert lines[1].signed_amount == Decimal("-350.00")
    assert lines[2].signed_amount == Decimal("-9.50")
    assert lines[3].signed_amount == Decimal("11690.00")
    assert header["closing_balance"] == 39311.14


def test_parse_columnar_transaction_blocks_ignores_pages_without_table_header():
    terms_page = [
        _word(42.0, 410.0, "Details"),
        _word(72.0, 410.0, "of"),
        _word(84.0, 410.0, "Agreement"),
    ]

    blocks = parse_columnar_transaction_blocks([_standard_bank_words(), terms_page])

    assert blocks[-1]["continuation_lines"] == ["GLOSS"]


def test_parse_text_statement_prefers_stronger_columnar_result(monkeypatch):
    monkeypatch.setattr(
        "app.services.bank_statement_extraction.pdf_parser.extract_pdf_text",
        lambda _file_bytes: (
            "Statement from 05 October 2024 to 05 November 2024\n"
            "10/09 Single text row 350.00 37942.14\n"
        ),
    )
    monkeypatch.setattr(
        "app.services.bank_statement_extraction.pdf_parser.extract_pdf_words_by_page",
        lambda _file_bytes: [_standard_bank_words()],
    )

    header, lines = parse_text_statement(b"pdf", bank_account_id="bank-1", currency="ZAR")

    assert header["parser_strategy"] == "pdf_columnar_blocks"
    assert len(lines) == 4
    assert lines[1].description == "CELLPHONE INSTANTMON CASH TO 0849549395 16H15 236320221"
