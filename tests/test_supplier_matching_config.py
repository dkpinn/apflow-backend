import pytest

from app.services.supplier_matching_config import (
    normalise_amount_tiers,
    resolve_match_threshold,
    serialise_amount_tiers,
)


def test_amount_tiers_are_sorted_and_catch_all_is_last():
    tiers = serialise_amount_tiers([
        {"max_amount": None, "required_matches": 4},
        {"max_amount": 5000, "required_matches": 3},
        {"max_amount": 1000, "required_matches": 2},
    ])
    assert tiers == [
        {"max_amount": 1000.0, "required_matches": 2},
        {"max_amount": 5000.0, "required_matches": 3},
        {"max_amount": None, "required_matches": 4},
    ]


def test_amount_tiers_reject_invalid_shapes_and_duplicates():
    with pytest.raises(ValueError):
        normalise_amount_tiers({"max_amount": 100, "required_matches": 2})
    with pytest.raises(ValueError):
        normalise_amount_tiers([
            {"max_amount": None, "required_matches": 2},
            {"max_amount": None, "required_matches": 3},
        ])
    with pytest.raises(ValueError):
        normalise_amount_tiers([
            {"max_amount": 100, "required_matches": 2},
            {"max_amount": 100, "required_matches": 3},
        ])
    with pytest.raises(ValueError):
        normalise_amount_tiers([
            {"max_amount": 100, "required_matches": 2, "unexpected": True},
        ])


def test_threshold_resolution_falls_back_for_unknown_or_malformed_values():
    tiers = [
        {"max_amount": 1000, "required_matches": 2},
        {"max_amount": None, "required_matches": 4},
    ]
    assert resolve_match_threshold(tiers, None, 3) == 3
    assert resolve_match_threshold(tiers, 1000, 3) == 2
    assert resolve_match_threshold(tiers, 1000.01, 3) == 4
    assert resolve_match_threshold([{"bad": "data"}], 100, 3) == 3
