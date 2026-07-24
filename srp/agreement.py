"""
agreement.py -- inter-rater reliability for screening decisions.

Why this exists: the README mandates dual independent screening with Cohen's
kappa (Stage 3, and the submission checklist asks the user to tick "Cohen's kappa
computed before reconciling"), screen.py's docstring tells you to duplicate the
sheet per reviewer, and extract.py's quality rubric claims the per-dimension
scores are "the raw material Cohen's kappa needs" -- but nothing in the codebase
could compute it. A reviewer's first question about any systematic review is what
the agreement was, and the answer was unavailable.

Kitchenham & Charters SS6.3 and Cochrane SS4.6.1 both require two independent
screeners with an explicit disagreement-resolution procedure. Single-reviewer
screening is a named validity threat, so this module also surfaces the conflicts
themselves -- a kappa without a reconciliation step is just a number.

Dependency-free on purpose: scikit-learn would be a heavy new dependency for one
well-defined formula.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Cohen (1960); the Landis & Koch (1977) bands are the convention reviewers expect
# to see named in a methods section.
_LANDIS_KOCH = [
    (0.81, "almost perfect"),
    (0.61, "substantial"),
    (0.41, "moderate"),
    (0.21, "fair"),
    (0.01, "slight"),
    (-1.01, "poor (worse than chance)"),
]


def interpret_kappa(kappa: float) -> str:
    for floor, label in _LANDIS_KOCH:
        if kappa >= floor:
            return label
    return "undefined"


@dataclass
class AgreementResult:
    n_compared: int = 0
    n_agreed: int = 0
    kappa: float | None = None
    observed_agreement: float = 0.0
    expected_agreement: float = 0.0
    conflicts: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    matrix: dict = field(default_factory=dict)
    note: str = ""

    @property
    def percent_agreement(self) -> float:
        return 100.0 * self.observed_agreement

    def summary(self) -> str:
        if self.kappa is None:
            return f"kappa not computable: {self.note}"
        return (f"Cohen's kappa = {self.kappa:.3f} ({interpret_kappa(self.kappa)}); "
                f"raw agreement {self.percent_agreement:.1f}% "
                f"({self.n_agreed}/{self.n_compared}); {len(self.conflicts)} conflict(s)")


# --- core logic ---
def cohens_kappa(pairs) -> AgreementResult:
    """Cohen's kappa for a sequence of (rater_a_label, rater_b_label) pairs.

    kappa = (Po - Pe) / (1 - Pe), where Po is observed agreement and Pe is the
    agreement expected by chance from each rater's marginal label frequencies.

    Two edge cases matter here and are reported rather than crashed on:
      - Pe == 1: both raters used exactly one label, and the same one. Agreement is
        total but entirely explained by chance, so kappa is undefined (0/0). This is
        NOT a perfect score, and a review that reports 1.0 here is misreporting.
      - no pairs: nothing to compare.
    """
    pairs = [(str(a).strip().lower(), str(b).strip().lower())
             for a, b in pairs
             if str(a).strip() and str(b).strip()]
    result = AgreementResult(n_compared=len(pairs))
    if not pairs:
        result.note = "no records were screened by both reviewers"
        return result

    labels = sorted({label for pair in pairs for label in pair})
    result.labels = labels

    matrix = {a: {b: 0 for b in labels} for a in labels}
    for a, b in pairs:
        matrix[a][b] += 1
    result.matrix = matrix

    n = len(pairs)
    agreed = sum(matrix[label][label] for label in labels)
    result.n_agreed = agreed
    po = agreed / n
    result.observed_agreement = po

    pe = 0.0
    for label in labels:
        marginal_a = sum(matrix[label][b] for b in labels) / n
        marginal_b = sum(matrix[a][label] for a in labels) / n
        pe += marginal_a * marginal_b
    result.expected_agreement = pe

    if abs(1.0 - pe) < 1e-12:
        result.note = ("both reviewers used a single identical label, so chance "
                       "agreement is 100% and kappa is undefined -- report the raw "
                       "agreement instead, and do not report this as kappa = 1.0")
        return result

    result.kappa = (po - pe) / (1.0 - pe)
    return result


def _clean_cell(v) -> str:
    """Blank-safe string for one cell. A CSV round-trip through pandas turns an
    empty string into a float NaN; str(nan) is the non-empty string "nan", which
    would otherwise be compared as if it were a real, agreed-upon label -- two
    reviewers who both left a column blank would show up as "both said nan" /
    perfect agreement on nothing, instead of "neither has extracted this yet"."""
    if v is None:
        return ""
    if isinstance(v, float) and v != v:  # NaN
        return ""
    s = str(v).strip().lower()
    return "" if s == "nan" else s


def compare_reviewers(rows_a: dict, rows_b: dict, stage_col: str = "ta_decision") -> AgreementResult:
    """Compare two {record_id: row} mappings on one decision column.

    Only records BOTH reviewers decided are compared -- that is what kappa is
    defined over. Records only one of them touched are not disagreements, they are
    missing data, and folding them in would silently deflate the statistic.
    """
    shared = [rid for rid in rows_a if rid in rows_b]
    pairs = []
    conflicts = []
    for rid in shared:
        a = _clean_cell(rows_a[rid].get(stage_col, ""))
        b = _clean_cell(rows_b[rid].get(stage_col, ""))
        if not a or not b:
            continue
        pairs.append((a, b))
        if a != b:
            conflicts.append({
                "id": rid,
                "title": rows_a[rid].get("title", ""),
                "reviewer_a": a,
                "reviewer_b": b,
            })
    result = cohens_kappa(pairs)
    result.conflicts = conflicts
    return result


def categorical_agreement(df_a, df_b, columns, id_col: str = "id") -> dict:
    """Per-column Cohen's kappa between two double-EXTRACTED sheets (not
    screening decisions) -- e.g. two extractors' quality_tier / R / A / T / C
    ratings on a piloted subset. Cochrane 5.4 and Kitchenham 6.4 both expect
    the extraction form to be piloted and double-extracted; this is the
    agreement check that makes the pilot worth running.

    Only categorical columns are meaningful here -- free-text fields like
    contribution/limitations have no defined agreement statistic, so the
    caller is responsible for passing only categorical column names (R, A, T,
    C, quality_tier, venue_tier, and any appraisal-instrument rating columns).

    Returns {column: AgreementResult}, keyed only by columns present in both
    frames.
    """
    a_by_id = {row[id_col]: row for row in df_a.to_dict("records")} \
        if hasattr(df_a, "to_dict") else {r[id_col]: r for r in df_a}
    b_by_id = {row[id_col]: row for row in df_b.to_dict("records")} \
        if hasattr(df_b, "to_dict") else {r[id_col]: r for r in df_b}

    out = {}
    for col in columns:
        if col not in df_a.columns or col not in df_b.columns:
            continue
        out[col] = compare_reviewers(a_by_id, b_by_id, stage_col=col)
    return out
