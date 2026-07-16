"""
search.py -- query OpenAlex, Semantic Scholar, Crossref, and arXiv for a set of
search terms over a year range, and write a unified output/candidates.csv.

Worked example (machine-learning / AI-based intrusion detection systems, ML-IDS):

    python search.py \
        --query "intrusion detection AND machine learning" \
        --year-from 2020 --year-to 2026 \
        --mailto you@example.com \
        --max-per-source 40 \
        --out output/candidates.csv

Each source contributes a bounded number of rows (--max-per-source, default 200)
so a first exploratory run stays fast and within API rate limits. Raise the cap
for a real review's search rounds.

API notes -- read before running:
  - OpenAlex      -- no key required, but always pass --mailto; this puts you in
                      OpenAlex's "polite pool" (better rate limits, more reliable
                      service). https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication
  - Semantic Scholar Graph API -- works keyless at a low shared rate limit
                      (~100 req/5 min across all unauthenticated users); pass
                      --s2-api-key (or set S2_API_KEY in the environment) for a
                      higher limit. https://www.semanticscholar.org/product/api
  - Crossref      -- no key required, but always pass --mailto (its own "polite
                      pool" convention). https://api.crossref.org/swagger-ui/index.html
  - arXiv Atom API -- no key, no email required, but respect the informal
                      ~1 request/3 seconds throttle and set a descriptive
                      User-Agent. https://info.arxiv.org/help/api/user-manual.html

A single source failing (network error, HTTP error, rate limit exhausted after
retries) is logged to stderr and skipped -- it does not abort the whole run, so
you still get a candidates.csv from whichever sources succeeded.
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
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

USER_AGENT_TEMPLATE = "systematic-review-pipeline/1.0 (mailto:{mailto})"


@dataclass
class Candidate:
    source: str
    title: str
    authors: str
    year: Optional[int]
    venue: str
    doi: str
    url: str
    abstract: str


def _reconstruct_openalex_abstract(inv_index: Optional[dict]) -> str:
    """OpenAlex serves abstracts as an inverted index {word: [positions]}; rebuild plain text."""
    if not inv_index:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inv_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


# --- API internals: leave unless the API changes ---
def search_openalex(query: str, year_from: int, year_to: int, mailto: str,
                     max_records: int) -> list[Candidate]:
    """OpenAlex works search, cursor-paginated.
    https://docs.openalex.org/api-entities/works/search-works"""
    out: list[Candidate] = []
    cursor = "*"
    base = "https://api.openalex.org/works"
    while len(out) < max_records:
        params = {
            "search": query,
            "filter": f"from_publication_date:{year_from}-01-01,to_publication_date:{year_to}-12-31",
            "per-page": min(200, max_records - len(out)),
            "cursor": cursor,
            "mailto": mailto,
        }
        r = requests.get(base, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for w in data.get("results", []):
            out.append(Candidate(
                source="openalex",
                title=w.get("title") or "",
                authors="; ".join(a["author"]["display_name"] for a in w.get("authorships", [])),
                year=w.get("publication_year"),
                venue=((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
                doi=(w.get("doi") or "").replace("https://doi.org/", ""),
                url=w.get("id", ""),
                abstract=_reconstruct_openalex_abstract(w.get("abstract_inverted_index")),
            ))
            if len(out) >= max_records:
                break
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor or not data.get("results"):
            break
        time.sleep(0.2)  # polite pacing even inside the polite pool
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def search_semantic_scholar(query: str, year_from: int, year_to: int,
                             api_key: Optional[str], max_records: int) -> list[Candidate]:
    """Semantic Scholar Graph API paper search.
    https://api.semanticscholar.org/api-docs/graph"""
    out: list[Candidate] = []
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    headers = {"x-api-key": api_key} if api_key else {}
    offset = 0
    fields = "title,authors,year,venue,externalIds,url,abstract"
    max_retries = 3
    while len(out) < max_records:
        params = {
            "query": query, "year": f"{year_from}-{year_to}",
            "fields": fields, "offset": offset, "limit": min(100, max_records - len(out)),
        }
        retries = 0
        while True:
            r = requests.get(base, params=params, headers=headers, timeout=30)
            if r.status_code == 429 and retries < max_retries:
                retries += 1
                time.sleep(5 * retries)  # unauthenticated pool is rate-limited; back off and retry
                continue
            break
        r.raise_for_status()
        data = r.json()
        for p in data.get("data", []):
            out.append(Candidate(
                source="semanticscholar",
                title=p.get("title") or "",
                authors="; ".join(a.get("name", "") for a in (p.get("authors") or [])),
                year=p.get("year"),
                venue=p.get("venue") or "",
                doi=(p.get("externalIds") or {}).get("DOI", "") or "",
                url=p.get("url") or "",
                abstract=p.get("abstract") or "",
            ))
            if len(out) >= max_records:
                break
        offset += 100
        if offset >= data.get("total", 0):
            break
        time.sleep(1.0)  # unauthenticated rate limit is tight; pace requests
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def search_crossref(query: str, mailto: str, max_records: int) -> list[Candidate]:
    """Crossref works search via query.bibliographic.
    https://api.crossref.org/swagger-ui/index.html"""
    out: list[Candidate] = []
    base = "https://api.crossref.org/works"
    offset = 0
    headers = {"User-Agent": USER_AGENT_TEMPLATE.format(mailto=mailto)}
    while len(out) < max_records:
        params = {"query.bibliographic": query, "rows": min(100, max_records - len(out)),
                   "offset": offset, "mailto": mailto}
        r = requests.get(base, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
        if not items:
            break
        for it in items:
            year = None
            date_parts = (it.get("issued", {}) or {}).get("date-parts") or [[None]]
            if date_parts and date_parts[0]:
                year = date_parts[0][0]
            out.append(Candidate(
                source="crossref",
                title=(it.get("title") or [""])[0],
                authors="; ".join(f"{a.get('given', '')} {a.get('family', '')}".strip()
                                   for a in (it.get("author") or [])),
                year=year,
                venue=(it.get("container-title") or [""])[0],
                doi=it.get("DOI", ""),
                url=it.get("URL", ""),
                abstract=(it.get("abstract", "") or "").replace("<jats:p>", "").replace("</jats:p>", ""),
            ))
            if len(out) >= max_records:
                break
        offset += 100
        time.sleep(0.5)
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def search_arxiv(query: str, mailto: str, max_records: int) -> list[Candidate]:
    """arXiv Atom API -- no key, XML/Atom response.
    https://info.arxiv.org/help/api/user-manual.html"""
    out: list[Candidate] = []
    base = "http://export.arxiv.org/api/query"
    headers = {"User-Agent": USER_AGENT_TEMPLATE.format(mailto=mailto)}
    start = 0
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    # arXiv's query parser does not understand bare "AND"/field syntax the way the
    # scholarly APIs do; a plain all-fields search on the same term string works well
    # enough for discovery and keeps one --query flag usable across all four sources.
    while len(out) < max_records:
        params = {"search_query": f"all:{query}", "start": start,
                   "max_results": min(100, max_records - len(out)), "sortBy": "relevance"}
        r = requests.get(base, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        entries = root.findall("atom:entry", ns)
        if not entries:
            break
        for e in entries:
            id_el = e.find("atom:id", ns)
            if id_el is None or not id_el.text:
                continue
            arxiv_id = id_el.text.rsplit("/", 1)[-1]
            title_el = e.find("atom:title", ns)
            published_el = e.find("atom:published", ns)
            summary_el = e.find("atom:summary", ns)
            out.append(Candidate(
                source="arxiv",
                title=(title_el.text or "").strip().replace("\n", " ") if title_el is not None else "",
                authors="; ".join(
                    a.find("atom:name", ns).text for a in e.findall("atom:author", ns)
                    if a.find("atom:name", ns) is not None
                ),
                year=int(published_el.text[:4]) if published_el is not None and published_el.text else None,
                venue="arXiv",
                doi=f"10.48550/arXiv.{arxiv_id.split('v')[0]}",
                url=f"https://arxiv.org/abs/{arxiv_id}",
                abstract=(summary_el.text or "").strip().replace("\n", " ") if summary_el is not None else "",
            ))
            if len(out) >= max_records:
                break
        start += 100
        time.sleep(3)  # arXiv asks for no more than ~1 request per 3 seconds
    return out[:max_records]


def main():
    ap = argparse.ArgumentParser(
        description="Query OpenAlex, Semantic Scholar, Crossref, and arXiv for a search "
                     "term over a year range; write a unified candidates.csv.")
    ap.add_argument("--query", required=True,
                     help='Search string, e.g. \'"intrusion detection" AND "machine learning"\'')
    ap.add_argument("--year-from", type=int, required=True)
    ap.add_argument("--year-to", type=int, required=True)
    ap.add_argument("--mailto", default=os.environ.get("MAILTO"),
                     help="Contact email for OpenAlex/Crossref polite pools; "
                          "falls back to the MAILTO environment variable")
    ap.add_argument("--s2-api-key", default=os.environ.get("S2_API_KEY"),
                     help="Optional Semantic Scholar API key; falls back to S2_API_KEY env var")
    ap.add_argument("--max-per-source", type=int, default=200,
                     help="Max records to fetch per source (default 200)")
    ap.add_argument("--sources", default="openalex,semanticscholar,crossref,arxiv",
                     help="Comma-separated subset of sources to query "
                          "(default: all four)")
    ap.add_argument("--out", default="output/candidates.csv")
    args = ap.parse_args()

    if not args.mailto:
        ap.error("--mailto is required (or set MAILTO in the environment / .env) -- "
                  "OpenAlex and Crossref rate-limit requests without a contact email")

    requested = {s.strip().lower() for s in args.sources.split(",") if s.strip()}

    records: list[Candidate] = []
    runners = {
        "openalex": lambda: search_openalex(args.query, args.year_from, args.year_to,
                                             args.mailto, args.max_per_source),
        "semanticscholar": lambda: search_semantic_scholar(args.query, args.year_from,
                                                             args.year_to, args.s2_api_key,
                                                             args.max_per_source),
        "crossref": lambda: search_crossref(args.query, args.mailto, args.max_per_source),
        "arxiv": lambda: search_arxiv(args.query, args.mailto, args.max_per_source),
    }
    for name, runner in runners.items():
        if name not in requested:
            continue
        try:
            print(f"querying {name} ...", file=sys.stderr)
            found = runner()
            print(f"  {name}: {len(found)} records", file=sys.stderr)
            records += found
        except requests.RequestException as e:
            print(f"  {name}: FAILED ({e}) -- skipping this source", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(asdict(r) for r in records)
    if df.empty:
        df = pd.DataFrame(columns=["id", "source", "title", "authors", "year", "venue",
                                    "doi", "url", "abstract"])
    else:
        df.insert(0, "id", range(1, len(df) + 1))
    df.to_csv(out_path, index=False, encoding="utf-8")
    print(f"wrote {len(df)} candidate records to {out_path}")


if __name__ == "__main__":
    main()
