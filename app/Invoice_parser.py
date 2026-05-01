# app/parsers/invoice_parser.py

import fitz  # PyMuPDF
import re


def parse_invoice_pdf(file_bytes):

    doc = fitz.open(stream=file_bytes, filetype="pdf")

    full_text = ""

    for page in doc:
        full_text += page.get_text()

    # VERY BASIC extraction (improve later)

    supplier = extract_supplier(full_text)
    invoice_number = extract_invoice_number(full_text)
    total = extract_total(full_text)

    return {
        "supplier_name": supplier,
        "invoice_number": invoice_number,
        "invoice_date": None,
        "total_amount": total,
        "currency": "GBP"
    }


def extract_supplier(text):
    lines = text.split("\n")
    return lines[0]  # placeholder


def extract_invoice_number(text):
    match = re.search(r"INV[-\s]?\d+", text)
    return match.group(0) if match else None


def extract_total(text):
    match = re.search(r"\$?\d{1,3}(,\d{3})*(\.\d{2})", text)
    return float(match.group(0).replace(",", "")) if match else None