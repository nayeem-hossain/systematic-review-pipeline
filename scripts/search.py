"""
search.py -- query scholarly APIs for a set of search terms over a year range,
and write a unified output/candidates.csv.

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

SOURCES -- five keyless (one of them better with a key), five keyed.
A keyed source with no key supplied is SKIPPED with a note on stderr; it never
errors the run. This is the same "optional integration" contract Semantic
Scholar and CORE already used.

  Keyless (always available):
    openalex        -- pass --mailto for the polite pool (better rate limits).
    crossref        -- pass --mailto (its own polite-pool convention).
    arxiv           -- no key, no email; informal ~1 req/3s throttle.
    doaj            -- no key needed for search (keys are for publishers only).
    pubmed          -- keyless at 3 req/s; --pubmed-api-key raises it to 10 req/s.
    semanticscholar -- works keyless on a low shared pool; --s2-api-key raises it.

  Keyed (opt-in, skipped without a key):
    ieee            -- --ieee-api-key      / IEEE_API_KEY
    scopus          -- --scopus-api-key    / SCOPUS_API_KEY  (+ --scopus-insttoken)
    springer        -- --springer-api-key  / SPRINGER_API_KEY
    core            -- --core-api-key      / CORE_API_KEY
    wos             -- --wos-api-key       / WOS_API_KEY  (Web of Science Expanded)

RATE LIMITS -- every default below is the number the provider publishes; the
constant's comment carries the doc URL. One is a flag rather than a constant,
because the published number depends on your plan:
  --core-min-interval  25 req/min on a personal key, 10 req/min on an academic
                       key (the academic tier trades burst rate for daily quota).

IEEE is a special case: it publishes no rate limit publicly (its Terms of Use
defer to "the Rate Limits displayed during the user registration process"). The
limits shown at registration are 10 calls/second and 200 calls/day, which is
what IEEE_MIN_INTERVAL and IEEE_DAILY_CALL_QUOTA encode. The daily quota is
enforced per-run only -- this script keeps no cross-run counter, so several runs
in one day can still exhaust it.

ABSTRACT AVAILABILITY -- this matters, because screening reads abstracts.
scopus returns abstracts only in the COMPLETE view, which caps page size at 25
instead of 200 and needs entitlement. We default to COMPLETE and fall back to
STANDARD (no abstracts) if your key is refused.

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
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import pandas as pd
import requests

# make the repo root importable so `srp` resolves when run as `python scripts/search.py`
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from srp.env import load_dotenv

USER_AGENT_TEMPLATE = "systematic-review-pipeline/1.0 (mailto:{mailto})"
NCBI_TOOL_NAME = "systematic-review-pipeline"

# --- API internals: documented rate limits ---
# Each value is the MINIMUM SECONDS BETWEEN REQUESTS to that API, derived from
# the provider's own published limit. Do not lower these without checking the
# cited doc -- they are contractual, not stylistic.

# OpenAlex polite pool. https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication
OPENALEX_MIN_INTERVAL = 0.2
# Crossref polite pool. https://api.crossref.org/swagger-ui/index.html
CROSSREF_MIN_INTERVAL = 0.5
# arXiv asks for no more than ~1 request per 3 seconds.
# https://info.arxiv.org/help/api/user-manual.html
ARXIV_MIN_INTERVAL = 3.0
# Semantic Scholar: "1 request per second, cumulative across all endpoints...
# Please set your rate limit to BELOW this threshold to avoid rejected requests."
# Hence 1.1s, not 1.0s -- pacing exactly at the limit is what they warn against.
# The keyless shared pool is stricter still (~100 req/5 min across ALL anonymous
# users), so keyless runs lean on the 429 backoff in _request().
# https://www.semanticscholar.org/product/api
S2_MIN_INTERVAL_KEYLESS = 1.1
S2_MIN_INTERVAL_KEYED = 1.1
# IEEE Xplore: 10 calls/second and 200 calls/day, as shown on the key registration
# page. IEEE publishes no rate limit publicly -- its Terms of Use say limits are
# "set forth on the Rate Limits displayed during the user registration process".
# https://developer.ieee.org/API_Terms_of_Use2
IEEE_MIN_INTERVAL = 1.0 / 10
IEEE_DAILY_CALL_QUOTA = 200
# Scopus Search: 9 requests/second, 20,000 requests/week.
# https://dev.elsevier.com/api_key_settings.html
SCOPUS_MIN_INTERVAL = 1.0 / 9
# Springer Basic plan: 100 requests/minute, 500 requests/day.
# https://dev.springernature.com/docs/rate-limit-details/rate-limits/
SPRINGER_MIN_INTERVAL = 60.0 / 100
# NCBI E-utilities: 3 req/s without an api_key, 10 req/s with one.
# https://www.ncbi.nlm.nih.gov/books/NBK25497/
PUBMED_MIN_INTERVAL_KEYLESS = 1.0 / 3
PUBMED_MIN_INTERVAL_KEYED = 1.0 / 10
# DOAJ: 2 requests/second on all API routes. https://doaj.org/api/v4/docs
DOAJ_MIN_INTERVAL = 0.5
# Web of Science Expanded: 2/3/5 req/sec on Basic/Advanced/Premium institutional
# tiers -- throttle to the most conservative published tier.
# https://developer.clarivate.com/apis/wos
WOS_MIN_INTERVAL = 0.5

# --- API internals: documented page-size ceilings ---
OPENALEX_MAX_PER_REQUEST = 200      # docs.openalex.org per-page cap
S2_MAX_PER_REQUEST = 100            # Graph API paper/search limit cap
CROSSREF_MAX_PER_REQUEST = 100
ARXIV_MAX_PER_REQUEST = 100
IEEE_MAX_PER_REQUEST = 200          # developer.ieee.org: max_records max 200
SCOPUS_VIEW_MAX = {"STANDARD": 200, "COMPLETE": 25}   # dev.elsevier.com/api_key_settings.html
SCOPUS_MAX_TOTAL = 5000             # hard ceiling per query without cursor paging
SPRINGER_MAX_PER_REQUEST = 25       # Basic plan `p` cap (Premium is 100)
PUBMED_ESEARCH_MAX = 10000          # esearch retmax ceiling
PUBMED_EFETCH_BATCH = 500           # NCBI's own recommended efetch batch size
PUBMED_POST_THRESHOLD = 200         # NCBI: use POST above ~200 UIDs
DOAJ_MAX_PER_REQUEST = 100          # "The page size limit is 100"
CORE_MAX_PER_REQUEST = 100          # not officially documented; 100 is the working ceiling
WOS_MAX_PER_REQUEST = 100           # documented `count` range is 0-100

# Default for the one plan-dependent throttle (overridable via a CLI flag).
CORE_DEFAULT_MIN_INTERVAL = 60.0 / 25   # personal key = 25 req/min

KEYLESS_SOURCES = ["openalex", "semanticscholar", "crossref", "arxiv", "pubmed", "doaj"]
KEYED_SOURCES = ["ieee", "scopus", "springer", "core", "wos"]
ALL_SOURCES = KEYLESS_SOURCES + KEYED_SOURCES


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


@dataclass
class SourceReport:
    """What one source was actually asked, and what it actually gave back.

    PRISMA 2020 item 7 requires "the full search strategies for all databases...
    including any filters and limits used", and item 6 the date last searched.
    Neither was recoverable before: candidates.csv carried only a `source` label,
    and the query is TRANSFORMED per source (arXiv gets `all:{q}`, Scopus gets
    TITLE-ABS-KEY(...) AND PUBYEAR..., Crossref gets query.bibliographic) with the
    transformed string discarded. Those have materially different field scopes, so
    "what did you actually run" had no answer.

    `total_available` vs `n_retrieved` is the other half: --max-per-source caps
    every source, and the cap was reported as "records identified". A capped search
    is a convenience sample drawn by each API's undocumented relevance ranking, and
    that must be visible rather than silently folded into the PRISMA count.
    """
    source: str
    query_sent: str = ""
    retrieved_at: str = ""
    n_retrieved: int = 0
    total_available: Optional[int] = None
    truncated: bool = False
    status: str = "ok"          # ok | failed | skipped_no_key
    detail: str = ""


# --- API internals: shared HTTP plumbing ---
class RateLimiter:
    """Enforce a minimum wall-clock interval between successive requests.

    One instance per source per run, so a slow source never starves a fast one.
    """

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


def _retry_after_seconds(resp: requests.Response, fallback: float) -> float:
    """Honour a Retry-After header when the server sends a plain seconds value.

    Elsevier sends X-RateLimit-Reset (epoch seconds) and CORE sends
    X-RateLimit-Retry-After (an ISO-8601 timestamp, not a duration), so a header
    we cannot read as a simple number falls back to caller-supplied backoff.
    """
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return fallback


def _request(method: str, url: str, limiter: RateLimiter, *, max_retries: int = 3,
             **kwargs) -> requests.Response:
    """Rate-limited request with 429 backoff. Raises for any other HTTP error."""
    kwargs.setdefault("timeout", 30)
    attempt = 0
    while True:
        limiter.wait()
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429 and attempt < max_retries:
            attempt += 1
            time.sleep(_retry_after_seconds(resp, fallback=5.0 * attempt))
            continue
        resp.raise_for_status()
        return resp


def _utc_now() -> str:
    """PRISMA item 6: date last searched. UTC and ISO-8601 so it is unambiguous."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_query(report, query_sent: str) -> None:
    """Record the query string ACTUALLY sent to this source, post-transformation."""
    if report is not None and not report.query_sent:
        report.query_sent = query_sent


def _record_total(report, total) -> None:
    """Record how many records the source says exist, so truncation is visible."""
    if report is None or total is None or report.total_available is not None:
        return
    try:
        report.total_available = int(total)
    except (TypeError, ValueError):
        pass


def _as_year(value) -> Optional[int]:
    """Coerce the many year shapes these APIs return ('2020', 2020, '2020-01-01')."""
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip()[:4])
    except (ValueError, TypeError):
        return None


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
                     max_records: int, report: "SourceReport | None" = None) -> list[Candidate]:
    """OpenAlex works search, cursor-paginated.
    https://docs.openalex.org/api-entities/works/search-works"""
    out: list[Candidate] = []
    limiter = RateLimiter(OPENALEX_MIN_INTERVAL)
    cursor = "*"
    base = "https://api.openalex.org/works"
    filter_expr = (f"from_publication_date:{year_from}-01-01,"
                   f"to_publication_date:{year_to}-12-31")
    _record_query(report, f"search={query}&filter={filter_expr}")
    while len(out) < max_records:
        params = {
            "search": query,
            "filter": filter_expr,
            "per-page": min(OPENALEX_MAX_PER_REQUEST, max_records - len(out)),
            "cursor": cursor,
            "mailto": mailto,
        }
        data = _request("GET", base, limiter, params=params).json()
        _record_total(report, (data.get("meta") or {}).get("count"))
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
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def search_semantic_scholar(query: str, year_from: int, year_to: int,
                             api_key: Optional[str], max_records: int,
                             report: "SourceReport | None" = None) -> list[Candidate]:
    """Semantic Scholar Graph API paper search.
    https://api.semanticscholar.org/api-docs/graph"""
    out: list[Candidate] = []
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    headers = {"x-api-key": api_key} if api_key else {}
    limiter = RateLimiter(S2_MIN_INTERVAL_KEYED if api_key else S2_MIN_INTERVAL_KEYLESS)
    offset = 0
    fields = "title,authors,year,venue,externalIds,url,abstract"
    _record_query(report, f"query={query}&year={year_from}-{year_to}")
    while len(out) < max_records:
        params = {
            "query": query, "year": f"{year_from}-{year_to}",
            "fields": fields, "offset": offset,
            "limit": min(S2_MAX_PER_REQUEST, max_records - len(out)),
        }
        data = _request("GET", base, limiter, params=params, headers=headers).json()
        _record_total(report, data.get("total"))
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
        offset += S2_MAX_PER_REQUEST
        if offset >= data.get("total", 0):
            break
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def search_crossref(query: str, mailto: str, max_records: int,
                     report: "SourceReport | None" = None) -> list[Candidate]:
    """Crossref works search via query.bibliographic.
    https://api.crossref.org/swagger-ui/index.html"""
    out: list[Candidate] = []
    base = "https://api.crossref.org/works"
    limiter = RateLimiter(CROSSREF_MIN_INTERVAL)
    offset = 0
    headers = {"User-Agent": USER_AGENT_TEMPLATE.format(mailto=mailto)}
    _record_query(report, f"query.bibliographic={query}")
    while len(out) < max_records:
        params = {"query.bibliographic": query,
                   "rows": min(CROSSREF_MAX_PER_REQUEST, max_records - len(out)),
                   "offset": offset, "mailto": mailto}
        message = _request("GET", base, limiter, params=params,
                            headers=headers).json().get("message", {})
        _record_total(report, message.get("total-results"))
        items = message.get("items", [])
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
        offset += CROSSREF_MAX_PER_REQUEST
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def search_arxiv(query: str, mailto: str, max_records: int,
                  report: "SourceReport | None" = None) -> list[Candidate]:
    """arXiv Atom API -- no key, XML/Atom response.
    https://info.arxiv.org/help/api/user-manual.html"""
    out: list[Candidate] = []
    base = "http://export.arxiv.org/api/query"
    headers = {"User-Agent": USER_AGENT_TEMPLATE.format(mailto=mailto)}
    limiter = RateLimiter(ARXIV_MIN_INTERVAL)
    start = 0
    ns = {"atom": "http://www.w3.org/2005/Atom",
          "opensearch": "http://a9.com/-/spec/opensearch/1.1/"}
    _record_query(report, f"search_query=all:{query}  (NOTE: arXiv's parser does not "
                           f"support the boolean/field syntax the other sources use, so the "
                           f"term string is sent as a plain all-fields search)")
    # arXiv's query parser does not understand bare "AND"/field syntax the way the
    # scholarly APIs do; a plain all-fields search on the same term string works well
    # enough for discovery and keeps one --query flag usable across all sources.
    while len(out) < max_records:
        params = {"search_query": f"all:{query}", "start": start,
                   "max_results": min(ARXIV_MAX_PER_REQUEST, max_records - len(out)),
                   "sortBy": "relevance"}
        r = _request("GET", base, limiter, params=params, headers=headers)
        root = ET.fromstring(r.text)
        total_el = root.find("opensearch:totalResults", ns)
        if total_el is not None:
            _record_total(report, (total_el.text or "").strip())
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
                year=_as_year(published_el.text) if published_el is not None else None,
                venue="arXiv",
                doi=f"10.48550/arXiv.{arxiv_id.split('v')[0]}",
                url=f"https://arxiv.org/abs/{arxiv_id}",
                abstract=(summary_el.text or "").strip().replace("\n", " ") if summary_el is not None else "",
            ))
            if len(out) >= max_records:
                break
        start += ARXIV_MAX_PER_REQUEST
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def _ieee_articles(data: dict) -> list[dict]:
    """Locate the article array in an IEEE response.

    IEEE documents the field names but never documents the response envelope --
    "Data Fields Returned" names the counters (totalfound/totalsearched) and the
    per-article fields, but not the key holding the array. So: try the observed
    key, then fall back to the first list-of-objects in the body.
    https://developer.ieee.org/docs/read/Metadata_API_responses
    """
    articles = data.get("articles")
    if isinstance(articles, list):
        return articles
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def _ieee_authors(article: dict) -> str:
    """IEEE nests authors; the docs list `full_name` and `author_order` as separate
    fields without giving the path, so accept both {"authors": {"authors": [...]}}
    and a bare list."""
    node = article.get("authors")
    if isinstance(node, dict):
        node = node.get("authors")
    if not isinstance(node, list):
        return ""
    return "; ".join(str(a.get("full_name") or "") for a in node if isinstance(a, dict))


def search_ieee(query: str, year_from: int, year_to: int, api_key: str,
                 max_records: int, report: "SourceReport | None" = None) -> list[Candidate]:
    """IEEE Xplore Metadata Search API. Key goes in the query string, not a header.

    Throttled to 10 calls/second and capped at 200 calls, the limits IEEE shows
    on the key registration page. The call cap is per-run: this script keeps no
    cross-run counter, so several runs in one day can still exhaust the daily 200.
    https://developer.ieee.org/docs/read/Searching_the_IEEE_Xplore_Metadata_API
    """
    out: list[Candidate] = []
    base = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
    limiter = RateLimiter(IEEE_MIN_INTERVAL)
    start_record = 1
    calls = 0
    _record_query(report, f"querytext={query}&start_year={year_from}&end_year={year_to}")
    while len(out) < max_records:
        if calls >= IEEE_DAILY_CALL_QUOTA:
            print(f"  ieee: hit the {IEEE_DAILY_CALL_QUOTA} calls/day quota after "
                   f"{len(out)} records -- stopping this source early", file=sys.stderr)
            break
        want = min(IEEE_MAX_PER_REQUEST, max_records - len(out))
        params = {
            "apikey": api_key,
            "querytext": query,
            "start_record": start_record,
            "max_records": want,
            "start_year": year_from,
            "end_year": year_to,
            "format": "json",
        }
        payload = _request("GET", base, limiter, params=params).json()
        calls += 1
        for key in ("total_records", "totalfound", "totalFound"):
            if payload.get(key) is not None:
                _record_total(report, payload[key])
                break
        articles = _ieee_articles(payload)
        if not articles:
            break
        for a in articles:
            out.append(Candidate(
                source="ieee",
                title=a.get("title") or "",
                authors=_ieee_authors(a),
                year=_as_year(a.get("publication_year")),
                venue=a.get("publication_title") or "",
                doi=a.get("doi") or "",
                url=a.get("html_url") or a.get("abstract_url") or "",
                abstract=a.get("abstract") or "",
            ))
            if len(out) >= max_records:
                break
        if len(articles) < want:
            break
        start_record += len(articles)
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def _scopus_authors(entry: dict) -> str:
    """COMPLETE view carries a full author[] array; STANDARD only has dc:creator
    (the first author). https://dev.elsevier.com/sc_search_views.html"""
    authors = entry.get("author")
    if isinstance(authors, list) and authors:
        names = [str(a.get("authname") or a.get("given-name") or "").strip()
                 for a in authors if isinstance(a, dict)]
        joined = "; ".join(n for n in names if n)
        if joined:
            return joined
    return entry.get("dc:creator") or ""


def search_scopus(query: str, year_from: int, year_to: int, api_key: str,
                   insttoken: Optional[str], max_records: int,
                   view: str = "COMPLETE",
                   report: "SourceReport | None" = None) -> list[Candidate]:
    """Elsevier Scopus Search API.

    PUBYEAR takes only strict > and < -- there is no inclusive range operator --
    so an inclusive [year_from, year_to] becomes > year_from-1 AND < year_to+1.
    https://dev.elsevier.com/sc_search_tips.html
    """
    out: list[Candidate] = []
    base = "https://api.elsevier.com/content/search/scopus"
    limiter = RateLimiter(SCOPUS_MIN_INTERVAL)
    headers = {"X-ELS-APIKey": api_key, "Accept": "application/json"}
    if insttoken:
        # Substitutes for institutional IP authentication when off-network.
        # https://dev.elsevier.com/tecdoc_api_authentication.html
        headers["X-ELS-Insttoken"] = insttoken
    scoped = (f"TITLE-ABS-KEY({query}) "
              f"AND PUBYEAR > {year_from - 1} AND PUBYEAR < {year_to + 1}")
    _record_query(report, scoped)
    start = 0
    ceiling = min(max_records, SCOPUS_MAX_TOTAL)
    while len(out) < ceiling:
        want = min(SCOPUS_VIEW_MAX.get(view, 25), ceiling - len(out))
        params = {"query": scoped, "view": view, "start": start, "count": want}
        try:
            r = _request("GET", base, limiter, params=params, headers=headers)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if view == "COMPLETE" and status in (401, 403):
                print("  scopus: COMPLETE view refused (entitlement) -- falling back to "
                       "STANDARD view; abstracts will be EMPTY for scopus rows",
                       file=sys.stderr)
                view = "STANDARD"
                continue
            raise
        results = r.json().get("search-results", {}) or {}
        _record_total(report, results.get("opensearch:totalResults"))
        entries = results.get("entry") or []
        # Scopus signals an empty result set with a single entry carrying "error".
        if len(entries) == 1 and "error" in entries[0]:
            break
        if not entries:
            break
        for e in entries:
            out.append(Candidate(
                source="scopus",
                title=e.get("dc:title") or "",
                authors=_scopus_authors(e),
                year=_as_year(e.get("prism:coverDate") or e.get("prism:coverDisplayDate")),
                venue=e.get("prism:publicationName") or "",
                doi=e.get("prism:doi") or "",
                url=f"https://doi.org/{e['prism:doi']}" if e.get("prism:doi") else "",
                abstract=e.get("dc:description") or "",
            ))
            if len(out) >= ceiling:
                break
        start += len(entries)
        try:
            total = int(results.get("opensearch:totalResults") or 0)
        except (TypeError, ValueError):
            total = 0
        if len(entries) < want or start >= min(total, SCOPUS_MAX_TOTAL):
            break
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def search_springer(query: str, year_from: int, year_to: int, api_key: str,
                     max_records: int,
                     report: "SourceReport | None" = None) -> list[Candidate]:
    """Springer Nature Meta v2 API.

    Year filtering uses datefrom/dateto, NOT `year:` -- the `year:` operator is
    Premium-only and silently unavailable on a free Basic key.
    https://dev.springernature.com/docs/supported-query-params/
    Paging index `s` is 1-based.
    https://dev.springernature.com/docs/advanced-querying/pagination-limits/
    """
    out: list[Candidate] = []
    base = "https://api.springernature.com/meta/v2/json"
    limiter = RateLimiter(SPRINGER_MIN_INTERVAL)
    q = f"{query} datefrom:{year_from}-01-01 dateto:{year_to}-12-31"
    _record_query(report, f"q={q}")
    start = 1
    while len(out) < max_records:
        want = min(SPRINGER_MAX_PER_REQUEST, max_records - len(out))
        params = {"q": q, "s": start, "p": want, "api_key": api_key}
        data = _request("GET", base, limiter, params=params).json()
        result_meta = data.get("result") or []
        if result_meta and isinstance(result_meta[0], dict):
            _record_total(report, result_meta[0].get("total"))
        records = data.get("records") or []
        if not records:
            break
        for rec in records:
            creators = rec.get("creators") or []
            out.append(Candidate(
                source="springer",
                title=rec.get("title") or "",
                authors="; ".join(str(c.get("creator") or "") for c in creators
                                   if isinstance(c, dict)),
                year=_as_year(rec.get("publicationDate") or rec.get("onlineDate")
                               or rec.get("coverDate")),
                venue=rec.get("publicationName") or rec.get("journalTitle") or "",
                doi=rec.get("doi") or "",
                url=f"https://doi.org/{rec['doi']}" if rec.get("doi") else "",
                abstract=rec.get("abstract") or "",
            ))
            if len(out) >= max_records:
                break
        if len(records) < want:
            break
        start += len(records)
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def _pubmed_abstract(article: ET.Element) -> str:
    """Structured abstracts arrive as repeated <AbstractText Label="BACKGROUND">
    siblings; unstructured ones as a single bare element. Join them, prefixing
    the label when present so the section structure survives into screening.
    https://dtd.nlm.nih.gov/ncbi/pubmed/doc/out/190101/el-Abstract.html
    """
    parts: list[str] = []
    for node in article.findall(".//Abstract/AbstractText"):
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        label = node.get("Label")
        parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts)


def _pubmed_parse(xml_text: str) -> list[Candidate]:
    out: list[Candidate] = []
    root = ET.fromstring(xml_text)
    for art in root.findall(".//PubmedArticle"):
        title_el = art.find(".//Article/ArticleTitle")
        journal_el = art.find(".//Article/Journal/Title")
        year_el = art.find(".//Article/Journal/JournalIssue/PubDate/Year")
        if year_el is None:
            year_el = art.find(".//Article/Journal/JournalIssue/PubDate/MedlineDate")
        pmid_el = art.find(".//MedlineCitation/PMID")

        doi = ""
        for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi" and aid.text:
                doi = aid.text.strip()
                break
        if not doi:
            eloc = art.find(".//Article/ELocationID[@EIdType='doi']")
            if eloc is not None and eloc.text:
                doi = eloc.text.strip()

        names = []
        for a in art.findall(".//Article/AuthorList/Author"):
            last = a.findtext("LastName")
            fore = a.findtext("ForeName")
            collective = a.findtext("CollectiveName")
            if last or fore:
                names.append(" ".join(p for p in (fore, last) if p))
            elif collective:
                names.append(collective)

        pmid = (pmid_el.text or "").strip() if pmid_el is not None else ""
        out.append(Candidate(
            source="pubmed",
            title="".join(title_el.itertext()).strip() if title_el is not None else "",
            authors="; ".join(names),
            year=_as_year(year_el.text) if year_el is not None else None,
            venue=journal_el.text or "" if journal_el is not None else "",
            doi=doi,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            abstract=_pubmed_abstract(art),
        ))
    return out


def search_pubmed(query: str, year_from: int, year_to: int, api_key: Optional[str],
                   mailto: str, max_records: int,
                   report: "SourceReport | None" = None) -> list[Candidate]:
    """PubMed via NCBI E-utilities: esearch for PMIDs, then efetch in batches.

    Keyless is 3 req/s; an api_key raises it to 10 req/s.
    https://www.ncbi.nlm.nih.gov/books/NBK25497/
    NCBI requires `tool` and `email` on every call, and recommends efetch
    batches of 500 via POST once the ID list is large.
    https://www.ncbi.nlm.nih.gov/books/NBK25499/
    """
    limiter = RateLimiter(PUBMED_MIN_INTERVAL_KEYED if api_key
                           else PUBMED_MIN_INTERVAL_KEYLESS)
    common = {"db": "pubmed", "tool": NCBI_TOOL_NAME, "email": mailto}
    if api_key:
        common["api_key"] = api_key

    esearch = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {
        **common,
        "term": query,
        "retmax": min(max_records, PUBMED_ESEARCH_MAX),
        "retstart": 0,
        "datetype": "pdat",
        "mindate": str(year_from),
        "maxdate": str(year_to),
        "retmode": "json",
    }
    _record_query(report, f"term={query}&datetype=pdat&mindate={year_from}&maxdate={year_to}")
    esearch_result = (_request("GET", esearch, limiter, params=params)
                      .json().get("esearchresult", {}))
    _record_total(report, esearch_result.get("count"))
    ids = esearch_result.get("idlist", [])[:max_records]
    if not ids:
        return []

    efetch = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    out: list[Candidate] = []
    for i in range(0, len(ids), PUBMED_EFETCH_BATCH):
        batch = ids[i:i + PUBMED_EFETCH_BATCH]
        body = {**common, "id": ",".join(batch), "retmode": "xml"}
        method = "POST" if len(batch) > PUBMED_POST_THRESHOLD else "GET"
        kwargs = {"data": body} if method == "POST" else {"params": body}
        r = _request(method, efetch, limiter, **kwargs)
        out += _pubmed_parse(r.text)
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def _doaj_doi(bibjson: dict) -> str:
    """DOAJ types its identifiers rather than keying them: identifier is a list of
    {id, type} with type in {doi, eissn, pissn, ...}."""
    for ident in bibjson.get("identifier") or []:
        if isinstance(ident, dict) and ident.get("type") == "doi":
            return str(ident.get("id") or "")
    return ""


def search_doaj(query: str, year_from: int, year_to: int,
                 max_records: int,
                 report: "SourceReport | None" = None) -> list[Candidate]:
    """DOAJ article search. No API key needed -- DOAJ keys exist only for
    publishers submitting data. Search is rate-limited to 2 req/s.
    The query is a URL-encoded PATH segment, not a query parameter.
    https://doaj.org/api/v4/docs
    """
    out: list[Candidate] = []
    limiter = RateLimiter(DOAJ_MIN_INTERVAL)
    lucene = f"{query} AND bibjson.year:[{year_from} TO {year_to}]"
    _record_query(report, lucene)
    base = "https://doaj.org/api/v4/search/articles/" + quote(lucene, safe="")
    page = 1
    while len(out) < max_records:
        want = min(DOAJ_MAX_PER_REQUEST, max_records - len(out))
        params = {"page": page, "pageSize": want}
        data = _request("GET", base, limiter, params=params).json()
        _record_total(report, data.get("total"))
        results = data.get("results") or []
        if not results:
            break
        for rec in results:
            bj = rec.get("bibjson") or {}
            url = ""
            for link in bj.get("link") or []:
                if isinstance(link, dict) and link.get("type") == "fulltext":
                    url = str(link.get("url") or "")
                    break
            out.append(Candidate(
                source="doaj",
                title=bj.get("title") or "",
                authors="; ".join(str(a.get("name") or "") for a in (bj.get("author") or [])
                                   if isinstance(a, dict)),
                year=_as_year(bj.get("year")),
                venue=(bj.get("journal") or {}).get("title") or "",
                doi=_doaj_doi(bj),
                url=url,
                abstract=bj.get("abstract") or "",
            ))
            if len(out) >= max_records:
                break
        if len(results) < want:
            break
        page += 1
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def search_core(query: str, year_from: int, year_to: int, api_key: str,
                 max_records: int, min_interval: float,
                 report: "SourceReport | None" = None) -> list[Candidate]:
    """CORE v3 works search (POST, so long boolean queries are not URL-limited).

    CORE bills in tokens rather than requests: a simple query costs 1 token,
    complex ones 3-5. Rate limits are per-minute + per-day token budgets that
    depend on your key's tier. https://api.core.ac.uk/docs/v3#section/Rate-limits
    Note the live API returns camelCase fields even though the OpenAPI spec
    documents snake_case; the docs' own examples use camelCase.
    """
    out: list[Candidate] = []
    base = "https://api.core.ac.uk/v3/search/works"
    limiter = RateLimiter(min_interval)
    headers = {"Authorization": f"Bearer {api_key}"}
    q = (f"({query}) AND yearPublished>={year_from} "
         f"AND yearPublished<={year_to}")
    _record_query(report, q)
    offset = 0
    while len(out) < max_records:
        want = min(CORE_MAX_PER_REQUEST, max_records - len(out))
        body = {"q": q, "limit": want, "offset": offset}
        data = _request("POST", base, limiter, json=body, headers=headers).json()
        _record_total(report, data.get("totalHits"))
        results = data.get("results") or []
        if not results:
            break
        for rec in results:
            journals = rec.get("journals") or []
            venue = ""
            if journals and isinstance(journals[0], dict):
                venue = str(journals[0].get("title") or "")
            out.append(Candidate(
                source="core",
                title=rec.get("title") or "",
                authors="; ".join(str(a.get("name") or "") for a in (rec.get("authors") or [])
                                   if isinstance(a, dict)),
                year=_as_year(rec.get("yearPublished")),
                venue=venue or str(rec.get("publisher") or ""),
                doi=rec.get("doi") or "",
                url=rec.get("downloadUrl") or "",
                abstract=rec.get("abstract") or "",
            ))
            if len(out) >= max_records:
                break
        if len(results) < want:
            break
        offset += len(results)
    return out[:max_records]


# --- API internals: leave unless the API changes ---
def _wos_as_list(node):
    """WoS's XML-derived JSON collapses a repeatable field to a bare dict (or
    bare string, for abstract paragraphs) when there is exactly one, and a
    list when there are more. Every repeatable field -- names, identifiers,
    titles, abstract paragraphs -- needs this before iterating. Verified
    against a real record pulled live from the API, not just documentation.
    """
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]


def _wos_titles(rec: dict) -> dict:
    """type -> content map from static_data.summary.titles.title[] -- 'item'
    is the paper title, 'source' is the journal/book title."""
    titles = (rec.get("static_data", {}).get("summary", {})
              .get("titles", {}).get("title"))
    out = {}
    for t in _wos_as_list(titles):
        if isinstance(t, dict) and t.get("type"):
            out[t["type"]] = t.get("content") or ""
    return out


def _wos_authors(rec: dict) -> str:
    names = rec.get("static_data", {}).get("summary", {}).get("names", {}).get("name")
    out = []
    for n in _wos_as_list(names):
        if not isinstance(n, dict):
            continue
        if n.get("role") not in (None, "author"):
            continue  # skip book editors etc. -- only report study authors
        display = n.get("display_name") or n.get("full_name") or ""
        if display:
            out.append(display)
    return "; ".join(out)


def _wos_doi(rec: dict) -> str:
    idents = (rec.get("dynamic_data", {}).get("cluster_related", {})
              .get("identifiers", {}).get("identifier"))
    for ident in _wos_as_list(idents):
        if isinstance(ident, dict) and ident.get("type") == "doi":
            return ident.get("value") or ""
    return ""


def _wos_abstract(rec: dict) -> str:
    abstracts = rec.get("static_data", {}).get("fullrecord_metadata", {}).get("abstracts")
    if not isinstance(abstracts, dict):
        return ""
    paragraphs = []
    for a in _wos_as_list(abstracts.get("abstract")):
        if not isinstance(a, dict):
            continue
        p = a.get("abstract_text", {})
        p = p.get("p") if isinstance(p, dict) else None
        paragraphs.extend(s for s in _wos_as_list(p) if isinstance(s, str))
    return " ".join(paragraphs)


def search_wos(query: str, year_from: int, year_to: int, api_key: str,
                max_records: int, report: "SourceReport | None" = None) -> list[Candidate]:
    """Clarivate Web of Science Expanded API.

    Pagination is a plain repeat of usrQuery with firstRecord incremented --
    verified live that this returns genuinely different records page to page,
    despite the response also carrying a QueryID (that field tracks the search
    execution; it is not required to continue paging).

    Rate limit is plan-dependent (2/3/5 req/sec on Basic/Advanced/Premium);
    WOS_MIN_INTERVAL throttles to the most conservative published tier --
    raise it if your plan is higher.
    https://developer.clarivate.com/apis/wos
    """
    out: list[Candidate] = []
    base = "https://wos-api.clarivate.com/api/wos"
    limiter = RateLimiter(WOS_MIN_INTERVAL)
    headers = {"X-ApiKey": api_key, "Accept": "application/json"}
    usr_query = f"TS=({query}) AND PY=({year_from}-{year_to})"
    _record_query(report, usr_query)
    first_record = 1
    while len(out) < max_records:
        want = min(WOS_MAX_PER_REQUEST, max_records - len(out))
        params = {
            "databaseId": "WOS", "usrQuery": usr_query,
            "count": want, "firstRecord": first_record, "optionView": "FR",
        }
        payload = _request("GET", base, limiter, params=params, headers=headers).json()
        result = payload.get("QueryResult") or {}
        _record_total(report, result.get("RecordsFound"))
        records_node = payload.get("Data", {}).get("Records", {}).get("records")
        recs = _wos_as_list(records_node.get("REC")) if isinstance(records_node, dict) else []
        if not recs:
            break
        for rec in recs:
            titles = _wos_titles(rec)
            doi = _wos_doi(rec)
            out.append(Candidate(
                source="wos",
                title=titles.get("item", ""),
                authors=_wos_authors(rec),
                year=_as_year(rec.get("static_data", {}).get("summary", {})
                              .get("pub_info", {}).get("pubyear")),
                venue=titles.get("source", ""),
                doi=doi,
                url=f"https://doi.org/{doi}" if doi else "",
                abstract=_wos_abstract(rec),
            ))
            if len(out) >= max_records:
                break
        if len(recs) < want:
            break
        first_record += len(recs)
    return out[:max_records]


# --- core logic ---
def build_runners(args, reports: dict) -> dict:
    """Map source name -> one-arg callable taking its SourceReport. Keyed sources
    whose key is absent are mapped to None so main() can skip them with a note."""
    r = reports
    return {
        "openalex": lambda: search_openalex(args.query, args.year_from, args.year_to,
                                             args.mailto, args.max_per_source, r["openalex"]),
        "semanticscholar": lambda: search_semantic_scholar(args.query, args.year_from,
                                                             args.year_to, args.s2_api_key,
                                                             args.max_per_source,
                                                             r["semanticscholar"]),
        "crossref": lambda: search_crossref(args.query, args.mailto, args.max_per_source,
                                             r["crossref"]),
        "arxiv": lambda: search_arxiv(args.query, args.mailto, args.max_per_source,
                                       r["arxiv"]),
        "pubmed": lambda: search_pubmed(args.query, args.year_from, args.year_to,
                                         args.pubmed_api_key, args.mailto,
                                         args.max_per_source, r["pubmed"]),
        "doaj": lambda: search_doaj(args.query, args.year_from, args.year_to,
                                     args.max_per_source, r["doaj"]),
        "ieee": (lambda: search_ieee(args.query, args.year_from, args.year_to,
                                      args.ieee_api_key, args.max_per_source,
                                      r["ieee"])) if args.ieee_api_key else None,
        "scopus": (lambda: search_scopus(args.query, args.year_from, args.year_to,
                                          args.scopus_api_key, args.scopus_insttoken,
                                          args.max_per_source, args.scopus_view,
                                          r["scopus"])) if args.scopus_api_key else None,
        "springer": (lambda: search_springer(args.query, args.year_from, args.year_to,
                                              args.springer_api_key, args.max_per_source,
                                              r["springer"])) if args.springer_api_key else None,
        "core": (lambda: search_core(args.query, args.year_from, args.year_to,
                                      args.core_api_key, args.max_per_source,
                                      args.core_min_interval,
                                      r["core"])) if args.core_api_key else None,
        "wos": (lambda: search_wos(args.query, args.year_from, args.year_to,
                                    args.wos_api_key, args.max_per_source,
                                    r["wos"])) if args.wos_api_key else None,
    }


KEY_FLAG_HINT = {
    "ieee": "--ieee-api-key / IEEE_API_KEY",
    "scopus": "--scopus-api-key / SCOPUS_API_KEY",
    "springer": "--springer-api-key / SPRINGER_API_KEY",
    "core": "--core-api-key / CORE_API_KEY",
    "wos": "--wos-api-key / WOS_API_KEY",
}

SECRET_ARGS = ["s2_api_key", "ieee_api_key", "scopus_api_key", "scopus_insttoken",
               "springer_api_key", "pubmed_api_key", "core_api_key", "wos_api_key"]


def make_redactor(args):
    """Return f(text) -> text with every supplied key masked.

    IEEE, Springer and NCBI take the key as a QUERY PARAMETER, and requests puts
    the full URL into its HTTPError message -- so an unredacted failure prints a
    live credential to stderr, where it reaches logs and pasted bug reports.
    """
    secrets: set[str] = set()
    for name in SECRET_ARGS:
        value = (getattr(args, name, None) or "").strip()
        if value:
            secrets.add(value)
            secrets.add(quote(value, safe=""))   # as it appears once URL-encoded

    def redact(text: str) -> str:
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "***REDACTED***")
        return text

    return redact


def main():
    # Must precede add_argument: every key/mailto flag uses default=os.environ.get(...),
    # which argparse evaluates right here. Loading .env afterwards would have no effect.
    load_dotenv()

    ap = argparse.ArgumentParser(
        description="Query scholarly APIs for a search term over a year range; "
                     "write a unified candidates.csv. Keyed sources without a key "
                     "are skipped, never fatal.")
    ap.add_argument("--query", required=True,
                     help='Search string, e.g. \'"intrusion detection" AND "machine learning"\'')
    ap.add_argument("--year-from", type=int, required=True)
    ap.add_argument("--year-to", type=int, required=True)
    ap.add_argument("--mailto", default=os.environ.get("MAILTO"),
                     help="Contact email for OpenAlex/Crossref polite pools and the "
                          "NCBI-required email param; falls back to the MAILTO env var")
    ap.add_argument("--max-per-source", type=int, default=200,
                     help="Max records to fetch per source (default 200)")
    ap.add_argument("--sources", default=",".join(ALL_SOURCES),
                     help=f"Comma-separated subset of sources to query "
                          f"(default: all -- {', '.join(ALL_SOURCES)})")
    ap.add_argument("--out", default="output/candidates.csv")
    ap.add_argument("--strategy-log", default=None,
                     help="Path for the per-source search-strategy log (the exact query sent "
                          "to each source, when, how many records it returned, and how many "
                          "it says exist). PRISMA items 6 and 7 require this and it cannot be "
                          "reconstructed afterwards. Default: <out>_search_strategy.csv")

    keys = ap.add_argument_group(
        "optional API keys",
        "Every key is optional. A keyed source with no key is skipped with a note.")
    keys.add_argument("--s2-api-key", default=os.environ.get("S2_API_KEY"),
                       help="Semantic Scholar key -- raises the rate limit above the "
                            "shared keyless pool; falls back to S2_API_KEY env var")
    keys.add_argument("--ieee-api-key", default=os.environ.get("IEEE_API_KEY"),
                       help="IEEE Xplore key (developer.ieee.org); falls back to IEEE_API_KEY")
    keys.add_argument("--scopus-api-key", default=os.environ.get("SCOPUS_API_KEY"),
                       help="Elsevier Scopus key (dev.elsevier.com); falls back to SCOPUS_API_KEY")
    keys.add_argument("--scopus-insttoken", default=os.environ.get("SCOPUS_INSTTOKEN"),
                       help="Elsevier institutional token, for use OFF your institution's "
                            "network (Scopus keys are IP-authenticated otherwise); "
                            "falls back to SCOPUS_INSTTOKEN")
    keys.add_argument("--springer-api-key", default=os.environ.get("SPRINGER_API_KEY"),
                       help="Springer Nature key (dev.springernature.com); falls back to "
                            "SPRINGER_API_KEY")
    keys.add_argument("--pubmed-api-key", default=os.environ.get("PUBMED_API_KEY"),
                       help="NCBI key -- pubmed works WITHOUT one at 3 req/s; a key raises "
                            "it to 10 req/s; falls back to PUBMED_API_KEY")
    keys.add_argument("--core-api-key", default=os.environ.get("CORE_API_KEY"),
                       help="CORE key (core.ac.uk/services/api); falls back to CORE_API_KEY")
    keys.add_argument("--wos-api-key", default=os.environ.get("WOS_API_KEY"),
                       help="Web of Science Expanded API key (developer.clarivate.com); "
                            "falls back to WOS_API_KEY")

    tuning = ap.add_argument_group(
        "plan-dependent rate limits",
        "Defaults are the most conservative published tier. Raise them only to match "
        "what your own key's plan actually allows.")
    tuning.add_argument("--core-min-interval", type=float, default=CORE_DEFAULT_MIN_INTERVAL,
                         help="Min seconds between CORE requests. Personal key is 25 req/min "
                              "(default 2.4s); an academic key is 10 req/min -- use 6.0 there.")
    tuning.add_argument("--scopus-view", choices=["COMPLETE", "STANDARD"], default="COMPLETE",
                         help="Scopus result view. COMPLETE returns abstracts but caps pages "
                              "at 25; STANDARD pages at 200 but returns NO abstract. Default "
                              "COMPLETE, auto-falling back to STANDARD if your key is refused.")

    args = ap.parse_args()

    if not args.mailto:
        ap.error("--mailto is required (or set MAILTO in the environment / .env) -- "
                  "OpenAlex and Crossref rate-limit requests without a contact email, "
                  "and NCBI requires it on every E-utilities call")

    requested = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in requested if s not in ALL_SOURCES]
    if unknown:
        ap.error(f"unknown source(s): {', '.join(unknown)}. "
                  f"Valid: {', '.join(ALL_SOURCES)}")

    reports = {name: SourceReport(source=name) for name in ALL_SOURCES}
    runners = build_runners(args, reports)
    redact = make_redactor(args)

    records: list[Candidate] = []
    attempted: list[str] = []
    failed: list[str] = []
    for name in requested:
        runner = runners[name]
        report = reports[name]
        if runner is None:
            report.status = "skipped_no_key"
            report.detail = KEY_FLAG_HINT[name]
            print(f"skipping {name}: no API key supplied "
                   f"({KEY_FLAG_HINT[name]})", file=sys.stderr)
            continue
        attempted.append(name)
        report.retrieved_at = _utc_now()
        try:
            print(f"querying {name} ...", file=sys.stderr)
            found = runner()
            report.n_retrieved = len(found)
            if report.total_available is not None:
                report.truncated = report.total_available > len(found)
            print(f"  {name}: {len(found)} records"
                   + (f" of {report.total_available} available" if report.total_available
                      is not None else ""), file=sys.stderr)
            records += found
        except requests.RequestException as e:
            failed.append(name)
            report.status, report.detail = "failed", redact(str(e))
            print(f"  {name}: FAILED ({redact(str(e))}) -- skipping this source",
                   file=sys.stderr)
        except ET.ParseError as e:
            failed.append(name)
            report.status, report.detail = "failed", f"malformed XML: {redact(str(e))}"
            print(f"  {name}: FAILED (malformed XML response: {redact(str(e))}) -- "
                   f"skipping this source", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # PRISMA items 6 and 7: what was run, where, when, and what was left behind.
    strategy_path = Path(args.strategy_log) if args.strategy_log \
        else out_path.with_name(out_path.stem + "_search_strategy.csv")
    strategy_rows = [asdict(reports[name]) for name in requested]
    pd.DataFrame(strategy_rows, columns=[f.name for f in fields(SourceReport)]) \
        .to_csv(strategy_path, index=False, encoding="utf-8")

    truncated = [r for r in (reports[n] for n in requested) if r.truncated]
    if truncated:
        print("\nWARNING -- the search was TRUNCATED by --max-per-source "
               f"({args.max_per_source}):", file=sys.stderr)
        for r in truncated:
            print(f"  {r.source}: retrieved {r.n_retrieved} of {r.total_available} "
                   f"available ({r.total_available - r.n_retrieved} not fetched)",
                   file=sys.stderr)
        print("These counts are NOT 'records identified' in the PRISMA sense -- they are a "
               "relevance-ranked sample drawn by each API's own undocumented ranking. Raise "
               "--max-per-source for a real search round, or report the truncation "
               f"explicitly. Per-source detail: {strategy_path}\n", file=sys.stderr)

    df = pd.DataFrame(asdict(r) for r in records)
    if df.empty:
        df = pd.DataFrame(columns=["id", "source", "title", "authors", "year", "venue",
                                    "doi", "url", "abstract"])
    else:
        df.insert(0, "id", range(1, len(df) + 1))
        # Candidate.year is Optional[int]; any single None in the column forces
        # pandas to upcast the whole column to float64, so every year printed as
        # "2020.0" in every downstream sheet (candidates_dedup.csv, screening.csv,
        # extraction.csv). pandas' nullable Int64 holds missing values without
        # forcing floats -- <NA> for an unknown year, a plain integer otherwise.
        df["year"] = df["year"].astype("Int64")
    df.to_csv(out_path, index=False, encoding="utf-8")
    print(f"wrote {len(df)} candidate records to {out_path}")

    # Exit non-zero when EVERY source we actually tried failed. Previously this
    # exited 0 regardless, so a phase in which all sources died (e.g. the network
    # dropped) was reported to the orchestrator as a clean success, and "0 records
    # identified" was recorded in provenance as though the query legitimately
    # matched nothing. An empty result and a total failure are not the same claim.
    if attempted and len(failed) == len(attempted):
        print(f"ERROR: every source tried ({', '.join(failed)}) failed -- this is a "
               f"failure, not an empty result set", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
