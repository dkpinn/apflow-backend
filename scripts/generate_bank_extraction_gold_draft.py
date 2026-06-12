from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path

from dotenv import load_dotenv

from app.services.bank_statement_extraction.common import dec_to_float
from app.services.bank_statement_extraction.pipeline import extract_statement

DUMMY_BANK_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a real bank statement file through the extraction pipeline (including "
            "the VLM fallback) and write a draft gold JSON file for hand-correction."
        )
    )
    parser.add_argument("input_file", help="Path to the statement PDF/image/CSV/XLSX")
    parser.add_argument("--document-id", help="Defaults to the input file's stem")
    parser.add_argument("--bank", required=True)
    parser.add_argument("--account-type", default="current_account")
    parser.add_argument("--document-variant", default="original_pdf")
    parser.add_argument("--currency", default="ZAR")
    parser.add_argument("--parsing-hint", default=None)
    parser.add_argument("--bank-account-id", default=DUMMY_BANK_ACCOUNT_ID)
    parser.add_argument("--output", help="Defaults to <document_id>_draft.json next to the input file")
    parser.add_argument(
        "--env-file",
        default=".env.development.local",
        help="Untracked environment file containing GOOGLE_API_KEY etc.",
    )
    args = parser.parse_args()

    load_dotenv(args.env_file, override=True)

    input_path = Path(args.input_file)
    document_id = args.document_id or input_path.stem
    output_path = Path(args.output) if args.output else input_path.with_name(f"{document_id}_draft.json")

    file_bytes = input_path.read_bytes()
    mime_type, _ = mimetypes.guess_type(input_path.name)

    header, lines = extract_statement(
        file_bytes,
        filename=input_path.name,
        mime_type=mime_type or "application/pdf",
        bank_account_id=args.bank_account_id,
        currency=args.currency,
        account_type=args.account_type,
        parsing_hint=args.parsing_hint,
    )

    transactions = []
    for index, line in enumerate(lines, start=1):
        transactions.append(
            {
                "transaction_index": index,
                "date": line.line_date,
                "description": line.description,
                "amount": dec_to_float(line.signed_amount),
                "debit": dec_to_float(line.debit_amount) if line.debit_amount else None,
                "credit": dec_to_float(line.credit_amount) if line.credit_amount else None,
                "running_balance": dec_to_float(line.balance_amount),
                "page_number": line.source_page,
                "source_reference": f"row-{line.source_row_index}" if line.source_row_index is not None else None,
            }
        )

    gold_draft = {
        "document_id": document_id,
        "bank": args.bank,
        "account_type": args.account_type,
        "document_variant": args.document_variant,
        "statement_start_date": header.get("statement_period_from"),
        "statement_end_date": header.get("statement_period_to"),
        "opening_balance": header.get("opening_balance"),
        "closing_balance": header.get("closing_balance"),
        "transactions": transactions,
    }

    output_path.write_text(json.dumps(gold_draft, indent=2), encoding="utf-8")

    print(f"Wrote draft gold JSON to {output_path}")
    print(f"Extractor used: {header.get('extractor')} ({header.get('parser_strategy')})")
    print(f"Confidence score: {header.get('confidence_score')}")
    warnings = header.get("extraction_warnings") or []
    if warnings:
        print(f"{len(warnings)} extraction warning(s):")
        for item in warnings:
            print(f"  - {item}")
    print(f"{len(transactions)} transaction(s) extracted.")
    print("Review this draft against the source document and correct any errors before using it as a gold file.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
