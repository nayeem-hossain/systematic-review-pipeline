"""
normalize.py -- the single definition of "the same DOI" and "the same title".

Two modules used to keep private copies of this logic, and they disagreed:

  - dedup.py decides which records are silently merged away as duplicates.
  - state.py builds the record_key that decides which records count as
    "already judged" across phases, and which included studies are the same
    study when phases are merged.

state.py's copy even carried the comment "normalization must match the repo's
dedup.py". It did not. dedup.py stripped only `https://doi.org/` and
`http://dx.doi.org/`, while state.py also stripped a bare `doi.org/`, so the two
returned different answers for the same DOI. When these disagree, a record can be
deduplicated under one identity and cached under another -- the kind of drift
that produces a wrong number in a published table with nothing in the logs to
explain it.

One definition, imported by both.
"""
from __future__ import annotations

import re

# Matches every DOI prefix these APIs actually emit:
#   https://doi.org/10.x, http://dx.doi.org/10.x, www.doi.org/10.x, doi.org/10.x,
#   doi:10.x, "DOI: 10.x"
_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://)?(?:dx\.|www\.)?doi\.org/"
    r"|^doi\s*:\s*",
    re.IGNORECASE,
)


def normalize_doi(doi) -> str:
    """Reduce any DOI form to a bare, lowercased DOI. Non-values become ""."""
    if doi is None:
        return ""
    s = str(doi).strip()
    if not s or s.lower() == "nan":
        return ""
    return _DOI_PREFIX_RE.sub("", s.lower()).strip()


def normalize_title(title) -> str:
    """Lowercase, strip punctuation, collapse whitespace -- for matching only,
    never for display. Non-values become ""."""
    if title is None:
        return ""
    s = str(title)
    if not s.strip() or s.strip().lower() == "nan":
        return ""
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def record_key(doi, title) -> str:
    """Stable cross-phase identity for a record: DOI if there is one, else title.

    Deliberately NOT the `id` column -- ids are positional and search.py renumbers
    them on every run, so they are not stable across phases or re-runs.
    """
    k = normalize_doi(doi)
    if k:
        return "doi:" + k
    t = normalize_title(title)
    if t:
        return "title:" + t
    return ""
