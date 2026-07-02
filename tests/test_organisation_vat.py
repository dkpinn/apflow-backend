import pytest

from app.services.organisation_vat import vat_applicability, vat_report_effective_period
from tests.conftest import MemoryDB


ORG_ID = "org-1"


def db_for_org(**org):
    return MemoryDB({
        "organisations": [
            {
                "id": ORG_ID,
                "vat_registered": False,
                "vat_registration_date": None,
                **org,
            }
        ]
    })


def test_vat_applicability_not_registered():
    status = vat_applicability(
        db_for_org(),
        organisation_id=ORG_ID,
        transaction_date="2026-06-30",
    )

    assert status.registered is False
    assert status.applicable is False
    assert status.reason == "organisation_not_vat_registered"


def test_vat_applicability_registered_without_date_blocks_setup():
    with pytest.raises(ValueError, match="VAT registration date"):
        vat_applicability(
            db_for_org(vat_registered=True),
            organisation_id=ORG_ID,
            transaction_date="2026-06-30",
            action="Post VAT",
        )


@pytest.mark.parametrize(
    ("transaction_date", "applicable", "reason"),
    [
        ("2026-06-29", False, "before_vat_registration_date"),
        ("2026-06-30", True, None),
        ("2026-07-01", True, None),
    ],
)
def test_vat_applicability_respects_registration_date(transaction_date, applicable, reason):
    status = vat_applicability(
        db_for_org(vat_registered=True, vat_registration_date="2026-06-30"),
        organisation_id=ORG_ID,
        transaction_date=transaction_date,
    )

    assert status.applicable is applicable
    assert status.reason == reason


def test_vat_report_blocks_non_registered_org():
    with pytest.raises(ValueError, match="not VAT registered"):
        vat_report_effective_period(
            db_for_org(),
            organisation_id=ORG_ID,
            date_from="2026-06-01",
            date_to="2026-06-30",
        )


def test_vat_report_trims_overlapping_period_to_registration_date():
    date_from, effective_start = vat_report_effective_period(
        db_for_org(vat_registered=True, vat_registration_date="2026-06-15"),
        organisation_id=ORG_ID,
        date_from="2026-06-01",
        date_to="2026-06-30",
    )

    assert date_from == "2026-06-15"
    assert effective_start == "2026-06-15"
