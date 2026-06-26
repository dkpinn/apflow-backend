import pytest

from app.services.accounting_locks import (
    assert_accounting_period_unlocked,
    effective_accounting_lock_date,
)
from tests.conftest import StubDB


def _db(periods):
    return StubDB({"organisation_accounting_periods": periods})


def test_effective_lock_date_uses_latest_closed_or_locked_period():
    lock_date = effective_accounting_lock_date(
        _db(
            [
                {"organisation_id": "org-1", "status": "open", "lock_date": "2026-06-30"},
                {"organisation_id": "org-1", "status": "closed", "lock_date": "2026-04-30"},
                {"organisation_id": "org-1", "status": "locked", "lock_date": "2026-05-31"},
                {"organisation_id": "org-2", "status": "locked", "lock_date": "2026-12-31"},
            ]
        ),
        organisation_id="org-1",
    )

    assert lock_date.isoformat() == "2026-05-31"


def test_unlocked_when_no_closed_or_locked_period_exists():
    assert_accounting_period_unlocked(
        _db([{"organisation_id": "org-1", "status": "open", "lock_date": "2026-06-30"}]),
        organisation_id="org-1",
        transaction_date="2026-06-01",
    )


def test_transaction_on_or_before_lock_date_is_blocked():
    with pytest.raises(ValueError, match="accounting lock date 2026-05-31"):
        assert_accounting_period_unlocked(
            _db([{"organisation_id": "org-1", "status": "locked", "lock_date": "2026-05-31"}]),
            organisation_id="org-1",
            transaction_date="2026-05-31",
            action="Post bank journal",
        )


def test_transaction_after_lock_date_is_allowed():
    assert_accounting_period_unlocked(
        _db([{"organisation_id": "org-1", "status": "locked", "lock_date": "2026-05-31"}]),
        organisation_id="org-1",
        transaction_date="2026-06-01",
    )


def test_missing_transaction_date_is_blocked_when_lock_exists():
    with pytest.raises(ValueError, match="transaction date is missing"):
        assert_accounting_period_unlocked(
            _db([{"organisation_id": "org-1", "status": "locked", "lock_date": "2026-05-31"}]),
            organisation_id="org-1",
            transaction_date=None,
        )
