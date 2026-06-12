from decimal import Decimal

from app.services.bank_statement_extraction.pdf_parser import (
    _build_statement_from_blocks,
    parse_columnar_transaction_blocks,
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
