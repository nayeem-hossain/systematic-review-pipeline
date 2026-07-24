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
  extraction_reviewer, extraction_date, notes
                  -- who extracted this row, when, and free-text notes.
                      Cochrane 5.4 and Kitchenham 6.4 both expect the
                      extraction form to be piloted and, ideally, filled in
                      independently by two extractors -- these columns are
                      what makes that auditable. See --pilot below.

Pass --instruments (comma-separated keys from srp.appraisal.INSTRUMENTS, e.g.
"dyba_dingsoyr" or "rob2") to append a field-appropriate critical-appraisal
instrument's verbatim domains as additional columns, alongside the R/A/T/C
rubric above (R/A/T/C is this tool's own ML-security-specific quality rubric;
an appraisal instrument is a separate, externally citable check of study
validity -- see README "Field-driven critical appraisal").

Pass --pilot N to sample N included studies into a second, separately-named
sheet with the same blank columns, for a second reviewer to extract
independently before reconciling -- Cochrane 5.4 / Kitchenham 6.4 both expect
the extraction form to be piloted this way. Compare the two afterwards with
scripts/extract.py --compare-a / --compare-b.
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

# make the repo root importable so `srp` resolves when run as `python scripts/extract.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from srp.agreement import categorical_agreement
from srp.appraisal import INSTRUMENTS, instrument_columns
from srp.decisions import pick_progressed

BASE_EXTRACTION_COLUMNS = [
    "id", "title", "authors", "year", "venue", "doi",
    "thematic_class", "study_type", "contribution", "key_findings",
    "rq_mapping", "limitations",
    "venue_tier", "R", "A", "T", "C", "quality_tier",
]
# Piloting/double-extraction tracking (Cochrane 5.4, Kitchenham 6.4) -- always last.
ADMIN_COLUMNS = ["extraction_reviewer", "extraction_date", "notes"]


def build_extraction_columns(instrument_keys: list[str] | None = None) -> list[str]:
    """Column order for extraction.csv: metadata, this tool's R/A/T/C rubric,
    then any field-appropriate appraisal instrument's verbatim domains
    (srp.appraisal), then the piloting/admin columns. Appending rather than
    replacing keeps every existing extraction.csv column name stable.

    Duplicate keys (e.g. a typo'd --instruments rob2,rob2) are collapsed to one
    occurrence. Without this, the same column header appeared twice; pandas
    writes that to CSV without complaint, but reading it back disambiguates
    with a ".1" suffix, silently splitting one instrument's data across two
    differently-named columns.
    """
    cols = list(BASE_EXTRACTION_COLUMNS)
    seen: set = set()
    for key in instrument_keys or []:
        if key in seen:
            continue
        if key not in INSTRUMENTS:
            print(f"WARNING: unknown appraisal instrument '{key}' -- skipping "
                   f"(see srp.appraisal.INSTRUMENTS for valid keys)", file=sys.stderr)
            continue
        seen.add(key)
        cols += instrument_columns(key)
    return cols + ADMIN_COLUMNS


# Kept for backward compatibility with any external code importing the flat list.
EXTRACTION_COLUMNS = build_extraction_columns()


# --- core logic ---
def pick_included(df: pd.DataFrame, include_col: str) -> tuple[pd.DataFrame, str]:
    """Filter to rows that proceed on include_col; fall back to ta_decision
    if include_col is missing or entirely blank (e.g. run right after screen.py,
    before full-text screening has produced any ft_decision values).

    Shared with scripts/export.py via srp.decisions.pick_progressed -- the two
    used to have separate copies, and export.py's checked only whether the
    column EXISTED rather than whether it was blank, so a freshly-built,
    not-yet-filled-in ft_decision column made export.py silently export zero
    rows on input that extract.py handled correctly by falling back.
    """
    return pick_progressed(df, include_col)


def resolve_authors(included: pd.DataFrame, candidates_path: str) -> pd.Series:
    """Authors for the extraction sheet: prefer what `included` already carries;
    otherwise join by id from a candidates CSV. See the docstring on the caller
    for why joining by id on a merged, multi-phase frame is dangerous."""
    if "authors" in included.columns and included["authors"].astype(str).str.strip().ne("").any():
        return included["authors"].fillna("")

    authors_by_id = {}
    if candidates_path:
        cand_path = Path(candidates_path)
        if cand_path.exists():
            cand = pd.read_csv(cand_path, encoding="utf-8")
            if "authors" in cand.columns:
                dupes = cand["id"].duplicated().sum()
                if dupes:
                    print(f"WARNING: --candidates {cand_path} has {dupes} duplicate id(s); "
                           f"an authors join on a non-unique id silently attributes the "
                           f"wrong authors. Skipping the join -- authors left blank.",
                           file=sys.stderr)
                else:
                    authors_by_id = dict(zip(cand["id"], cand["authors"]))
        else:
            print(f"note: --candidates file {cand_path} not found -- 'authors' "
                  f"column will be left blank")
    return included["id"].map(authors_by_id).fillna("") if authors_by_id \
        else pd.Series([""] * len(included), index=included.index)


def build_sheet(included: pd.DataFrame, authors: pd.Series,
                 columns: list[str]) -> pd.DataFrame:
    """The blank extraction template for one set of included studies. `columns`
    is normally build_extraction_columns(instrument_keys) -- passed in rather
    than recomputed so --pilot can build two IDENTICALLY-shaped sheets."""
    base = {
        "id": included["id"], "title": included.get("title", ""), "authors": authors,
        "year": included.get("year", ""), "venue": included.get("venue", ""),
        "doi": included.get("doi", ""),
    }
    for col in columns:
        if col not in base:
            base[col] = ""
    return pd.DataFrame(base)[columns]


def cmd_compare(args) -> int:
    """--compare-a / --compare-b: report per-column agreement between two
    independently double-extracted sheets covering the same studies (the pilot
    Cochrane 5.4 / Kitchenham 6.4 both call for)."""
    df_a = pd.read_csv(args.compare_a, encoding="utf-8")
    df_b = pd.read_csv(args.compare_b, encoding="utf-8")
    categorical_cols = [c for c in ("R", "A", "T", "C", "quality_tier", "venue_tier")
                        if c in df_a.columns and c in df_b.columns]
    if args.instruments:
        for key in args.instruments.split(","):
            key = key.strip()
            if key in INSTRUMENTS:
                categorical_cols += [c for c in instrument_columns(key)
                                     if c in df_a.columns and c in df_b.columns]
    if not categorical_cols:
        print("No shared categorical columns found to compare (R/A/T/C/quality_tier/"
              "venue_tier or --instruments columns).", file=sys.stderr)
        return 1

    results = categorical_agreement(df_a, df_b, categorical_cols)
    for col, r in results.items():
        print(f"{col}: {r.summary()}")
        for c in r.conflicts:
            print(f"    id={c['id']} {c['title'][:50]!r}: A={c['reviewer_a']} vs B={c['reviewer_b']}")
    return 0


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
    ap.add_argument("--instruments", default="",
                     help="Comma-separated appraisal instrument keys (see "
                          "srp.appraisal.INSTRUMENTS) whose verbatim domains are "
                          "appended as extra columns, e.g. 'dyba_dingsoyr' or 'rob2'")
    ap.add_argument("--pilot", type=int, default=0, metavar="N",
                     help="Also write a second, identically-shaped sheet sampling N "
                          "included studies, for a second reviewer to extract "
                          "independently before reconciling (Cochrane 5.4 / "
                          "Kitchenham 6.4). Written to --pilot-out.")
    ap.add_argument("--pilot-out", default="",
                     help="Path for the pilot sheet (default: <out> with a "
                          "'_pilot' suffix before the extension)")
    ap.add_argument("--pilot-seed", type=int, default=42,
                     help="Random seed for the pilot sample, so it's reproducible "
                          "(default 42)")
    ap.add_argument("--compare-a", default=None,
                     help="Instead of building a sheet, compare two completed "
                          "extraction sheets' categorical columns (R/A/T/C/"
                          "quality_tier/venue_tier + any --instruments columns) "
                          "and report Cohen's kappa + conflicts per column. Pass "
                          "with --compare-b.")
    ap.add_argument("--compare-b", default=None)
    args = ap.parse_args()

    if args.compare_a or args.compare_b:
        if not (args.compare_a and args.compare_b):
            print("error: --compare-a and --compare-b must be given together", file=sys.stderr)
            return 2
        return cmd_compare(args)

    df = pd.read_csv(args.inp, encoding="utf-8")
    included, used_col = pick_included(df, args.include_col)

    # Prefer authors the input already carries. slr.py's cross-phase merge attaches
    # them per phase, where ids are unique; re-joining them here by id afterwards is
    # both redundant AND wrong, because ids are only unique WITHIN a phase. On a
    # multi-phase review dict(zip(id, authors)) resolves last-wins, so a phase-1
    # study silently inherited a phase-2 study's authors -- straight into
    # extraction.csv and references.bib.
    authors = resolve_authors(included, args.candidates)

    instrument_keys = [k.strip() for k in args.instruments.split(",") if k.strip()]
    columns = build_extraction_columns(instrument_keys)
    sheet = build_sheet(included, authors, columns)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.to_csv(out_path, index=False, encoding="utf-8")
    print(f"{len(included)} rows marked include in '{used_col}'; wrote {len(sheet)} "
          f"extraction template rows to {out_path} -- assessment columns are blank "
          f"for a human reviewer to fill in")

    if args.pilot > 0:
        if args.pilot >= len(sheet):
            print(f"note: --pilot {args.pilot} >= {len(sheet)} rows available -- "
                  f"piloting the entire sheet", file=sys.stderr)
        pilot_included = included.sample(n=min(args.pilot, len(included)),
                                          random_state=args.pilot_seed)
        pilot_authors = authors.loc[pilot_included.index]
        pilot_sheet = build_sheet(pilot_included, pilot_authors, columns)
        pilot_out = Path(args.pilot_out) if args.pilot_out else \
            out_path.with_name(out_path.stem + "_pilot" + out_path.suffix)
        pilot_out.parent.mkdir(parents=True, exist_ok=True)
        pilot_sheet.to_csv(pilot_out, index=False, encoding="utf-8")
        print(f"wrote a {len(pilot_sheet)}-row pilot sheet (seed={args.pilot_seed}) to "
              f"{pilot_out} -- have a second reviewer extract it independently, then "
              f"run:\n  python extract.py --compare-a {out_path} --compare-b {pilot_out}"
              + (f" --instruments {args.instruments}" if args.instruments else ""))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
