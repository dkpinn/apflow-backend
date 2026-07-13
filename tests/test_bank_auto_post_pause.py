"""Auto-post honours the per-account pause flag."""
from __future__ import annotations

import app.services.bank.auto_post as ap
from tests.conftest import MemoryDB

ORG = "00000000-0000-0000-0000-000000000001"


def _base_tables(paused: bool):
    return {
        "bank_transaction_rules": [
            {
                "id": "rule-1",
                "organisation_id": ORG,
                "active": True,
                "auto_post": True,
                "priority": 1,
                "amount_direction": "any",
                "match_type": "contains",
            }
        ],
        "bank_accounts": [
            {
                "id": "ba-1",
                "organisation_id": ORG,
                "gl_account_id": "gl-1",
                "auto_post_paused": paused,
            }
        ],
        "bank_statement_lines": [
            {
                "id": "line-1",
                "organisation_id": ORG,
                "bank_account_id": "ba-1",
                "posting_status": "unposted",
                "duplicate_status": "clear",
                "signed_amount": -100.0,
                "description": "Anything",
            }
        ],
    }


def test_auto_post_skips_when_account_paused():
    db = MemoryDB(_base_tables(paused=True))

    result = ap.auto_post_matched_lines(
        db, organisation_id=ORG, bank_account_id="ba-1", line_ids=["line-1"]
    )

    assert result.get("paused") is True
    assert result["posted_count"] == 0
    # Nothing was posted while on hold.
    assert not db.tables.get("gl_journals")
