"""
export.py -- BibTeX (.bib) and RIS (.ris) formatting for the systematic-review
included-studies set: record dicts in, reference-manager-ready text out.
"""
from __future__ import annotations

import re

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"

_TITLE_STOPWORDS = {"a", "an", "the", "on", "of", "for", "and", "in", "to"}


def _split_authors(authors) -> list[str]:
    """Split a free-form authors string into individual author names.
    Separator priority: ';' first (safe alongside "Last, First" commas inside
    each name), then ' and ', then ',' -- a lone "Last, First" author with no
    other separator is ambiguous with a two-author comma list and, per the
    ',' fallback, is treated as two names; use ';' to disambiguate that case."""
    s = str(authors or "").strip()
    if not s:
        return []
    if ";" in s:
        parts = s.split(";")
    elif re.search(r"\band\b", s, flags=re.IGNORECASE):
        parts = re.split(r"\s+and\s+", s, flags=re.IGNORECASE)
    elif "," in s:
        parts = s.split(",")
    else:
        parts = [s]
    return [p.strip() for p in parts if p.strip()]


def _last_name(name: str) -> str:
    name = name.strip()
    if "," in name:
        return name.split(",", 1)[0].strip()
    tokens = name.split()
    return tokens[-1] if tokens else ""


def _slug(s: str) -> str:
    """Lowercase, ascii-alnum only -- drops everything else (spaces, punctuation,
    non-ascii)."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _year_digits(year) -> str:
    """4-digit year string if one can be found in `year`, else ''."""
    if year is None:
        return ""
    if isinstance(year, float):
        if year != year:  # NaN
            return ""
        year = int(year)
    s = str(year).strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})", s) or re.search(r"\d{4}", s)
    return m.group(0) if m else ""


def _first_title_word(title: str) -> str:
    """First alnum word >2 chars, skipping common stopwords; '' if none found."""
    if not title:
        return ""
    for w in re.findall(r"[A-Za-z0-9]+", title):
        if len(w) > 2 and w.lower() not in _TITLE_STOPWORDS:
            return w.lower()
    return ""


def _suffix_for(i: int) -> str:
    """1 -> 'b', 2 -> 'c', ..., 25 -> 'z', 26 -> 'aa', 27 -> 'ab', ... (bijective
    base-26, shifted by one so it never collides with the unsuffixed key)."""
    n = i + 1
    chars = []
    while n > 0:
        n, r = divmod(n - 1, 26)
        chars.append(_ALPHABET[r])
    return "".join(reversed(chars))


def _escape_bibtex(value) -> str:
    s = str(value)
    s = s.replace("&", r"\&")
    s = s.replace("%", r"\%")
    s = s.replace("_", r"\_")
    s = s.replace("#", r"\#")
    return s


# --- core logic ---
def bibtex_key(record: dict, existing: set[str]) -> str:
    authors = _split_authors(record.get("authors"))
    last = _last_name(authors[0]) if authors else ""
    last_slug = _slug(last) or "anon"

    year_digits = _year_digits(record.get("year"))
    year_slug = year_digits if year_digits else "nd"

    word = _first_title_word(str(record.get("title") or ""))
    word_slug = _slug(word) or "study"

    base = f"{last_slug}{year_slug}{word_slug}"

    if base not in existing:
        existing.add(base)
        return base
    i = 1
    while True:
        candidate = base + _suffix_for(i)
        if candidate not in existing:
            existing.add(candidate)
            return candidate
        i += 1


def to_bibtex(records: list[dict]) -> str:
    existing: set[str] = set()
    entries = []
    for record in records:
        key = bibtex_key(record, existing)

        fields = []

        title = str(record.get("title") or "").strip()
        if title:
            fields.append("  title = {{" + _escape_bibtex(title) + "}}")

        authors = _split_authors(record.get("authors"))
        if authors:
            fields.append("  author = {" + _escape_bibtex(" and ".join(authors)) + "}")

        year = _year_digits(record.get("year"))
        if year:
            fields.append("  year = {" + year + "}")

        venue = str(record.get("venue") or "").strip()
        if venue:
            fields.append("  journal = {" + _escape_bibtex(venue) + "}")

        doi = str(record.get("doi") or "").strip()
        if doi:
            fields.append("  doi = {" + _escape_bibtex(doi) + "}")

        rid = record.get("id")
        if rid is not None and str(rid).strip() != "":
            fields.append("  note = {id: " + _escape_bibtex(str(rid)) + "}")

        entries.append("@article{" + key + ",\n" + ",\n".join(fields) + "\n}")

    return "\n\n".join(entries)


def to_ris(records: list[dict]) -> str:
    blocks = []
    for record in records:
        lines = ["TY  - JOUR"]

        for author in _split_authors(record.get("authors")):
            lines.append("AU  - " + author)

        title = str(record.get("title") or "").strip()
        if title:
            lines.append("TI  - " + title)

        year = _year_digits(record.get("year"))
        if year:
            lines.append("PY  - " + year)

        venue = str(record.get("venue") or "").strip()
        if venue:
            lines.append("JO  - " + venue)

        doi = str(record.get("doi") or "").strip()
        if doi:
            lines.append("DO  - " + doi)

        rid = record.get("id")
        if rid is not None and str(rid).strip() != "":
            lines.append("ID  - " + str(rid))

        lines.append("ER  - ")
        blocks.append("\n".join(lines))

    return ("\n\n".join(blocks) + "\n") if blocks else ""
