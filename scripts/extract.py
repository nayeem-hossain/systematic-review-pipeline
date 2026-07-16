"""
extract.py -- build output/extraction.csv, the Stage-4/5 data-extraction +
quality-scoring template a human reviewer fills in for every study that
survived full-text screening.

Usage:
    python extract.py --in output/screening.csv --out output/extraction.csv \
        --include-col ft_decision

Reads the screening sheet and keeps only rows marked "include" in
--include-col (default ft_decision, the full-text stage). Falls back to
ta_decision if --include-col is missing from the sheet or entirely blank --
this makes the script runnable as a dry run of the template shape right after
screen.py, before full-text screening has actually happened. `authors` is
joined in by id from --candidates (default output/candidates_dedup.csv),
since the screening sheet itself does not carry an authors column; pass
--candidates '' to skip the join. All other metadata columns (title, year,
venue, doi) are copied straight from the screening sheet.

Everything past the metadata columns is the Stage-4/5 template and is left
blank for a human reviewer to fill in:

  thematic_class  -- which PICOC-Intervention sub-area/theme this study belongs to
  study_type      -- e.g. survey, empirical, benchmark, thesis, position paper
  contribution    -- one-paragraph core contribution
  key_findings    -- quantitative findings, copied verbatim with enough context
                      (dataset, split, baseline, hardware) per README Stage 4 --
                      convert units at synthesis time, not here
  rq_mapping      -- which RQ(s) this study evidences (semicolon-separated)
  limitations     -- the paper's own stated limitations, not the reviewer's opinion
  venue_tier      -- T1 (flagship peer-reviewed venue) / T2 (preprint or workshop
                      paper not yet at a flagship venue) / T3 (other peer-reviewed
                      venue, standards document, or thesis)
  R, A, T, C      -- the four-dimension quality rubric (README Stage 5), each
                      rated Low / Some / High concern (styled after the Cochrane
                      RoB-2 traffic-light convention -- Low = fully satisfied,
                      High = significant gaps or missing):
                        R = Rigor -- methodology fit for the claims made,
                            statistical validity, threats-to-validity
                            discussion, experimental scale
                        A = Artifact availability -- source code, dataset, and
                            hyperparameter availability; independent
                            verifiability
                        T = Threat-model completeness -- explicit adversary/
                            attack model, stated assumptions, disclosed
                            limitations
                        C = Currency -- current datasets/standards (e.g. not
                            solely the outdated KDD Cup 99), or a justified
                            deviation
  quality_tier    -- the aggregate letter grade (A/B/C) derived from R/A/T/C
                      per a published aggregation formula (README Stage 5) --
                      decide and publish that formula before scoring begins,
                      so a different reviewer reproduces the same tier from
                      the same four ratings.
"""
# ===========================================================================
# WHAT YOU CHANGE (safe): the command-line flags -- run `python <this>.py --help`.
#   For a real review: your search terms / thresholds / paths / --mailto, etc.
#   You do NOT need to edit any code below for normal use.
# WHAT YOU DON'T CHANGE (unless an API changes or you are extending the tool):
#   the parts marked "# --- API internals ---" / "# --- core logic ---"
#   (endpoint URLs, pagination, parsing, rate-limit handling, plot geometry).
# ===========================================================================
import argparse
from pathlib import Path

import pandas as pd

EXTRACTION_COLUMNS = [
    "id", "title", "authors", "year", "venue", "doi",
    "thematic_class", "study_type", "contribution", "key_findings",
    "rq_mapping", "limitations",
    "venue_tier", "R", "A", "T", "C", "quality_tier",
]


# --- core logic ---
def pick_included(df: pd.DataFrame, include_col: str) -> tuple[pd.DataFrame, str]:
    """Filter to rows marked 'include' in include_col; fall back to ta_decision
    if include_col is missing or entirely blank (e.g. run right after screen.py,
    before full-text screening has produced any ft_decision values)."""
    col = include_col
    if col not in df.columns or df[col].fillna("").astype(str).str.strip().eq("").all():
        col = "ta_decision"
    decisions = df.get(col, pd.Series(dtype=object)).fillna("").astype(str).str.strip().str.lower()
    return df[decisions.eq("include")].copy(), col


def main():
    ap = argparse.ArgumentParser(
        description="Build the Stage-4/5 data-extraction + quality-scoring "
                     "template from screening rows marked 'include'.")
    ap.add_argument("--in", dest="inp", default="output/screening.csv")
    ap.add_argument("--out", default="output/extraction.csv")
    ap.add_argument("--include-col", default="ft_decision",
                     help="Screening column to filter on (default ft_decision, "
                          "the full-text stage; falls back to ta_decision if "
                          "--include-col is missing or entirely blank)")
    ap.add_argument("--candidates", default="output/candidates_dedup.csv",
                     help="Candidates CSV to join 'authors' from by id -- the "
                          "screening sheet itself does not carry authors; pass "
                          "an empty string to skip the join")
    args = ap.parse_args()

    df = pd.read_csv(args.inp, encoding="utf-8")
    included, used_col = pick_included(df, args.include_col)

    authors_by_id = {}
    if args.candidates:
        cand_path = Path(args.candidates)
        if cand_path.exists():
            cand = pd.read_csv(cand_path, encoding="utf-8")
            if "authors" in cand.columns:
                authors_by_id = dict(zip(cand["id"], cand["authors"]))
        else:
            print(f"note: --candidates file {cand_path} not found -- 'authors' "
                  f"column will be left blank")

    sheet = pd.DataFrame({
        "id": included["id"],
        "title": included.get("title", ""),
        "authors": included["id"].map(authors_by_id).fillna(""),
        "year": included.get("year", ""),
        "venue": included.get("venue", ""),
        "doi": included.get("doi", ""),
        "thematic_class": "",
        "study_type": "",
        "contribution": "",
        "key_findings": "",
        "rq_mapping": "",
        "limitations": "",
        "venue_tier": "",
        "R": "",
        "A": "",
        "T": "",
        "C": "",
        "quality_tier": "",
    })[EXTRACTION_COLUMNS]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.to_csv(out_path, index=False, encoding="utf-8")
    print(f"{len(included)} rows marked include in '{used_col}'; wrote {len(sheet)} "
          f"extraction template rows to {out_path} -- assessment columns are blank "
          f"for a human reviewer to fill in")


if __name__ == "__main__":
    main()
