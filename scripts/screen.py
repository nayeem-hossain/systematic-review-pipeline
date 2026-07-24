"""
screen.py -- turn output/candidates_dedup.csv into output/screening.csv: one row
per canonical (non-duplicate) record, with blank title/abstract- and full-text-
stage decision columns for a human reviewer to fill in. Keeping a `reviewer`
column per record (rather than a merged consensus-only decision) is what makes
inter-rater agreement (e.g., Cohen's kappa) computable later, if you duplicate
this file per reviewer.

Usage:
    python screen.py --in output/candidates_dedup.csv --out output/screening.csv

RE-RUNNING IS SAFE. If the output already exists, decisions already recorded in
it are carried over onto the rebuilt sheet, matched by record id. This script
used to truncate the file unconditionally, so the ordinary act of adding a search
source and re-running the pipeline silently destroyed every decision a reviewer
had made -- with an exit code of 0 and a success message. Pass --force to
deliberately start over from blank.
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
import sys
from pathlib import Path

import pandas as pd

SCREENING_COLUMNS = [
    "id", "title", "year", "venue", "doi",
    "abstract",      # carried through so AI-assist and the reviewer can screen on it
    "ta_decision",   # include / exclude / maybe   (title/abstract stage)
    "ta_reason",     # free text, or a code from your exclusion-reason list
    "ft_decision",   # include / exclude            (full-text stage; blank until reached)
    "ft_reason",
    "round",         # which search round/source surfaced this record
    "reviewer",      # reviewer initials -- required for inter-rater reliability later
]

# The columns a human (or an AI-assist pass) fills in. These are what must survive
# a rebuild; everything else is regenerated from the candidates file.
DECISION_COLUMNS = ["ta_decision", "ta_reason", "ft_decision", "ft_reason", "reviewer"]


# --- core logic ---
def build_sheet(df: pd.DataFrame) -> pd.DataFrame:
    """Blank screening sheet for the canonical records in a deduped candidates frame."""
    if "duplicate_of" in df.columns:
        df = df[df["duplicate_of"].isna()]  # screen canonical records only
    return pd.DataFrame({
        "id": df["id"],
        "title": df["title"],
        "year": df.get("year", ""),
        "venue": df.get("venue", ""),
        "doi": df.get("doi", ""),
        "abstract": df.get("abstract", ""),
        "ta_decision": "",
        "ta_reason": "",
        "ft_decision": "",
        "ft_reason": "",
        "round": df.get("source", ""),
        "reviewer": "",
    })[SCREENING_COLUMNS]


def decided_ids(df: pd.DataFrame) -> set:
    """Ids carrying a decision at either stage."""
    if df is None or df.empty or "id" not in df.columns:
        return set()
    mask = pd.Series(False, index=df.index)
    for col in ("ta_decision", "ft_decision"):
        if col in df.columns:
            filled = df[col].astype(str).str.strip().replace({"nan": "", "None": ""})
            mask = mask | (filled != "")
    return set(df.loc[mask, "id"])


def merge_decisions(sheet: pd.DataFrame, existing: pd.DataFrame):
    """Carry decisions from `existing` onto a freshly built `sheet`, matched on id.

    Returns (merged_sheet, stats). Rows present in `existing` but no longer
    canonical (e.g. now flagged as duplicates) are dropped -- that is correct --
    but any that carried a decision are counted in stats["dropped_decided"] so the
    caller can say so out loud rather than losing them silently.
    """
    stats = {"carried": 0, "new": len(sheet), "dropped_decided": 0}
    if existing is None or existing.empty or "id" not in existing.columns:
        return sheet, stats

    merged = sheet.copy()
    old = existing.drop_duplicates(subset="id").set_index("id")
    for col in DECISION_COLUMNS:
        if col not in old.columns:
            continue
        carried = merged["id"].map(old[col])
        merged[col] = carried.where(carried.notna(), merged[col]).fillna("")

    old_decided = decided_ids(existing)
    new_ids = set(sheet["id"])
    stats["carried"] = len(old_decided & new_ids)
    stats["new"] = len(new_ids - set(existing["id"]))
    stats["dropped_decided"] = len(old_decided - new_ids)
    return merged, stats


def main():
    ap = argparse.ArgumentParser(
        description="Build a title/abstract + full-text screening spreadsheet "
                     "from a deduped candidates CSV. Re-running preserves decisions "
                     "already recorded in the output.")
    ap.add_argument("--in", dest="inp", default="output/candidates_dedup.csv")
    ap.add_argument("--out", default="output/screening.csv")
    ap.add_argument("--force", action="store_true",
                     help="Overwrite the output with a blank sheet, DISCARDING every "
                          "decision already recorded in it. Default is to preserve them.")
    args = ap.parse_args()

    df = pd.read_csv(args.inp, encoding="utf-8")
    sheet = build_sheet(df)

    out_path = Path(args.out)
    existing = None
    if out_path.exists() and not args.force:
        try:
            existing = pd.read_csv(out_path, encoding="utf-8")
        except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as e:
            # Refuse rather than silently treat a corrupt sheet as "no decisions".
            print(f"ERROR: {out_path} exists but could not be read ({e}).\n"
                   f"Fix or move it, or pass --force to start from a blank sheet "
                   f"(this DISCARDS any decisions it holds).", file=sys.stderr)
            return 2

    if out_path.exists() and args.force:
        discarding = len(decided_ids(pd.read_csv(out_path, encoding="utf-8")))
        if discarding:
            print(f"--force: discarding {discarding} existing decision(s) in {out_path}")

    sheet, stats = merge_decisions(sheet, existing)
    sheet.to_csv(out_path, index=False, encoding="utf-8")

    print(f"wrote {len(sheet)} rows to {out_path}")
    if existing is not None:
        print(f"  preserved {stats['carried']} existing decision(s); "
               f"{stats['new']} new record(s) added blank")
        if stats["dropped_decided"]:
            print(f"  WARNING: {stats['dropped_decided']} decided record(s) are no longer "
                   f"canonical (now flagged duplicate or absent from the input) and were "
                   f"dropped from the sheet", file=sys.stderr)
    else:
        print(f"  decisions are blank for a human reviewer to fill in (duplicate this "
               f"file per reviewer before computing inter-rater agreement)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
