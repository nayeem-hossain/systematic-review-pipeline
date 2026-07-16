"""
download.py -- resolve and fetch open-access PDFs for deduped candidates.

Sources tried, in order, per record (override the order/set with --sources):
  1. arXiv direct pattern: arxiv.org/abs/<id> -> arxiv.org/pdf/<id>.pdf
     (works for any record whose url or doi contains an arXiv id -- no
     network lookup needed, so this path never fails to *resolve*, only to
     download).
  2. Unpaywall (any record with a DOI) -- REQUIRES a real contact email on
     every request; no key needed otherwise.
     https://unpaywall.org/products/api
  3. OpenAlex (any record with a DOI) -- best_oa_location / primary_location
     pdf_url. No key needed; a contact email (--mailto) is sent for the
     polite pool. https://docs.openalex.org/
  4. Semantic Scholar (any record with a DOI) -- openAccessPdf.url. No key
     needed; an optional S2_API_KEY environment variable raises the rate
     limit. https://api.semanticscholar.org/
  5. CORE (any record, DOI preferred, falls back to title search) -- only
     tried if a CORE API key is supplied (--core-api-key or CORE_API_KEY).
     https://core.ac.uk/services/api

Usage:
    python download.py --in output/candidates_dedup.csv --mailto you@example.com \
        --outdir pdfs

This is a best-effort OA resolver, not a paywall bypass: records with no open-
access copy are logged as such and skipped, not force-downloaded. Every record
that ends without a saved PDF (no OA URL found, or the download itself failed)
is also written to --report (default output/manual_download_needed.csv) for
manual follow-up.

Do not skip the manual verification step after this script runs. Automated
resolution can fetch the wrong PDF (e.g., a similarly-titled paper, or a
publisher landing page mislabeled as a PDF) often enough to matter. Check every
downloaded PDF's title page against the expected title before it enters
extraction -- see the README's integrity guardrails section.
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
import re
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

POLITE_DELAY_SECS = 1.5
KNOWN_SOURCES = ["arxiv", "unpaywall", "openalex", "semanticscholar", "core"]


# --- API internals: leave unless the API changes ---
def unpaywall_pdf_url(doi: str, email: str) -> Optional[str]:
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email}, timeout=20)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    best = r.json().get("best_oa_location") or {}
    return best.get("url_for_pdf") or best.get("url")


# --- API internals: leave unless the API changes ---
def arxiv_pdf_url(url_or_doi: str) -> Optional[str]:
    if not isinstance(url_or_doi, str):
        return None
    m = re.search(r"arxiv\.org/abs/([\w.\-]+)", url_or_doi) or re.search(r"arXiv\.([\w.\-]+)", url_or_doi)
    return f"https://arxiv.org/pdf/{m.group(1)}.pdf" if m else None


# --- API internals: leave unless the API changes ---
def openalex_oa_pdf_url(doi: str, mailto: str) -> Optional[str]:
    try:
        r = requests.get(
            f"https://api.openalex.org/works/https://doi.org/{doi}",
            params={"mailto": mailto},
            timeout=20,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    loc = data.get("best_oa_location") or data.get("primary_location") or {}
    return loc.get("pdf_url")


# --- API internals: leave unless the API changes ---
def semanticscholar_oa_pdf_url(doi: str, api_key: Optional[str]) -> Optional[str]:
    try:
        headers = {"x-api-key": api_key} if api_key else {}
        r = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
            params={"fields": "openAccessPdf"},
            headers=headers,
            timeout=20,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return (data.get("openAccessPdf") or {}).get("url")


# --- API internals: leave unless the API changes ---
def core_pdf_url(doi: str, title: str, api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        q = f'doi:"{doi}"' if doi else f'title:"{title}"'
        r = requests.post(
            "https://api.core.ac.uk/v3/search/works",
            headers=headers,
            json={"q": q, "limit": 1},
            timeout=20,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        results = r.json().get("results") or []
    except ValueError:
        return None
    if not results:
        return None
    first = results[0]
    return first.get("downloadUrl") or first.get("fullTextIdentifier")


def download(url: str, dest: Path, mailto: str) -> tuple[bool, str]:
    try:
        headers = {"User-Agent": f"systematic-review-pipeline/1.0 (mailto:{mailto})"}
        r = requests.get(url, headers=headers, timeout=60, stream=True)
        content_type = r.headers.get("Content-Type", "").lower()
        if r.status_code != 200 or "pdf" not in content_type:
            return False, f"http {r.status_code}, content-type {content_type or 'unknown'}"
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return True, "ok"
    except requests.RequestException as e:
        return False, str(e)


def resolve_source_order(raw: str) -> list:
    """Parse --sources into a filtered, ordered list of known source names."""
    sources = []
    for name in raw.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name not in KNOWN_SOURCES:
            print(f"warning: unknown OA source '{name}' in --sources, ignoring", file=sys.stderr)
            continue
        sources.append(name)
    return sources


def main():
    ap = argparse.ArgumentParser(
        description="Resolve and download open-access PDFs for deduped candidates "
                     "via arXiv direct links, Unpaywall, OpenAlex, Semantic Scholar, and CORE.")
    ap.add_argument("--in", dest="inp", default="output/candidates_dedup.csv")
    ap.add_argument("--mailto", default=os.environ.get("MAILTO"),
                     help="Real contact email -- required by Unpaywall; "
                          "falls back to the MAILTO environment variable")
    ap.add_argument("--outdir", default="pdfs")
    ap.add_argument("--max-downloads", type=int, default=None,
                     help="Stop after this many successful downloads (default: no limit)")
    ap.add_argument("--core-api-key", default=os.environ.get("CORE_API_KEY"),
                     help="Optional CORE API key (https://core.ac.uk/services/api) to try "
                          "CORE as an extra OA source; falls back to CORE_API_KEY env var")
    ap.add_argument("--report", default="output/manual_download_needed.csv",
                     help="Path for the CSV listing records with no OA PDF found "
                          "(for manual retrieval)")
    ap.add_argument("--sources", default="arxiv,unpaywall,openalex,semanticscholar,core",
                     help="Comma-separated OA sources to try, in order")
    args = ap.parse_args()

    if not args.mailto:
        ap.error("--mailto is required (or set MAILTO in the environment / .env) -- "
                  "Unpaywall requires a real contact email on every request")

    sources = resolve_source_order(args.sources)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(args.inp, encoding="utf-8")
    if "duplicate_of" in df.columns:
        df = df[df["duplicate_of"].isna()]  # fetch canonical (non-duplicate) records only

    log_rows = []
    manual_rows = []
    n_ok = 0
    # --- core logic: the per-record OA resolution + download loop ---
    for _, row in df.iterrows():
        if args.max_downloads is not None and n_ok >= args.max_downloads:
            break
        rid = row["id"]
        doi = str(row.get("doi", "") or "")
        url = str(row.get("url", "") or "")
        title = str(row.get("title", "") or "")
        has_doi = bool(doi) and doi.lower() != "nan"

        pdf_url, method, tried = None, None, []
        for src in sources:
            u = None
            if src == "arxiv":
                tried.append("arxiv")
                u = arxiv_pdf_url(url or doi)
            elif src == "unpaywall":
                if not has_doi:
                    continue
                tried.append("unpaywall")
                u = unpaywall_pdf_url(doi, args.mailto)
            elif src == "openalex":
                if not has_doi:
                    continue
                tried.append("openalex")
                u = openalex_oa_pdf_url(doi, args.mailto)
            elif src == "semanticscholar":
                if not has_doi:
                    continue
                tried.append("semanticscholar")
                u = semanticscholar_oa_pdf_url(doi, os.environ.get("S2_API_KEY"))
            elif src == "core":
                if not args.core_api_key:
                    continue
                tried.append("core")
                u = core_pdf_url(doi, title, args.core_api_key)
            if u:
                pdf_url, method = u, src
                break

        if not pdf_url:
            reason = "no_oa_url_found"
            log_rows.append({"id": rid, "doi": doi, "oa_status": reason,
                              "method": method or "", "saved_path": ""})
            manual_rows.append({"id": rid, "doi": doi, "title": title, "url": url,
                                 "tried": "|".join(tried), "reason": reason})
            continue

        dest = outdir / f"{rid}.pdf"
        ok, msg = download(pdf_url, dest, args.mailto)
        oa_status = "downloaded" if ok else f"failed: {msg}"
        log_rows.append({"id": rid, "doi": doi, "oa_status": oa_status,
                          "method": method, "saved_path": str(dest) if ok else ""})
        if ok:
            n_ok += 1
        else:
            manual_rows.append({"id": rid, "doi": doi, "title": title, "url": url,
                                 "tried": "|".join(tried), "reason": oa_status})
        time.sleep(POLITE_DELAY_SECS)  # be polite to publisher/arXiv servers

    log = pd.DataFrame(log_rows, columns=["id", "doi", "oa_status", "method", "saved_path"])
    log_path = Path("output/download_log.csv")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.to_csv(log_path, index=False, encoding="utf-8")

    manual = pd.DataFrame(manual_rows, columns=["id", "doi", "title", "url", "tried", "reason"])
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    manual.to_csv(report_path, index=False, encoding="utf-8")

    print(f"{n_ok}/{len(log)} PDFs downloaded to {outdir}/; see {log_path} for the full log")
    print(f"{n_ok} downloaded, {len(manual_rows)} need manual retrieval -> {report_path}")
    print("Reminder: verify every downloaded PDF's title page against its expected "
          "title before it enters extraction (see README integrity guardrails).")


if __name__ == "__main__":
    main()
