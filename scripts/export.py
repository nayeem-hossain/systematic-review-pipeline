"""
export.py -- turn the final included-studies set (output/extraction.csv, or
output/screening.csv before extraction has run) into references.bib
(LaTeX/Overleaf) and references.ris (Zotero/Mendeley/EndNote).

Usage:
    python export.py --in output/extraction.csv --included-only
    python export.py --in output/screening.csv --formats ris
"""
# ===========================================================================
# WHAT YOU CHANGE (safe): the command-line flags -- run `python export.py --help`.
#   For a real review: your input CSV / output paths / formats.
#   You do NOT need to edit any code below for normal use.
# WHAT YOU DON'T CHANGE (unless you are extending the tool):
#   the parts marked "# --- core logic ---" (BibTeX/RIS field formatting).
# ===========================================================================
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

# make the repo root importable so `srp` resolves when run as `python scripts/export.py`
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import srp.export
from srp.decisions import pick_progressed

RECORD_COLUMNS = ["id", "title", "authors", "year", "venue", "doi"]


def filter_included(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only rows that have progressed: ft_decision == include if that
    column has any values, else ta_decision in {include, maybe}, else all rows.

    Used to prefer ft_decision merely because the COLUMN EXISTED, even if it
    was entirely blank -- the normal state before full-text screening has run.
    That silently exported zero rows instead of falling back to ta_decision on
    exactly the input extract.py's equivalent fallback handled correctly.
    """
    if "ft_decision" not in df.columns and "ta_decision" not in df.columns:
        print("note: no ft_decision or ta_decision column found -- exporting all rows")
        return df
    filtered, _col = pick_progressed(df, "ft_decision")
    return filtered


def build_records(df: pd.DataFrame) -> list[dict]:
    """Record dicts for srp.export, one per row; missing columns -> ''."""
    sub = pd.DataFrame({c: df[c] if c in df.columns else "" for c in RECORD_COLUMNS})
    sub = sub.fillna("")
    return sub.to_dict(orient="records")


def main():
    ap = argparse.ArgumentParser(
        description="Export the included-studies set to BibTeX and/or RIS.")
    ap.add_argument("--in", dest="inp", required=True,
                     help="input CSV (typically output/extraction.csv, or "
                          "output/screening.csv which has no authors column)")
    ap.add_argument("--out-bib", default="references.bib")
    ap.add_argument("--out-ris", default="references.ris")
    ap.add_argument("--formats", default="bibtex,ris",
                     help="comma list of formats to write: bibtex,ris (default both)")
    ap.add_argument("--included-only", action="store_true",
                     help="keep only rows marked include (ft_decision, falling "
                          "back to ta_decision)")
    args = ap.parse_args()

    df = pd.read_csv(args.inp, encoding="utf-8")
    if args.included_only:
        df = filter_included(df)

    records = build_records(df)
    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}

    written = []
    if "bibtex" in formats:
        out_path = Path(args.out_bib)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(srp.export.to_bibtex(records), encoding="utf-8")
        written.append(str(out_path))
    if "ris" in formats:
        out_path = Path(args.out_ris)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(srp.export.to_ris(records), encoding="utf-8")
        written.append(str(out_path))

    unknown = formats - {"bibtex", "ris"}
    if unknown:
        print(f"note: ignoring unknown format(s): {', '.join(sorted(unknown))}")

    print(f"{len(records)} records exported -- wrote: "
          f"{', '.join(written) if written else '(nothing written -- check --formats)'}")


if __name__ == "__main__":
    main()
