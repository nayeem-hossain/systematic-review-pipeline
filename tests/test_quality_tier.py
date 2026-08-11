"""
compute_quality_tier() -- the mechanical R/A/T/C -> quality_tier aggregation
formula settled on in README Stage 5: no reviewer judgement call, so two
reviewers scoring the same four L/S/H ratings always land on the same tier.

Point scale: L=2, S=1, H=0 per dimension (0-8 raw points).
Hard gate (no compensation): R=H or T=H caps the tier at C outright, before
points are even summed -- a fatal rigor or threat-model gap should not be
buyable back by strong artifact availability or currency. A=H/C=H do NOT gate,
since missing artifacts and stale datasets are common enough in this field to
be graded rather than disqualifying.
Otherwise: 7-8 points -> A, 5-6 points -> B, 0-4 points -> C.
"""
import pytest

from srp.quality_tier import compute_quality_tier


class TestHardGate:
    def test_r_high_caps_at_c_even_with_a_high_raw_score(self):
        """R=H, everything else L would sum to 6 points (B) under pure
        point-sum -- the hard gate must still force C."""
        assert compute_quality_tier("H", "L", "L", "L") == "C"

    def test_t_high_caps_at_c_even_with_a_high_raw_score(self):
        assert compute_quality_tier("L", "L", "H", "L") == "C"

    def test_both_r_and_t_high_still_just_c(self):
        assert compute_quality_tier("H", "L", "H", "L") == "C"

    def test_a_high_does_not_gate(self):
        """Missing artifacts alone must not crash the tier -- graded via
        points, not disqualifying."""
        assert compute_quality_tier("L", "H", "L", "L") == "B"

    def test_c_high_does_not_gate(self):
        assert compute_quality_tier("L", "L", "L", "H") == "B"


class TestPointSumBoundaries:
    def test_all_low_is_max_points_tier_a(self):
        assert compute_quality_tier("L", "L", "L", "L") == "A"

    def test_seven_points_is_tier_a(self):
        assert compute_quality_tier("L", "L", "L", "S") == "A"

    def test_six_points_is_tier_b(self):
        assert compute_quality_tier("L", "S", "L", "S") == "B"

    def test_five_points_is_tier_b(self):
        assert compute_quality_tier("S", "S", "S", "L") == "B"

    def test_four_points_is_tier_c(self):
        assert compute_quality_tier("L", "H", "L", "H") == "C"

    def test_zero_points_is_tier_c(self):
        assert compute_quality_tier("H", "H", "H", "H") == "C"


class TestNormalization:
    def test_lowercase_inputs_are_accepted(self):
        assert compute_quality_tier("l", "l", "l", "l") == "A"

    def test_surrounding_whitespace_is_stripped(self):
        assert compute_quality_tier(" L ", " L ", " L ", " L ") == "A"


class TestFullWordSynonyms:
    """Real hand-filled extraction sheets (e.g. examples/quality_scored_sample.csv,
    written before the guided select-menu editor existed) spell dimensions out
    as 'Low'/'Some'/'High' rather than the terse L/S/H codes -- these must be
    recognized too, or every pre-existing sheet silently fails to auto-compute."""

    def test_full_words_case_insensitive(self):
        assert compute_quality_tier("Low", "Some", "High", "Low") == "C"  # T=High gates

    def test_full_words_all_low_is_tier_a(self):
        assert compute_quality_tier("Low", "Low", "Low", "Low") == "A"

    def test_full_words_matches_example_row(self):
        """id 1 from examples/quality_scored_sample.csv: Some/Some/Some/Low."""
        assert compute_quality_tier("Some", "Some", "Some", "Low") == "B"


class TestIncompleteOrInvalidInputs:
    """A blank or unrecognized dimension means the tier cannot be computed --
    must return None, not silently default to a tier."""

    @pytest.mark.parametrize("r,a,t,c", [
        ("", "L", "L", "L"),
        (None, "L", "L", "L"),
        ("L", "L", "L", None),
        ("Medium", "L", "L", "L"),  # not part of this 3-point rubric's vocabulary
        ("L", "L", "L", "N/A"),
    ])
    def test_returns_none(self, r, a, t, c):
        assert compute_quality_tier(r, a, t, c) is None
