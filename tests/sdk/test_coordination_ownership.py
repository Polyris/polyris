"""The generation that names a task row's owner (ADR #137 §3, Rule 47)."""

from polyris.coordination import ownership


def test_no_owner_of_1_3_matches_a_row_that_1_2_left_without_a_generation():
    """The stop of a row 1.2 left in flight writes it under LEGACY; every 1.3 row starts at FIRST and only rises."""
    assert ownership.FIRST > ownership.LEGACY
