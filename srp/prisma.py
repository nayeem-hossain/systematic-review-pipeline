"""
prisma.py -- the multi-phase PRISMA-count derivation shared by slr.py's guided
TUI (`_compute_prisma_counts`) and scripts/figures.py's standalone --run-dir
mode. Extracted so there is exactly one implementation of "sum identified /
duplicates-removed / screened / excluded-ta / assessed-ft across every phase's
own candidates/dedup/screening sheets, then read excluded-ft / included from
the run's merged included_final.csv" -- ft_decision lives there, never in any
single phase's screening.csv. See srp/decisions.py's docstring for why this
project treats a second copy of this kind of logic as a bug waiting to
happen: that fix already happened once, for AI-decision ingestion; this is
the same fix for PRISMA counting, after scripts/figures.py's standalone CLI
was found to have no way to reach the (already correct) multi-phase logic
that only lived inside slr.py.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import pandas as pd

from srp.decisions import TA_PROCEED_DECISIONS


def _norm(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.lower()


@dataclass
class PhaseFrames:
    candidates: pd.DataFrame
    dedup: pd.DataFrame
    screening: pd.DataFrame


def derive_prisma_counts_for_run(phases: list[PhaseFrames],
                                  included_final: pd.DataFrame) -> dict:
    identified = duplicates_removed = screened = excluded_ta = assessed_ft = 0
    undecided_ta = 0
    for p in phases:
        identified += len(p.candidates)
        if "duplicate_of" in p.dedup.columns:
            duplicates_removed += int(p.dedup["duplicate_of"].notna().sum())
        elif not p.candidates.empty and not p.dedup.empty:
            duplicates_removed += max(len(p.candidates) - len(p.dedup), 0)
        screened += len(p.screening)
        if "ta_decision" in p.screening.columns:
            ta = _norm(p.screening["ta_decision"])
            excluded_ta += int(ta.eq("exclude").sum())
            undecided_ta += int(ta.eq("").sum())
            assessed_ft += int(ta.isin(TA_PROCEED_DECISIONS).sum())

    excluded_ft = included = undecided_ft = 0
    ft_reasons: Counter = Counter()
    if not included_final.empty and "ft_decision" in included_final.columns:
        ft = _norm(included_final["ft_decision"])
        excluded_ft = int(ft.eq("exclude").sum())
        included = int(ft.eq("include").sum())
        undecided_ft = int(ft.eq("").sum())
        if "ft_reason" in included_final.columns:
            for reason in included_final.loc[ft.eq("exclude"), "ft_reason"].dropna():
                reason = str(reason).strip()
                if reason:
                    ft_reasons[reason] += 1

    return {
        "identified": identified,
        "duplicates_removed": duplicates_removed,
        "screened": screened,
        "excluded_ta": excluded_ta,
        "assessed_ft": assessed_ft,
        "excluded_ft": excluded_ft,
        "included": included,
        "undecided_ta": undecided_ta,
        "undecided_ft": undecided_ft,
        "ft_reasons": dict(ft_reasons),
    }


def prisma_residuals(counts: dict) -> list[str]:
    """Arithmetic sanity checks across the PRISMA count derivation -- warnings,
    not hard errors, since a partially-run review legitimately has gaps."""
    warnings = []
    if counts.get("undecided_ta"):
        warnings.append(
            f"{counts['undecided_ta']} record(s) have no title/abstract decision yet -- "
            f"they appear in neither the 'excluded' nor the 'assessed' box, so the "
            f"diagram will not balance until they are screened.")
    screened = counts.get("screened", 0)
    excluded_ta = counts.get("excluded_ta", 0)
    assessed_ft = counts.get("assessed_ft", 0)
    undecided_ta = counts.get("undecided_ta", 0)
    diff = screened - (excluded_ta + assessed_ft + undecided_ta)
    if diff != 0:
        warnings.append(
            f"screened ({screened}) != excluded_ta ({excluded_ta}) + assessed_ft "
            f"({assessed_ft}) + undecided_ta ({undecided_ta}); unexplained difference "
            f"of {diff}.")
    included = counts.get("included", 0)
    if included > assessed_ft:
        warnings.append(
            f"included ({included}) exceeds assessed_ft ({assessed_ft}) -- a study "
            f"cannot be included without being assessed. This normally means full-text "
            f"screening has not been run.")
    return warnings
