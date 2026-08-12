"""
dedup.py -- de-duplicate output/candidates.csv: exact (normalized) DOI match
first, then fuzzy title match (rapidfuzz token_sort_ratio). Writes
output/candidates_dedup.csv with duplicate_of / dedup_method columns; canonical
records have an empty duplicate_of.

Usage:
    python dedup.py --in output/candidates.csv --out output/candidates_dedup.csv \
        --title-threshold 92

Why this matters more than it looks: every record this file flags as a duplicate
disappears before a human ever sees it (screen.py keeps only canonical rows), and
it is reported as "duplicates removed" in the PRISMA flow diagram. A false merge
here silently deletes a study from the review and no later stage can recover it.
The matching rules below are therefore deliberately conservative in the one
direction that loses data, and deliberately permissive in the direction that only
costs a reviewer a second look.
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

# make the repo root importable so `srp` resolves when run as `python scripts/dedup.py`
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

# One shared definition of "same DOI" / "same title", also used by srp/state.py to
# build record_key. These two used to be separate copies that had already drifted.
from srp.normalize import normalize_doi, normalize_title  # noqa: E402


# Minimum len(shorter)/len(longer) for two titles to be considered the same work.
# A paper whose title is a strict extension of another's ("... : A Survey") is a
# DIFFERENT paper, and this is the guard that says so.
MIN_LENGTH_RATIO = 0.75


def _length_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return min(len(a), len(b)) / max(len(a), len(b))


def title_similarity(a: str, b: str) -> float:
    """Similarity of two normalized titles, 0-100.

    token_sort_ratio, NOT token_set_ratio. token_set_ratio scores 100 whenever one
    title's token set is a SUBSET of the other's, so "Anomaly Detection in IoT
    Networks" vs "Anomaly Detection in IoT Networks Using Federated Learning"
    scored 100 and the second paper was silently deleted as a duplicate. Raising
    the threshold could not fix that -- the score was already maxed. token_sort_ratio
    sorts the tokens and compares the FULL strings, so extra words cost similarity.
    """
    return fuzz.token_sort_ratio(a, b)


def titles_match(a: str, b: str, threshold: int,
                  min_length_ratio: float = MIN_LENGTH_RATIO):
    """Return the similarity score if a and b are the same work, else None."""
    if not a or not b:
        return None
    if _length_ratio(a, b) < min_length_ratio:
        return None
    score = title_similarity(a, b)
    return score if score >= threshold else None


def author_surnames(authors) -> set[str]:
    """Best-effort surnames from the '; '-joined author string search.py emits.

    Handles both shapes the APIs produce: "Jane Smith" and "Smith, Jane".
    Deliberately loose -- this is corroborating evidence for a title match, never
    a match on its own.
    """
    if not isinstance(authors, str) or not authors.strip():
        return set()
    out: set[str] = set()
    for part in authors.split(";"):
        p = part.strip()
        if not p:
            continue
        surname = p.split(",")[0] if "," in p else p.split()[-1]
        surname = re.sub(r"[^a-z]", "", surname.lower())
        if len(surname) >= 2:
            out.add(surname)
    return out


def records_match(title_a, title_b, authors_a, authors_b, threshold: int,
                   min_length_ratio: float = MIN_LENGTH_RATIO):
    """Return a similarity score if these two records are the same work, else None.

    A title match ALONE is not enough, and this is not hypothetical: the shipped
    example corpus contains two distinct 2025/2026 book chapters both titled
    "Machine Learning for Intrusion Detection", by different authors, in different
    venues, with different DOIs. Generic titles are common in this field, so
    merging on title similarity alone silently deletes real studies.

    Requiring a shared surname keeps the preprint/published case working (same
    authors, different DOI and year) while rejecting title collisions between
    unrelated groups. When either side has no parseable author we refuse the
    merge: a missed duplicate costs a reviewer one manual catch during screening,
    a false merge deletes a study from the review permanently, and those costs are
    nowhere near symmetric.
    """
    score = titles_match(title_a, title_b, threshold, min_length_ratio)
    if score is None:
        return None
    surnames_a, surnames_b = author_surnames(authors_a), author_surnames(authors_b)
    if not surnames_a or not surnames_b:
        return None
    if not (surnames_a & surnames_b):
        return None
    return score


def _has_content(value) -> bool:
    return pd.notna(value) and str(value).strip() != ""


def dedup(df: pd.DataFrame, title_threshold: int = 92,
           min_length_ratio: float = MIN_LENGTH_RATIO) -> pd.DataFrame:
    df = df.copy()
    df["doi_norm"] = df["doi"].apply(normalize_doi)
    df["title_norm"] = df["title"].fillna("").apply(normalize_title)
    df["duplicate_of"] = pd.NA
    df["dedup_method"] = pd.NA

    # Union-find over row indices: Pass 1 and Pass 2 below only need to decide
    # WHICH rows belong together, not which one is canonical -- that is resolved
    # once, after both passes, from the full cluster (see "Canonical selection"
    # below). This lets the survivor be picked on data quality (has a DOI, has an
    # abstract) rather than on which row happened to be compared first.
    parent = {idx: idx for idx in df.index}

    def find(i):
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    # dedup_method is recorded against whichever row a merge was actually
    # detected FOR, describing the real comparison that caught it -- even if
    # canonical selection later assigns duplicate_of to a different cluster
    # member than the one it was directly compared against.
    method_of: dict[int, str] = {}

    # Pass 1: exact DOI match.
    seen_doi: dict[str, int] = {}
    for idx in df.index:
        doi = df.at[idx, "doi_norm"]
        if not doi:
            continue
        if doi in seen_doi:
            union(seen_doi[doi], idx)
            method_of[idx] = "doi_exact"
        else:
            seen_doi[doi] = idx

    # Pass 2: fuzzy title + author match over EVERY record -- including records
    # that have a DOI, and across year boundaries.
    #
    # The previous version compared only DOI-less records, bucketed by year. Both
    # restrictions silently defeated the single most common duplicate in a
    # systematic review: an arXiv preprint and its published version. Those differ
    # in year AND carry different DOIs (10.48550/arXiv.x vs 10.1109/y), so they
    # could never meet. Counting one study twice inflates the evidence base, which
    # is exactly what PRISMA's duplicate-removal step exists to prevent.
    #
    # DOI-bearing records are considered first so a match is discovered anchored
    # to a citable record where possible. Cost is O(n^2) title comparisons, which
    # for a review-sized corpus (hundreds to low thousands, bounded by
    # --max-per-source) is well under a second with the length pre-filter.
    # --- core logic: the dedup match loop ---
    candidates = [i for i in df.index if df.at[i, "title_norm"]]
    candidates.sort(key=lambda i: 0 if df.at[i, "doi_norm"] else 1)  # stable: DOI first

    has_authors = "authors" in df.columns
    for pos_a, a in enumerate(candidates):
        title_a, doi_a = df.at[a, "title_norm"], df.at[a, "doi_norm"]
        authors_a = df.at[a, "authors"] if has_authors else ""
        for b in candidates[pos_a + 1:]:
            if find(a) == find(b):
                continue  # already the same cluster
            title_b, doi_b = df.at[b, "title_norm"], df.at[b, "doi_norm"]
            if doi_a and doi_b and doi_a == doi_b:
                continue  # already unioned by pass 1
            authors_b = df.at[b, "authors"] if has_authors else ""
            score = records_match(title_a, title_b, authors_a, authors_b,
                                   title_threshold, min_length_ratio)
            if score is None:
                continue
            # Label cross-DOI merges distinctly: they are the preprint/published
            # case and the one a reviewer may legitimately want to audit.
            kind = "fuzzy_title_crossdoi" if (doi_a and doi_b) else "fuzzy_title"
            union(a, b)
            method_of[b] = f"{kind}_{score:.0f}"

    # Canonical selection, per cluster: has a DOI (citable) > has an abstract
    # (screenable without hunting down the full text) > first-seen. DOI-presence
    # stays the primary axis -- references.bib is built from the canonical
    # record, and an uncitable survivor is a bigger loss than a thin one.
    has_abstract_col = "abstract" in df.columns

    def priority(idx):
        has_doi = bool(df.at[idx, "doi_norm"])
        has_abstract = has_abstract_col and _has_content(df.at[idx, "abstract"])
        return (not has_doi, not has_abstract, idx)

    clusters: dict[int, list[int]] = {}
    for idx in df.index:
        clusters.setdefault(find(idx), []).append(idx)

    for members in clusters.values():
        if len(members) < 2:
            continue
        canonical = min(members, key=priority)
        canonical_id = df.at[canonical, "id"]
        for idx in members:
            if idx == canonical:
                continue
            df.at[idx, "duplicate_of"] = canonical_id
            df.at[idx, "dedup_method"] = method_of.get(idx, "doi_exact")
    return df


def main():
    ap = argparse.ArgumentParser(
        description="De-duplicate a candidates CSV by DOI, then fuzzy title match.")
    ap.add_argument("--in", dest="inp", default="output/candidates.csv")
    ap.add_argument("--out", default="output/candidates_dedup.csv")
    ap.add_argument("--title-threshold", type=int, default=92,
                     help="rapidfuzz token_sort_ratio threshold, 0-100 (default 92)")
    ap.add_argument("--min-length-ratio", type=float, default=MIN_LENGTH_RATIO,
                     help="Two titles are only compared if the shorter is at least this "
                          f"fraction of the longer (default {MIN_LENGTH_RATIO}). This is what "
                          "stops 'X' from swallowing 'X: A Survey' as a duplicate.")
    args = ap.parse_args()

    df = pd.read_csv(args.inp, encoding="utf-8")
    result = dedup(df, args.title_threshold, args.min_length_ratio)
    n_dupes = result["duplicate_of"].notna().sum()
    print(f"{len(result)} records in, {n_dupes} flagged duplicate, "
          f"{len(result) - n_dupes} unique records remain for screening")
    if n_dupes:
        by_method = result["dedup_method"].dropna().str.replace(r"_\d+$", "", regex=True)
        for method, count in by_method.value_counts().items():
            print(f"  {method}: {count}")
    result.to_csv(args.out, index=False, encoding="utf-8")


if __name__ == "__main__":
    main()
