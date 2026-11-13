"""The generation that names a task row's owner (ADR #130 §2.1, D40)."""

import pytest

from polyris.coordination import ownership


@pytest.mark.parametrize("value,legacy", [
    (None, True), ("", True), (0, True), ("0", True), (0.0, True),   # a pre-upgrade row or owner
    (1, False), ("2", False), (3.0, False),
])
def test_only_rows_nobody_raised_count_as_legacy(value, legacy):
    from decimal import Decimal

    assert ownership.is_legacy(value) is legacy
    if isinstance(value, int):
        assert ownership.is_legacy(Decimal(value)) is legacy        # as DynamoDB returns it


def test_a_raised_generation_never_matches_a_legacy_owner():
    assert ownership.FIRST > ownership.LEGACY
    assert ownership.RAISE_VALUES == {":zero": ownership.LEGACY, ":one": 1}
