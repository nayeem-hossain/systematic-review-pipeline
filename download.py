"""
download.py -- resolve and fetch open-access PDFs for deduped candidates.

Sources tried, in order, per record:
  1. arXiv direct pattern: arxiv.org/abs/<id> -> arxiv.org/pdf/<id>.pdf
     (works for any record whose url or doi contains an arXiv id -- no
     network lookup needed, so this path never fails to *resolve*, only to
     download).
  2. Unpaywall (any record with a DOI) -- REQUIRES a real contact email on
     every request; no key needed otherwise.
     https://unpaywall.org/products/api

Usage:
    python download.py --in output/candidates_dedup.csv --mailto you@example.com \
        --outdir pdfs

This is a best-effort OA resolver, not a paywall bypass: records with no open-
access copy are logged as such and skipped, not force-downloaded.

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
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

POLITE_DELAY_SECS = 1.5


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


def main():
    ap = argparse.ArgumentParser(
        description="Resolve and download open-access PDFs for deduped candidates "
                     "via arXiv direct links and Unpaywall.")
    ap.add_argument("--in", dest="inp", default="output/candidates_dedup.csv")
    ap.add_argument("--mailto", default=os.environ.get("MAILTO"),
                     help="Real contact email -- required by Unpaywall; "
                          "falls back to the MAILTO environment variable")
    ap.add_argument("--outdir", default="pdfs")
    ap.add_argument("--max-downloads", type=int, default=None,
                     help="Stop after this many successful downloads (default: no limit)")
    args = ap.parse_args()

    if not args.mailto:
        ap.error("--mailto is required (or set MAILTO in the environment / .env) -- "
                  "Unpaywall requires a real contact email on every request")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(args.inp, encoding="utf-8")
    if "duplicate_of" in df.columns:
        df = df[df["duplicate_of"].isna()]  # fetch canonical (non-duplicate) records only

    log_rows = []
    n_ok = 0
    for _, row in df.iterrows():
        if args.max_downloads is not None and n_ok >= args.max_downloads:
            break
        rid = row["id"]
        doi = str(row.get("doi", "") or "")
        url = str(row.get("url", "") or "")

        pdf_url, method = None, None
        u = arxiv_pdf_url(url or doi)
        if u:
            pdf_url, method = u, "arxiv"
        elif doi and doi.lower() != "nan":
            pdf_url, method = unpaywall_pdf_url(doi, args.mailto), "unpaywall"

        if not pdf_url:
            log_rows.append({"id": rid, "doi": doi, "oa_status": "no_oa_url_found",
                              "method": method or "", "saved_path": ""})
            continue

        dest = outdir / f"{rid}.pdf"
        ok, msg = download(pdf_url, dest, args.mailto)
        log_rows.append({"id": rid, "doi": doi,
                          "oa_status": "downloaded" if ok else f"failed: {msg}",
                          "method": method, "saved_path": str(dest) if ok else ""})
        if ok:
            n_ok += 1
        time.sleep(POLITE_DELAY_SECS)  # be polite to publisher/arXiv servers

    log = pd.DataFrame(log_rows, columns=["id", "doi", "oa_status", "method", "saved_path"])
    log_path = Path("output/download_log.csv")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.to_csv(log_path, index=False, encoding="utf-8")
    print(f"{n_ok}/{len(log)} PDFs downloaded to {outdir}/; see {log_path} for the full log")
    print("Reminder: verify every downloaded PDF's title page against its expected "
          "title before it enters extraction (see README integrity guardrails).")


if __name__ == "__main__":
    main()
