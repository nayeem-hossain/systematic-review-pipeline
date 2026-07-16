"""
screen.py -- turn output/candidates_dedup.csv into output/screening.csv: one row
per canonical (non-duplicate) record, with blank title/abstract- and full-text-
stage decision columns for a human reviewer to fill in. Keeping a `reviewer`
column per record (rather than a merged consensus-only decision) is what makes
inter-rater agreement (e.g., Cohen's kappa) computable later, if you duplicate
this file per reviewer.

Usage:
    python screen.py --in output/candidates_dedup.csv --out output/screening.csv
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

import pandas as pd

SCREENING_COLUMNS = [
    "id", "title", "year", "venue", "doi",
    "ta_decision",   # include / exclude / maybe   (title/abstract stage)
    "ta_reason",     # free text, or a code from your exclusion-reason list
    "ft_decision",   # include / exclude            (full-text stage; blank until reached)
    "ft_reason",
    "round",         # which search round/source surfaced this record
    "reviewer",      # reviewer initials -- required for inter-rater reliability later
]


def main():
    ap = argparse.ArgumentParser(
        description="Build a title/abstract + full-text screening spreadsheet "
                     "from a deduped candidates CSV.")
    ap.add_argument("--in", dest="inp", default="output/candidates_dedup.csv")
    ap.add_argument("--out", default="output/screening.csv")
    args = ap.parse_args()

    df = pd.read_csv(args.inp, encoding="utf-8")
    if "duplicate_of" in df.columns:
        df = df[df["duplicate_of"].isna()]  # screen canonical records only

    sheet = pd.DataFrame({
        "id": df["id"],
        "title": df["title"],
        "year": df.get("year", ""),
        "venue": df.get("venue", ""),
        "doi": df.get("doi", ""),
        "ta_decision": "",
        "ta_reason": "",
        "ft_decision": "",
        "ft_reason": "",
        "round": df.get("source", ""),
        "reviewer": "",
    })[SCREENING_COLUMNS]

    sheet.to_csv(args.out, index=False, encoding="utf-8")
    print(f"wrote {len(sheet)} rows to {args.out} -- decisions are blank for a human "
          f"reviewer to fill in (duplicate this file per reviewer before computing "
          f"inter-rater agreement)")


if __name__ == "__main__":
    main()
