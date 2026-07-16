"""
verify_citations.py -- deterministic DOI verification against Crossref. This is
the fabricated-citation guard: it resolves each DOI you give it and checks that
the title you claim for it actually matches what the DOI resolves to. Use this,
not an LLM, to check citations -- an LLM asked "is this citation real?" will
confidently confirm a fabricated one; a script that actually resolves the DOI
and diffs the metadata cannot.

Two input modes:

  1. A CSV of citations to check (e.g., your extraction matrix or a manuscript's
     reference list exported to CSV), with at minimum `doi` and `title` columns:

         python verify_citations.py --csv output/candidates_dedup.csv \
             --doi-col doi --title-col title

  2. A single ad hoc citation on the command line:

         python verify_citations.py --doi 10.1002/ett.4150 \
             --claimed-title "Network intrusion detection system: A systematic study of machine learning and deep learning approaches"

Exit code is 0 if every checked citation passes, 1 if any fails or errors --
convenient for wiring into a pre-submission check.
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
import time

import pandas as pd
import requests
from rapidfuzz import fuzz

TITLE_MATCH_THRESHOLD = 85


# --- API internals: leave unless the API changes ---
def crossref_lookup(doi: str) -> tuple[bool, str, str]:
    """Returns (resolved, real_title, real_authors_str)."""
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}", timeout=20)
    except requests.RequestException as e:
        return False, "", f"request error: {e}"
    if r.status_code != 200:
        return False, "", f"http {r.status_code}"
    msg = r.json().get("message", {})
    real_title = (msg.get("title") or [""])[0]
    real_authors = "; ".join(a.get("family", "") for a in msg.get("author", []) if a.get("family"))
    return True, real_title, real_authors


def verify_one(doi: str, claimed_title: str) -> dict:
    doi = (doi or "").strip()
    claimed_title = (claimed_title or "").strip()
    if not doi:
        return {"doi": doi, "claimed_title": claimed_title, "status": "SKIP",
                "detail": "no DOI given", "title_score": ""}

    resolved, real_title, real_authors = crossref_lookup(doi)
    if not resolved:
        return {"doi": doi, "claimed_title": claimed_title, "status": "FAIL",
                "detail": f"DOI does not resolve on Crossref ({real_authors}) "
                          f"-- likely fabricated or mistyped", "title_score": ""}

    score = round(fuzz.token_set_ratio(claimed_title.lower(), real_title.lower()), 1) if claimed_title else 0
    ok = score >= TITLE_MATCH_THRESHOLD
    return {"doi": doi, "claimed_title": claimed_title, "status": "PASS" if ok else "FAIL",
            "detail": f'resolves to "{real_title}" (authors: {real_authors})',
            "title_score": score}


def main():
    ap = argparse.ArgumentParser(
        description="Verify that claimed citation titles match what their DOI "
                     "actually resolves to on Crossref (fabricated-citation guard).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--csv", help="CSV file of citations to check")
    mode.add_argument("--doi", help="Single DOI to check")
    ap.add_argument("--doi-col", default="doi", help="DOI column name (CSV mode, default 'doi')")
    ap.add_argument("--title-col", default="title", help="Title column name (CSV mode, default 'title')")
    ap.add_argument("--claimed-title", help="Claimed title for the single-DOI mode")
    ap.add_argument("--limit", type=int, default=None, help="Check at most N rows (CSV mode)")
    ap.add_argument("--out", default=None, help="Optional path to write a results CSV")
    args = ap.parse_args()

    if args.doi and not args.claimed_title:
        ap.error("--claimed-title is required alongside --doi")

    results = []
    if args.doi:
        results.append(verify_one(args.doi, args.claimed_title))
    else:
        df = pd.read_csv(args.csv, encoding="utf-8")
        if args.doi_col not in df.columns or args.title_col not in df.columns:
            ap.error(f"CSV must contain '{args.doi_col}' and '{args.title_col}' columns; "
                      f"found: {list(df.columns)}")
        rows = df.head(args.limit) if args.limit else df
        for _, row in rows.iterrows():
            doi = row.get(args.doi_col)
            title = row.get(args.title_col)
            if pd.isna(doi) or not str(doi).strip():
                continue  # nothing to verify for records with no DOI
            result = verify_one(str(doi), str(title) if not pd.isna(title) else "")
            results.append(result)
            time.sleep(0.3)  # polite pacing against Crossref

    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    n_skip = sum(1 for r in results if r["status"] == "SKIP")

    for r in results:
        print(f"[{r['status']}] {r['doi'] or '(no doi)'} -- {r['detail']}"
              + (f" (title match {r['title_score']}/100)" if r["title_score"] != "" else ""))

    print(f"\n{n_pass} passed, {n_fail} failed, {n_skip} skipped, {len(results)} checked total")

    if args.out:
        pd.DataFrame(results).to_csv(args.out, index=False, encoding="utf-8")
        print(f"wrote results to {args.out}")

    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
