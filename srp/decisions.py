"""
decisions.py -- the single path by which a parsed AI-assist reply becomes a
recorded screening decision.

This logic used to exist twice: slr.py's _apply_ta_decisions and assist.py's
cmd_parse, ~45 near-identical lines each, and they had already drifted (they wrote
different phase numbers into provenance for the same action). Both sat directly on
the AI-decision ingestion path -- the one place in this pipeline where a machine's
output becomes part of the review's record -- so drift there is drift in the audit
trail.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from srp.normalize import record_key

AI_REVIEWER = "ai-assisted"

_DECISION_COL = {"ta": "ta_decision", "ft": "ft_decision"}
_REASON_COL = {"ta": "ta_reason", "ft": "ft_reason"}

# PRISMA has no "maybe" state, but title/abstract screening produces them --
# the abstract didn't contain enough information to decide. Cochrane's norm is
# to err toward retrieval: a "maybe" proceeds to full-text reading exactly like
# an "include". This must be the ONE place that rule lives, because it used to
# be inconsistent across the codebase -- figures.py counted maybe as assessed at
# full text while the review gate and cross-phase merge silently dropped it, so
# the same screening.csv produced two different denominators depending which
# code path read it. A "maybe" is only ever a TA-stage state: ft_decision is
# terminal (include/exclude), so this constant is deliberately not offered at
# the full-text stage.
TA_PROCEED_DECISIONS = frozenset({"include", "maybe"})


def normalize_decision_series(series: "pd.Series") -> "pd.Series":
    return series.fillna("").astype(str).str.strip().str.lower()


def ta_proceeds_mask(series: "pd.Series") -> "pd.Series":
    """True for ta_decision values that proceed to full-text assessment."""
    return normalize_decision_series(series).isin(TA_PROCEED_DECISIONS)


def pick_progressed(df: "pd.DataFrame", preferred_col: str = "ft_decision"):
    """Filter to rows that have progressed (include, or maybe-at-TA-stage).

    Prefers `preferred_col` (normally the full-text decision, the terminal
    stage); falls back to `ta_decision` if that column is missing OR entirely
    blank. The "entirely blank" check matters: extract.py and export.py used to
    differ here -- extract.py checked for blank-and-fell-back, export.py only
    checked existence, so a `ft_decision` column that existed but had not been
    filled in yet (the normal state before full-text screening runs) made
    export.py silently export zero records instead of falling back to the
    title/abstract decisions like extract.py did on the identical input.

    Returns (filtered_df, resolved_column_name). ta_decision counts "maybe" as
    progressed (a maybe proceeds to full-text reading); ft_decision, being the
    terminal stage, only counts "include".
    """
    col = preferred_col
    if col not in df.columns or normalize_decision_series(df[col]).eq("").all():
        col = "ta_decision"
    if col not in df.columns:
        return df.iloc[0:0].copy(), col
    decisions = normalize_decision_series(df[col])
    proceed_values = TA_PROCEED_DECISIONS if col == "ta_decision" else {"include"}
    return df[decisions.isin(proceed_values)].copy(), col


def id_to_str(v) -> str:
    """Ids survive a CSV round-trip as floats ('61.0') or as namespaced strings
    ('p1_61'). Compare them as canonical strings."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def native(v):
    """numpy scalar -> plain Python, so json.dump can serialize it."""
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            return v
    return v


def _is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:
        return True
    return str(v).strip() in ("", "nan", "None")


@dataclass
class ApplyResult:
    matched: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)
    skipped_decided: list = field(default_factory=list)
    rejected_ft_maybe: list = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)

    def problems(self) -> list[str]:
        out = []
        if self.unmatched:
            out.append(f"{len(self.unmatched)} decision(s) referenced ids not in the "
                        f"sheet and were ignored: {self.unmatched[:5]}")
        if self.skipped_decided:
            out.append(f"{len(self.skipped_decided)} record(s) already had a decision and "
                        f"were left untouched: {self.skipped_decided[:5]} "
                        f"(re-run with overwrite to replace them)")
        if self.rejected_ft_maybe:
            out.append(f"{len(self.rejected_ft_maybe)} full-text decision(s) came back "
                        f"'maybe', which is not a valid ft_decision (it is the terminal "
                        f"stage) -- left undecided, resolve these by hand: "
                        f"{self.rejected_ft_maybe[:5]}")
        return out


# --- core logic ---
def apply_decisions(screening_path, parsed, state, phase: int, stage: str = "ta",
                     overwrite: bool = False) -> ApplyResult:
    """Write parsed AI decisions into a screening sheet and the append-only log.

    By default an existing decision is NEVER replaced. The old code wrote
    unconditionally while preserving a non-blank `reviewer`, so re-parsing a stale
    reply -- or a chatbot echoing an id from an earlier batch -- silently replaced a
    human's decision with the model's while the row went on claiming the human's
    initials. That is a false attribution in the exact file that exists to make
    inter-rater reliability computable.

    When overwrite=True the decision IS replaced, and `reviewer` is reset to
    "ai-assisted" so attribution follows the decision.
    """
    result = ApplyResult()
    screening_path = Path(screening_path)
    decision_col = _DECISION_COL[stage]
    reason_col = _REASON_COL[stage]

    if not screening_path.exists():
        result.unmatched = [rec["id"] for rec in parsed]
        return result

    df = pd.read_csv(screening_path, encoding="utf-8")
    if df.empty or "id" not in df.columns:
        result.unmatched = [rec["id"] for rec in parsed]
        return result

    for col in (decision_col, reason_col, "reviewer"):
        if col in df.columns:
            # An all-blank column reads back as float64 NaN; assigning a string into
            # it would otherwise raise or coerce.
            df[col] = df[col].astype(object)
        else:
            df[col] = ""

    id_index: dict = {}
    for idx, v in df["id"].items():
        id_index.setdefault(id_to_str(v), idx)  # first-wins; duplicates are a bug upstream

    for rec in parsed:
        key = id_to_str(rec["id"])
        idx = id_index.get(key)
        if idx is None:
            result.unmatched.append(rec["id"])
            continue

        if not overwrite and not _is_blank(df.at[idx, decision_col]):
            result.skipped_decided.append(rec["id"])
            continue

        # ft_decision is terminal (include/exclude only); a "maybe" here would
        # silently vanish from the PRISMA counts with no exclusion reason
        # recorded. The prompt no longer offers MAYBE at this stage, but a
        # chatbot can still write one -- refuse it rather than write it.
        if stage == "ft" and rec["decision"] == "maybe":
            result.rejected_ft_maybe.append(rec["id"])
            continue

        df.at[idx, decision_col] = rec["decision"]
        df.at[idx, reason_col] = rec["reason"]
        df.at[idx, "reviewer"] = AI_REVIEWER

        result.matched.append(rec["id"])
        result.counts[rec["decision"]] += 1

        row = df.loc[idx]
        if state is not None:
            state.record_decision(
                record_key=record_key(row.get("doi", ""), row.get("title", "")),
                id=native(row.get("id")),
                decision=rec["decision"],
                stage=stage,
                phase=phase,
                reason=rec["reason"],
                source="manual-paste",
                title=row.get("title", ""),
                doi=row.get("doi", ""),
            )

    df.to_csv(screening_path, index=False, encoding="utf-8")
    return result
