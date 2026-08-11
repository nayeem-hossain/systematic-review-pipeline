"""
quality_tier.py -- the mechanical R/A/T/C -> quality_tier aggregation formula
(README Stage 5). Deliberately removes the reviewer-judgement step the old
README example formula left in ("0-2 points -> C+/C, split by reviewer
judgement on which dimension drove the low score"): given any four L/S/H
ratings, exactly one tier follows, so a second reviewer applying the same
ratings always reproduces the same tier.
"""
from __future__ import annotations

POINTS = {"L": 2, "S": 1, "H": 0}

# Hand-filled sheets (extract.py predates the guided select-menu editor) commonly
# spell dimensions out as the README's descriptive words rather than the terse
# code -- these must normalize to the same code, or a pre-existing sheet's R/A/T/C
# silently fails to auto-compute.
_SYNONYMS = {
    "L": "L", "LOW": "L", "LOW CONCERN": "L",
    "S": "S", "SOME": "S", "SOME CONCERNS": "S", "SOME CONCERN": "S",
    "H": "H", "HIGH": "H", "HIGH CONCERN": "H",
}

# R (rigor) and T (threat-model completeness) are hard gates: a fatal gap in
# either caps the tier at C outright, before points are even summed, because
# it puts the study's findings themselves in doubt -- not something strong
# artifact availability or currency should be able to buy back. A and C do
# NOT gate: missing artifacts and stale datasets are common enough in
# ML-security papers to be graded via points rather than disqualifying.
_GATED_DIMENSIONS = ("R", "T")


def compute_quality_tier(r, a, t, c) -> str | None:
    """Returns 'A' / 'B' / 'C', or None if any dimension is blank or not one
    of L/S/H (case-insensitive, surrounding whitespace tolerated)."""
    values = {}
    for name, raw in (("R", r), ("A", a), ("T", t), ("C", c)):
        key = str(raw).strip().upper() if raw is not None else ""
        norm = _SYNONYMS.get(key)
        if norm is None:
            return None
        values[name] = norm

    if any(values[dim] == "H" for dim in _GATED_DIMENSIONS):
        return "C"

    total = sum(POINTS[v] for v in values.values())
    if total >= 7:
        return "A"
    if total >= 5:
        return "B"
    return "C"
