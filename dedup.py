"""
dedup.py -- de-duplicate output/candidates.csv: exact (normalized) DOI match
first, then fuzzy title match (rapidfuzz token_set_ratio) for records with no
DOI. Writes output/candidates_dedup.csv with duplicate_of / dedup_method
columns; canonical records have an empty duplicate_of.

Usage:
    python dedup.py --in output/candidates.csv --out output/candidates_dedup.csv \
        --title-threshold 92
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
import re

import pandas as pd
from rapidfuzz import fuzz


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace -- for matching only, not display."""
    if not isinstance(title, str):
        return ""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_doi(doi) -> str:
    if not isinstance(doi, str) or not doi.strip():
        return ""
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip().lower())


def dedup(df: pd.DataFrame, title_threshold: int = 92) -> pd.DataFrame:
    df = df.copy()
    df["doi_norm"] = df["doi"].apply(normalize_doi)
    df["title_norm"] = df["title"].fillna("").apply(normalize_title)
    df["duplicate_of"] = pd.NA
    df["dedup_method"] = pd.NA

    # Pass 1: exact DOI match. First-seen row per DOI is canonical.
    seen_doi: dict[str, int] = {}
    for idx, row in df.iterrows():
        doi = row["doi_norm"]
        if not doi:
            continue
        if doi in seen_doi:
            df.at[idx, "duplicate_of"] = seen_doi[doi]
            df.at[idx, "dedup_method"] = "doi_exact"
        else:
            seen_doi[doi] = row["id"]

    # Pass 2: fuzzy title match for DOI-less records, bucketed by year to keep
    # this roughly linear instead of O(n^2) over the whole corpus. Cross-year
    # title collisions are rare enough to catch manually during screening if missed here.
    # --- core logic: the dedup match loop ---
    remaining = df[df["duplicate_of"].isna() & (df["doi_norm"] == "")]
    for _, group in remaining.groupby(df["year"].fillna(-1)):
        rows = group.to_dict("records")
        for i in range(len(rows)):
            a_idx = df.index[df["id"] == rows[i]["id"]][0]
            if not pd.isna(df.at[a_idx, "duplicate_of"]):
                continue
            for j in range(i + 1, len(rows)):
                b_idx = df.index[df["id"] == rows[j]["id"]][0]
                if not pd.isna(df.at[b_idx, "duplicate_of"]):
                    continue
                if not rows[i]["title_norm"] or not rows[j]["title_norm"]:
                    continue
                score = fuzz.token_set_ratio(rows[i]["title_norm"], rows[j]["title_norm"])
                if score >= title_threshold:
                    df.at[b_idx, "duplicate_of"] = rows[i]["id"]
                    df.at[b_idx, "dedup_method"] = f"fuzzy_title_{score:.0f}"
    return df


def main():
    ap = argparse.ArgumentParser(
        description="De-duplicate a candidates CSV by DOI, then fuzzy title match.")
    ap.add_argument("--in", dest="inp", default="output/candidates.csv")
    ap.add_argument("--out", default="output/candidates_dedup.csv")
    ap.add_argument("--title-threshold", type=int, default=92,
                     help="rapidfuzz token_set_ratio threshold, 0-100 (default 92)")
    args = ap.parse_args()

    df = pd.read_csv(args.inp, encoding="utf-8")
    result = dedup(df, args.title_threshold)
    n_dupes = result["duplicate_of"].notna().sum()
    print(f"{len(result)} records in, {n_dupes} flagged duplicate, "
          f"{len(result) - n_dupes} unique records remain for screening")
    result.to_csv(args.out, index=False, encoding="utf-8")


if __name__ == "__main__":
    main()
