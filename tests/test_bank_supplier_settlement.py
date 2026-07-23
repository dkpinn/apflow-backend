from __future__ import annotations

import pytest

from app.services.bank.supplier_settlement import (
    accept_supplier_invoice_bank_match,
    reverse_supplier_invoice_bank_match,
)
from app.services.bank.accounts import supplier_invoice_match_eligibility
from tests.conftest import StubDB


def test_accept_supplier_invoice_bank_match_calls_atomic_rpc_and_returns_settlement():
    result_payload = {
        "journal_id": "journal-1",
        "payment_id": "payment-1",
        "invoice_id": "invoice-1",
        "amount": 100.0,
        "outstanding_before": 100.0,
        "outstanding_after": 0.0,
        "payment_status": "paid",
    }
    db = StubDB(rpc_result=result_payload)

    result = accept_supplier_invoice_bank_match(
        db,
        organisation_id="org-1",
        bank_statement_line_id="line-1",
        suggestion_id="suggestion-1",
        actor_user_id="user-1",
    )

    assert result == result_payload
    assert db.rpc_calls == [(
        "accept_supplier_invoice_bank_match_atomic",
        {
            "p_org_id": "org-1",
            "p_bank_statement_line_id": "line-1",
            "p_suggestion_id": "suggestion-1",
            "p_user_id": "user-1",
        },
    )]


def test_accept_supplier_invoice_bank_match_rejects_empty_rpc_result():
    db = StubDB(rpc_result=None)

    with pytest.raises(ValueError, match="did not return a posted journal"):
        accept_supplier_invoice_bank_match(
            db,
            organisation_id="org-1",
            bank_statement_line_id="line-1",
            suggestion_id="suggestion-1",
            actor_user_id="user-1",
        )


def test_reverse_supplier_invoice_bank_match_calls_atomic_rpc():
    db = StubDB(rpc_result={"reversal_id": "reversal-1", "payment_id": "payment-1"})

    result = reverse_supplier_invoice_bank_match(
        db,
        organisation_id="org-1",
        journal_id="journal-1",
        actor_user_id="user-1",
    )

    assert result["reversal_id"] == "reversal-1"
    assert db.rpc_calls == [(
        "reverse_supplier_invoice_bank_match_atomic",
        {
            "p_org_id": "org-1",
            "p_journal_id": "journal-1",
            "p_user_id": "user-1",
        },
    )]


def test_atomic_supplier_bank_match_migration_covers_payment_allocation_and_gl():
    sql = (
        __import__("pathlib").Path(__file__).parents[1]
        / "supabase"
        / "migrations"
        / "20260722130000_atomic_supplier_bank_match.sql"
    ).read_text(encoding="utf-8").lower()

    assert "insert into public.payments" in sql
    assert "insert into public.reconciliation_lines" in sql
    assert "insert into public.gl_journal_lines" in sql
    assert "payable_gl_id" in sql
    assert "posting_status = 'posted'" in sql
    assert "outstanding_after" in sql
    assert "for update" in sql
    assert "reverse_supplier_invoice_bank_match_atomic" in sql
    assert "delete from public.reconciliations" in sql


@pytest.mark.parametrize(
    ("invoice", "eligible", "block_code"),
    [
        (
            {"review_status": "pending", "approval_status": "pending", "posting_status": "unposted"},
            False,
            "invoice_not_posted",
        ),
        (
            {"review_status": "approved", "approval_status": "approved", "posting_status": "unposted"},
            False,
            "invoice_not_posted",
        ),
        (
            {"review_status": "approved", "approval_status": "approved", "posting_status": "posted"},
            True,
            None,
        ),
    ],
)
def test_supplier_invoice_match_eligibility(invoice, eligible, block_code):
    assert supplier_invoice_match_eligibility(invoice) == (eligible, block_code)
