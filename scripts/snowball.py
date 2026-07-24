"""
snowball.py -- real citation snowballing: backward (reference lists) and
forward (citing papers) chasing from a seed set, via the OpenAlex citation
graph.

Why this exists: slr.py's between-phase "query expansion" step tokenizes
included titles and suggests keywords -- useful, but it is NOT snowballing in
the sense Wohlin (2014) means, and the tool used to call it that. Real
snowballing walks the citation graph outward from a seed set: for each seed,
read the papers it CITES (backward) and the papers that CITE it (forward),
then screen what comes back. This script is that citation walk.

Usage (typically pointed at a run's included_final.csv):

    python snowball.py --seeds runs/<id>/included_final.csv --mailto you@example.com \
        --direction both --max-per-seed 50 --out runs/<id>/snowball/candidates.csv

Then run the SAME pipeline you already used on the main search -- dedup.py,
screen.py, the review gate -- on the output, because snowball results are
identification candidates, not automatically-included studies, exactly like a
database search's hits. PRISMA 2020's flow-diagram template has a column for
this precisely because it is a distinct identification method from a database
search, not a variant of it -- see figures.py's draw_prisma_2020 docstring.

One round per invocation. Iterating to closure (re-running snowball on the newly
included studies until nothing new survives screening) is a deliberate manual
loop: run this, screen the results, then run it again pointed at the new
included set, until a round adds nothing.
"""
# ===========================================================================
# WHAT YOU CHANGE (safe): the command-line flags -- run `python <this>.py --help`.
# WHAT YOU DON'T CHANGE (unless the API changes): the parts marked
#   "# --- API internals ---" / "# --- core logic ---"
# ===========================================================================
import argparse
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from rapidfuzz import fuzz

# make the repo root importable so `srp` resolves, and reuse search.py's shared
# HTTP/rate-limit plumbing and Candidate shape rather than duplicating it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from srp.env import load_dotenv
from srp.normalize import normalize_doi, normalize_title
from search import (Candidate, RateLimiter, USER_AGENT_TEMPLATE,
                     _reconstruct_openalex_abstract, _request, OPENALEX_MIN_INTERVAL)

OPENALEX_BATCH_SIZE = 50  # OR-filter values per request; OpenAlex's own documented cap
SEED_RESOLVE_MIN_INTERVAL = OPENALEX_MIN_INTERVAL
# Below this token_sort_ratio, a title-search "match" is rejected as unresolved
# rather than accepted -- OpenAlex's title search always returns its single best
# hit even for a title with no real match, so an ungated fallback silently pulls
# in an unrelated paper's entire citation subgraph as if it belonged to the seed.
TITLE_MATCH_THRESHOLD = 85


# --- API internals: leave unless the API changes ---
def resolve_seed(doi: str, title: str, mailto: str, limiter: RateLimiter) -> Optional[dict]:
    """Resolve one seed record to its OpenAlex work object, primarily by DOI
    (reliable), falling back to a title search (best-effort) when no DOI is
    available. Returns None if the seed cannot be found in OpenAlex at all --
    common for grey literature and very new preprints, and not an error.
    https://docs.openalex.org/api-entities/works/get-a-single-work
    """
    headers = {"User-Agent": USER_AGENT_TEMPLATE.format(mailto=mailto)}
    doi_bare = normalize_doi(doi)
    if doi_bare:
        try:
            r = _request("GET", f"https://api.openalex.org/works/https://doi.org/{doi_bare}",
                          limiter, params={"mailto": mailto}, headers=headers)
            return r.json()
        except requests.HTTPError:
            pass  # DOI not in OpenAlex -- fall through to title search

    title = (title or "").strip()
    if not title:
        return None
    try:
        r = _request("GET", "https://api.openalex.org/works", limiter,
                      params={"search": title, "per-page": 1, "mailto": mailto},
                      headers=headers)
        results = r.json().get("results") or []
    except requests.RequestException:
        return None
    if not results:
        return None

    # OpenAlex's title search always returns its single best hit, even for a
    # title with no real match in its index -- an ungated fallback here would
    # silently resolve an unrelated paper and pull in ITS entire citation
    # subgraph as if it were the seed's. Reject anything below the threshold.
    candidate = results[0]
    score = fuzz.token_sort_ratio(normalize_title(title), normalize_title(candidate.get("title")))
    return candidate if score >= TITLE_MATCH_THRESHOLD else None


def _openalex_to_candidate(w: dict, direction: str) -> Candidate:
    return Candidate(
        source=f"openalex-{direction}-snowball",
        title=w.get("title") or "",
        authors="; ".join(a["author"]["display_name"] for a in w.get("authorships", [])),
        year=w.get("publication_year"),
        venue=((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
        doi=(w.get("doi") or "").replace("https://doi.org/", ""),
        url=w.get("id", ""),
        abstract=_reconstruct_openalex_abstract(w.get("abstract_inverted_index")),
    )


# --- API internals: leave unless the API changes ---
def backward_snowball(seed_works: dict, mailto: str, max_per_seed: int,
                       limiter: RateLimiter) -> list:
    """For each resolved seed work, collect the OpenAlex ids it references
    (already present on the seed object -- no extra call needed per seed), then
    batch-resolve those ids into full records. Returns (candidates, id_to_seed_keys).
    """
    headers = {"User-Agent": USER_AGENT_TEMPLATE.format(mailto=mailto)}
    id_to_seeds: dict = {}
    for seed_key, w in seed_works.items():
        refs = (w.get("referenced_works") or [])[:max_per_seed]
        for ref_id in refs:
            short_id = ref_id.rsplit("/", 1)[-1]
            id_to_seeds.setdefault(short_id, []).append(seed_key)

    candidates = []
    ids = list(id_to_seeds)
    for i in range(0, len(ids), OPENALEX_BATCH_SIZE):
        batch = ids[i:i + OPENALEX_BATCH_SIZE]
        params = {"filter": f"openalex_id:{'|'.join(batch)}",
                   "per-page": len(batch), "mailto": mailto}
        r = _request("GET", "https://api.openalex.org/works", limiter,
                      params=params, headers=headers)
        for w in r.json().get("results") or []:
            short_id = w.get("id", "").rsplit("/", 1)[-1]
            candidates.append((_openalex_to_candidate(w, "backward"), id_to_seeds.get(short_id, [])))
    return candidates


# --- API internals: leave unless the API changes ---
def forward_snowball(seed_works: dict, mailto: str, max_per_seed: int,
                      limiter: RateLimiter) -> list:
    """For each resolved seed work, fetch works that cite it via the `cites`
    filter, paginated up to max_per_seed. One or more requests per seed --
    unlike backward, forward citations aren't listed on the seed object itself.
    https://docs.openalex.org/api-entities/works/filter-works#cites
    """
    headers = {"User-Agent": USER_AGENT_TEMPLATE.format(mailto=mailto)}
    out = []
    for seed_key, w in seed_works.items():
        short_id = w.get("id", "").rsplit("/", 1)[-1]
        if not short_id:
            continue
        collected = 0
        cursor = "*"
        while collected < max_per_seed:
            params = {"filter": f"cites:{short_id}",
                      "per-page": min(200, max_per_seed - collected),
                      "cursor": cursor, "mailto": mailto}
            r = _request("GET", "https://api.openalex.org/works", limiter,
                          params=params, headers=headers)
            data = r.json()
            results = data.get("results") or []
            if not results:
                break
            for citing in results:
                out.append((_openalex_to_candidate(citing, "forward"), [seed_key]))
                collected += 1
                if collected >= max_per_seed:
                    break
            cursor = data.get("meta", {}).get("next_cursor")
            if not cursor:
                break
    return out


# --- core logic ---
def run_snowball(seeds: pd.DataFrame, mailto: str, direction: str,
                  max_per_seed: int) -> pd.DataFrame:
    """Resolve every seed to OpenAlex, then walk backward and/or forward from
    the resolved set. Seeds that cannot be resolved (no DOI/title match in
    OpenAlex) are skipped and counted, not silently dropped -- see the
    "unresolved" print in main()."""
    limiter = RateLimiter(SEED_RESOLVE_MIN_INTERVAL)
    seed_works: dict = {}
    unresolved: list = []
    for _, row in seeds.iterrows():
        key = str(row.get("id", row.get("doi", row.get("title", ""))))
        w = resolve_seed(str(row.get("doi", "") or ""), str(row.get("title", "") or ""),
                          mailto, limiter)
        if w is None:
            unresolved.append(key)
            continue
        seed_works[key] = w

    pairs: list = []
    if direction in ("backward", "both"):
        pairs += backward_snowball(seed_works, mailto, max_per_seed, limiter)
    if direction in ("forward", "both"):
        pairs += forward_snowball(seed_works, mailto, max_per_seed, limiter)

    # Dedup within this run: a paper cited by multiple seeds must appear once,
    # with every contributing seed recorded, not once per seed.
    by_url: dict = {}
    for cand, seed_keys in pairs:
        key = cand.url or cand.doi or cand.title
        if key not in by_url:
            by_url[key] = (cand, set())
        by_url[key][1].update(seed_keys)

    rows = []
    for cand, seed_keys in by_url.values():
        d = asdict(cand)
        d["snowball_from"] = "; ".join(sorted(seed_keys))
        rows.append(d)

    df = pd.DataFrame(rows)
    if not df.empty:
        df.insert(0, "id", range(1, len(df) + 1))
        df["year"] = df["year"].astype("Int64")
    print(f"resolved {len(seed_works)}/{len(seeds)} seed(s) in OpenAlex "
           f"({len(unresolved)} unresolved)", file=sys.stderr)
    if unresolved:
        print(f"  unresolved: {', '.join(unresolved[:10])}"
               + (f" (+{len(unresolved) - 10} more)" if len(unresolved) > 10 else ""),
               file=sys.stderr)
    return df


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(
        description="Backward/forward citation snowballing from a seed set, via "
                     "the OpenAlex citation graph (Wohlin 2014 sense -- distinct "
                     "from slr.py's title-term query expansion).")
    ap.add_argument("--seeds", required=True,
                     help="CSV with 'doi' and/or 'title' columns -- typically "
                          "included_final.csv or final_dedup.csv")
    ap.add_argument("--mailto", default=None,
                     help="Contact email for OpenAlex's polite pool; falls back "
                          "to the MAILTO environment variable")
    ap.add_argument("--direction", choices=["backward", "forward", "both"], default="both")
    ap.add_argument("--max-per-seed", type=int, default=50,
                     help="Cap references/citations pulled per seed (default 50)")
    ap.add_argument("--out", default="output/candidates_snowball.csv")
    args = ap.parse_args()

    import os
    mailto = args.mailto or os.environ.get("MAILTO")
    if not mailto:
        ap.error("--mailto is required (or set MAILTO in the environment / .env) -- "
                  "OpenAlex rate-limits requests without a contact email")

    seeds = pd.read_csv(args.seeds, encoding="utf-8")
    if seeds.empty:
        print("no seeds in --seeds file -- nothing to snowball from", file=sys.stderr)
        pd.DataFrame(columns=["id", "source", "title", "authors", "year", "venue",
                               "doi", "url", "abstract", "snowball_from"]) \
            .to_csv(args.out, index=False, encoding="utf-8")
        return 0

    df = run_snowball(seeds, mailto, args.direction, args.max_per_seed)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if df.empty:
        df = pd.DataFrame(columns=["id", "source", "title", "authors", "year", "venue",
                                    "doi", "url", "abstract", "snowball_from"])
    df.to_csv(out_path, index=False, encoding="utf-8")
    print(f"wrote {len(df)} snowball candidate(s) to {out_path} -- run dedup.py / "
          f"screen.py on this file exactly like a database search's output before "
          f"treating any of it as included.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
