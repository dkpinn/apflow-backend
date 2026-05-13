from __future__ import annotations
from pydoc import text
import re
from typing import Optional


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]

def normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())

def clean_numeric(value: str) -> str:
    return re.sub(r"\D", "", value.strip())

def extract_banking_block(text: str, window: int = 30) -> str:
    """
    Returns the section of the invoice most likely to contain banking/payment details.
    This prevents bank extraction from scanning the entire invoice too aggressively.
    """
    lines = normalise_lines(text)

    start_terms = [
        "banking details",
        "bank details",
        "payment details",
        "eft details",
        "electronic payment",
        "direct deposit",
        "bank account",
    ]

    for index, line in enumerate(lines):
        lower = line.lower()
        if any(term in lower for term in start_terms):
            return "\n".join(lines[index:index + window])

    return ""

def match_known_bank(value: str) -> Optional[str]:
    bank_aliases = {
        "Nedbank": [
            "nedbank",
            "nedbank ltd",
            "nedbank limited",
        ],
        "Standard Bank": [
            "standard bank",
            "standard bank of south africa",
            "standard bank ltd",
        ],
        "ABSA": [
            "absa",
            "absa bank",
            "absa bank ltd",
        ],
        "FNB": [
            "fnb",
            "first national bank",
            "first national bank ltd",
        ],
        "Capitec": [
            "capitec",
            "capitec bank",
        ],
        "Investec": [
            "investec",
            "investec bank",
        ],
        "TymeBank": [
            "tymebank",
            "tyme bank",
        ],
        "African Bank": [
            "african bank",
        ],
        "Discovery Bank": [
            "discovery bank",
        ],
        "Mercantile Bank": [
            "mercantile bank",
        ],
        "Bidvest Bank": [
            "bidvest bank",
        ],
        "Sasfin": [
            "sasfin",
            "sasfin bank",
        ],
        "NatWest": [
            "natwest",
            "national westminster bank",
        ],
        "Barclays": [
            "barclays",
            "barclays bank",
        ],
    }

    value_norm = normalise_text(value)

    for canonical_name, aliases in bank_aliases.items():
        for alias in aliases:
            alias_norm = normalise_text(alias)
            if re.search(rf"\b{re.escape(alias_norm)}\b", value_norm, re.IGNORECASE):
                return canonical_name

    return None

def extract_value_after_label(lines: list[str], labels: set[str], max_lookahead: int = 4) -> Optional[str]:
    """
    Handles:
    Bank Name:
    Nedbank Ltd

    Account Number:
    123456789
    """
    for index, line in enumerate(lines):
        line_norm = normalise_text(line).rstrip(":")

        if line_norm not in labels:
            continue

        for candidate in lines[index + 1:index + 1 + max_lookahead]:
            candidate = candidate.strip()

            if not candidate:
                continue

            candidate_norm = normalise_text(candidate).rstrip(":")

            if candidate_norm in labels:
                continue

            return candidate

    return None

def extract_same_line_value(text: str, patterns: list[str]) -> Optional[str]:
    """
    Handles:
    Bank Name: Nedbank Ltd
    Account Number: 123456789
    """
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            if value:
                return value

    return None

def extract_bank_name(text: str) -> Optional[str]:
    """
    Extract bank name from explicit banking labels or banking block.
    """
    one_line = extract_one_line_bank_details(text)
    
    if one_line.get("bank_name"):
        return one_line["bank_name"]

    banking_block = extract_banking_block(text)
    search_text = banking_block or text
    lines = normalise_lines(search_text)

    same_line_patterns = [
        r"^(?:Name\s+of\s+Bank|Bank\s+Name|Bank)\s*:\s*(.+)$",
        r"^(?:Bank)\s*[-]\s*(.+)$",
    ]

    candidate = extract_same_line_value(search_text, same_line_patterns)

    if candidate:
        known = match_known_bank(candidate)
        if known:
            return known

        if 2 <= len(candidate) <= 60 and not re.search(r"\d{4,}", candidate):
            return candidate

    label_candidate = extract_value_after_label(
        lines,
        labels={
            "name of bank",
            "bank name",
            "bank",
        },
    )

    if label_candidate:
        known = match_known_bank(label_candidate)
        if known:
            return known

        if 2 <= len(label_candidate) <= 60 and not re.search(r"\d{4,}", label_candidate):
            return label_candidate

    known = match_known_bank(search_text)
    if known:
        return known

    return None

def extract_bank_account_name(text: str) -> Optional[str]:
    """
    Extract beneficiary/account holder name.
    """

    banking_block = extract_banking_block(text)
    search_text = banking_block or text
    lines = normalise_lines(search_text)

    same_line_patterns = [
        r"^(?:Name\s+of\s+Account|Account\s+Name|Beneficiary|Account\s+Holder)\s*:\s*(.+)$",
    ]

    candidate = extract_same_line_value(search_text, same_line_patterns)

    if candidate:
        candidate = candidate.strip()
        if 2 <= len(candidate) <= 90 and not re.search(r"^\d+$", candidate):
            return candidate

    label_candidate = extract_value_after_label(
        lines,
        labels={
            "name of account",
            "account name",
            "beneficiary",
            "account holder",
        },
    )

    if label_candidate:
        label_candidate = label_candidate.strip()
        if 2 <= len(label_candidate) <= 90 and not re.search(r"^\d+$", label_candidate):
            return label_candidate

    return None

def extract_bank_account_number(text: str) -> Optional[str]:
    """
    Extract bank account number.
    """
    one_line = extract_one_line_bank_details(text)

    if one_line.get("account_number"):
        return one_line["account_number"]
    
    banking_block = extract_banking_block(text)
    search_text = banking_block or text
    lines = normalise_lines(search_text)

    same_line_patterns = [
        r"^(?:Account\s*(?:No\.?|Number)?|Acc\s*No\.?|Bank\s*Account)\s*[:#\-]?\s*([0-9\- ]{6,25})$",
    ]

    candidate = extract_same_line_value(search_text, same_line_patterns)

    if candidate:
        value = clean_numeric(candidate)
        if 6 <= len(value) <= 25:
            return value

    label_candidate = extract_value_after_label(
        lines,
        labels={
            "account number",
            "account no",
            "acc no",
            "bank account",
        },
    )

    if label_candidate:
        value = clean_numeric(label_candidate)
        if re.fullmatch(r"[0-9\-]{6,25}", value):
            return value

    return None

def extract_bank_branch_code(text: str) -> Optional[str]:
    """
    Extract SA branch code or equivalent sort/routing code.
    """
    one_line = extract_one_line_bank_details(text)

    if one_line.get("branch_code"):
        return one_line["branch_code"]
    
    banking_block = extract_banking_block(text)
    search_text = banking_block or text
    lines = normalise_lines(search_text)

    same_line_patterns = [
        r"^(?:Branch\s+Code|Branch|Sort\s+Code|Routing\s+Number)\s*[:#\-]?\s*([0-9\- ]{4,15})$",
    ]

    candidate = extract_same_line_value(search_text, same_line_patterns)

    if candidate:
        value = clean_numeric(candidate)
        if 4 <= len(value) <= 15:
            return value

    label_candidate = extract_value_after_label(
        lines,
        labels={
            "branch code",
            "branch",
            "sort code",
            "routing number",
        },
    )

    if label_candidate:
        value = clean_numeric(label_candidate)
        if re.fullmatch(r"[0-9\-]{4,15}", value):
            return value

    return None

def extract_swift_code(text: str) -> Optional[str]:
    """
    Extract SWIFT/BIC code.
    """
    one_line = extract_one_line_bank_details(text)

    if one_line.get("swift_code"):
        return one_line["swift_code"]
    
    banking_block = extract_banking_block(text)
    search_text = banking_block or text
    lines = normalise_lines(search_text)

    same_line_patterns = [
        r"^(?:SWIFT|BIC|SWIFT\s+Code|BIC\s+Code)\s*[:#\-]?\s*([A-Z0-9]{8,11})$",
    ]

    candidate = extract_same_line_value(search_text, same_line_patterns)

    if candidate:
        return candidate.strip().upper()

    label_candidate = extract_value_after_label(
        lines,
        labels={
            "swift",
            "bic",
            "swift code",
            "bic code",
        },
    )

    if label_candidate:
        value = label_candidate.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{8,11}", value):
            return value

    return None

def extract_one_line_bank_details(text: str) -> dict:
    """
    Handles one-line banking details such as:

    Bank details: Standard Bank - Sandton - Account No: 023250739 - Branch Code: 019205
    """

    patterns = [
        r"Bank\s+details\s*:\s*(.+)",
        r"Banking\s+details\s*:\s*(.+)",
        r"Payment\s+details\s*:\s*(.+)",
    ]

    banking_line = None

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            banking_line = match.group(1).strip()
            break

    if not banking_line:
        return {
            "bank_name": None,
            "bank_branch_name": None,
            "account_number": None,
            "branch_code": None,
            "swift_code": None,
        }

    bank_name = match_known_bank(banking_line)

    account_number = None
    account_match = re.search(
        r"(?:Account\s*No\.?|Account\s*Number|Acc\s*No\.?)\s*[:#\-]?\s*([0-9\- ]{6,25})",
        banking_line,
        re.IGNORECASE,
    )
    if account_match:
        account_number = clean_numeric(account_match.group(1))

    branch_code = None
    branch_match = re.search(
        r"(?:Branch\s*Code|Sort\s*Code|Routing\s*Number)\s*[:#\-]?\s*([0-9\- ]{4,15})",
        banking_line,
        re.IGNORECASE,
    )
    if branch_match:
        branch_code = clean_numeric(branch_match.group(1))

    swift_code = None
    swift_match = re.search(
        r"(?:SWIFT|BIC|SWIFT\s*Code|BIC\s*Code)\s*[:#\-]?\s*([A-Z0-9]{8,11})",
        banking_line,
        re.IGNORECASE,
    )
    if swift_match:
        swift_code = swift_match.group(1).strip().upper()

    bank_branch_name = None

    # Example split:
    # Standard Bank - Sandton - Account No: 023250739 - Branch Code: 019205
    parts = [part.strip() for part in banking_line.split("-") if part.strip()]

    if len(parts) >= 2:
        possible_branch = parts[1]

        if not re.search(r"(account|branch|swift|bic|code|number)", possible_branch, re.IGNORECASE):
            bank_branch_name = possible_branch

    return {
        "bank_name": bank_name,
        "bank_branch_name": bank_branch_name,
        "account_number": account_number,
        "branch_code": branch_code,
        "swift_code": swift_code,
    }