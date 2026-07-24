"""
methods_report.py -- auto-draft the "Search methods" text a manuscript needs,
directly from the pipeline's own recorded search strategy and PRISMA counts.

Why this exists: a user asked "how will I quote these counts, and can the tool
do it for me?" PRISMA items 6/7 require the exact per-database query, the date
each search was run, and any limits/filters used. search.py's search_strategy.csv
already records all of that per source (see scripts/search.py's SourceReport),
and slr.py's _compute_prisma_counts already computes the cross-phase totals --
but a user still had to hand-assemble a methods paragraph from raw CSVs and
remember, unprompted, which counts were truncated. This module turns the two
into one paste-ready paragraph plus a per-source supplementary table, so the
numbers a manuscript quotes are drawn from the pipeline's own record rather than
retyped by hand, and carry the truncation caveat automatically rather than being
quoted as if they were a complete database result.

The output is a DRAFT. It states facts the pipeline actually recorded; it is not
a substitute for the reviewer reading it before submission.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SourceStrategyRow:
    source: str
    query_sent: str = ""
    retrieved_at: str = ""
    n_retrieved: int = 0
    total_available: "int | None" = None
    truncated: bool = False
    status: str = "ok"


@dataclass
class PhaseSearchRecord:
    phase: int
    label: str  # "Phase 1" or "Phase 2 (query expansion)"
    rows: list = field(default_factory=list)  # list[SourceStrategyRow]


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_str(v, default: str = "n/a") -> str:
    """NaN-safe string display -- a search_strategy.csv round-trip through pandas
    can turn an empty string into a float NaN, which str() renders as "nan"."""
    if v is None:
        return default
    if isinstance(v, float) and v != v:
        return default
    s = str(v).strip()
    return default if (not s or s.lower() == "nan") else s


def _fmt_query(config: dict) -> str:
    """The review's base query, formatted for prose.

    Reuses ReviewConfig.search_query() rather than re-deriving it from
    config["keywords"] -- the wizard now builds keyword_blocks (OR-within,
    AND-across) and deliberately leaves the flat `keywords` field empty, and
    search_query() is the one place that already knows the right precedence
    between the two. A second implementation here previously read only the
    flat field, so every guided-wizard review (which populates keyword_blocks,
    not keywords) got a drafted methods paragraph reading "using the base query
    (no keywords recorded)" -- silently wrong, and exactly the sentence a user
    pastes into a manuscript without rechecking.
    """
    from srp.config import ReviewConfig  # local import: keep this module's only
                                          # srp dependency scoped to where it's used
    query = ReviewConfig.from_dict(config).search_query()
    return query if query.strip() else "(no keywords recorded)"


# --- core logic ---
def render_search_strategy_table(phases: list) -> str:
    """Markdown table, one row per (phase, source) -- the supplementary-materials
    table PRISMA item 7 asks for: the exact query sent to each database."""
    lines = ["| Phase | Source | Query sent | Retrieved at (UTC) | Retrieved | "
             "Total available | Status |",
             "|---|---|---|---|---|---|---|"]
    any_rows = False
    for ph in phases:
        for r in ph.rows:
            any_rows = True
            query = _fmt_str(r.query_sent).replace("|", "\\|").replace("\n", " ")
            avail = _fmt_int(r.total_available) if r.total_available is not None else "n/a"
            status = r.status if r.status not in ("ok", "", None) else (
                "truncated" if r.truncated else "complete")
            lines.append(f"| {ph.label} | {r.source} | `{query}` | {_fmt_str(r.retrieved_at)} | "
                         f"{_fmt_int(r.n_retrieved)} | {avail} | {status} |")
    if not any_rows:
        lines.append("| _(no search-strategy log found for any phase)_ | | | | | | |")
    return "\n".join(lines)


def render_search_methods(cfg: dict, phases: list, prisma: dict) -> str:
    """A paste-ready prose paragraph for a manuscript's Methods/Search section.

    Every sentence is conditioned on what the pipeline actually recorded --
    it says nothing about a source it has no row for, and it only raises the
    truncation caveat for sources that were actually truncated.
    """
    year_from, year_to = cfg.get("year_from"), cfg.get("year_to")
    date_range = (f"published between {year_from} and {year_to} " if year_from and year_to else "")

    all_rows = [r for ph in phases for r in ph.rows]
    sources_attempted = sorted({r.source for r in all_rows if r.status != "skipped_no_key"})
    skipped = sorted({r.source for r in all_rows if r.status == "skipped_no_key"})
    truncated_sources = sorted({r.source for r in all_rows if r.truncated})

    parts: list[str] = []

    query_str = _fmt_query(cfg)
    if sources_attempted:
        parts.append(
            f"We searched {', '.join(sources_attempted)} for records {date_range}"
            f"using the base query {query_str}. The exact query string sent to each "
            f"database, and the date it was run, are recorded verbatim in the "
            f"search-strategy log (Table S1); several sources require the query in a "
            f"different syntax than the one shown here, and Table S1 reports what was "
            f"actually sent, not the base string."
        )
    else:
        parts.append("No search-strategy log was found for this run -- this paragraph "
                      "could not be drafted from recorded data.")

    if skipped:
        verb = "was" if len(skipped) == 1 else "were"
        parts.append(f"{', '.join(skipped)} {verb} not queried in this review "
                      f"(no API access available for {'it' if len(skipped) == 1 else 'them'}).")

    if truncated_sources:
        parts.append(
            f"Results from {', '.join(truncated_sources)} were capped by a per-source "
            f"retrieval limit and therefore represent a relevance-ranked sample rather "
            f"than each source's complete result set for this query. Table S1 reports, "
            f"for each truncated source, both the number of records retrieved and the "
            f"total the source reported as matching the query; readers should not treat "
            f"the retrieved count as the database's full yield for these sources."
        )

    n_phases = len(phases)
    if n_phases > 1:
        parts.append(
            f"The search was conducted across {n_phases} phases: an initial search, "
            f"followed by {n_phases - 1} additional round(s) using a query expanded with "
            f"terms drawn from the titles of studies included at the previous phase "
            f"(query expansion by term frequency, not citation snowballing)."
        )

    undecided = prisma.get("undecided_ta") or 0
    parts.append(
        f"In total, {_fmt_int(prisma.get('identified'))} records were identified. After "
        f"removing {_fmt_int(prisma.get('duplicates_removed'))} duplicates, "
        f"{_fmt_int(prisma.get('screened'))} records were screened at title/abstract "
        f"level, of which {_fmt_int(prisma.get('excluded_ta'))} were excluded. "
        f"{_fmt_int(prisma.get('assessed_ft'))} record(s) proceeded to full-text "
        f"assessment"
        + (f" ({_fmt_int(undecided)} still awaiting a title/abstract decision at the "
           f"time this paragraph was drafted)" if undecided else "")
        + f", of which {_fmt_int(prisma.get('excluded_ft'))} were excluded at full text "
        f"(see Table S2 for exclusion reasons) and {_fmt_int(prisma.get('included'))} "
        f"were included in the review."
    )

    return "\n\n".join(parts)
