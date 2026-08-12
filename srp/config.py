"""
config.py -- ReviewConfig: the run-wide settings for a systematic-review
pipeline run (topic, keywords, sources, thresholds), with JSON save/load.

SECRETS POLICY: the API-key fields below are held in memory for the session and
deliberately NEVER persisted. to_dict() -- which is what gets written to
runs/<id>/config.json and what feeds PROVENANCE.md -- drops them. The run
directory is the artifact users are told to keep, share with co-authors, and
attach as a reproducibility appendix, so it must not contain credentials.
The durable home for a key is .env (gitignored); hydrate_secrets_from_env()
reloads them on resume.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

# field name -> environment variable that supplies it. This is the single source
# of truth for "which fields are secret": to_dict() strips exactly these, and
# secret_env() forwards exactly these to child processes.
SECRET_ENV_VARS: dict[str, str] = {
    "s2_api_key": "S2_API_KEY",
    "pubmed_api_key": "PUBMED_API_KEY",
    "core_api_key": "CORE_API_KEY",
    "ieee_api_key": "IEEE_API_KEY",
    "scopus_api_key": "SCOPUS_API_KEY",
    "scopus_insttoken": "SCOPUS_INSTTOKEN",
    "springer_api_key": "SPRINGER_API_KEY",
    "wos_api_key": "WOS_API_KEY",
}
SECRET_FIELDS = frozenset(SECRET_ENV_VARS)


@dataclass
class ReviewConfig:
    topic: str = ""
    keywords: list[str] = field(default_factory=list)
    year_from: int = 2020
    year_to: int = 2026
    mailto: str = ""
    # The protocol's eligibility criteria, fixed BEFORE the search runs. These are
    # what screening applies -- without them the AI-assist prompt falls back to
    # "include if plausibly relevant", which is not a systematic review's
    # eligibility criterion and cannot be defended in a methods section.
    inclusion_criteria: str = ""
    exclusion_criteria: str = ""
    # PRISMA 2020 item 24a: report the registration name/number, or state explicitly
    # that the review was not registered. Recorded here so PROVENANCE.md can say so
    # either way rather than being silent.
    registration_id: str = ""
    # Every API key is optional. A keyed source with a blank key is skipped by
    # search.py rather than erroring; see scripts/search.py for the per-source
    # rate limits and what each key buys you.
    s2_api_key: str = ""        # raises the Semantic Scholar rate limit; S2 works without it
    pubmed_api_key: str = ""    # raises PubMed from 3 to 10 req/s; PubMed works without it
    core_api_key: str = ""      # enables CORE, both as a search source and for PDF download
    ieee_api_key: str = ""      # enables IEEE Xplore
    scopus_api_key: str = ""    # enables Scopus
    scopus_insttoken: str = ""  # Scopus off-campus institutional token (keys are IP-bound)
    springer_api_key: str = ""  # enables Springer Nature
    wos_api_key: str = ""       # enables Web of Science Expanded
    sources: list[str] = field(
        default_factory=lambda: ["openalex", "semanticscholar", "crossref", "arxiv",
                                  "pubmed", "doaj"]
    )
    max_per_source: int = 200
    n_phases: int = 1
    title_threshold: int = 92
    assist_tool_name: str = ""  # e.g. "ChatGPT (free web)" -- named in the AI-assistance disclosure
    reviewer: str = ""

    # Keyword BLOCKS: each inner list is OR'd together, blocks are AND'd across --
    # e.g. [["intrusion detection", "IDS"], ["machine learning", "deep learning"]]
    # means (intrusion detection OR IDS) AND (machine learning OR deep learning).
    # `keywords` (flat, all-AND) is kept for backward compatibility with existing
    # config.json files and the simple single-block case; search_query() prefers
    # keyword_blocks when present. Cramming every term into one AND-block (the
    # old-only behavior) collapses recall fast on anything past 2 terms -- see
    # search_query()'s docstring.
    keyword_blocks: list[list[str]] = field(default_factory=list)

    # --- Field-driven critical appraisal (see srp/appraisal.py) ---
    research_field: str = ""              # key into srp.appraisal.FIELD_PROFILES
    primary_study_instruments: list[str] = field(default_factory=list)
    certainty_framework: str = ""         # instrument key, or "" if none applies
    review_level_instrument: str = ""     # instrument key, or ""
    appraisal_justification: str = ""     # required prose when an instrument is absent

    # --- PRISMA 2020 administrative items (25 funding, 26 competing interests, 5 eligibility/language) ---
    funding_statement: str = ""
    competing_interests: str = ""
    language_restriction: str = ""

    # --- Reporting bias / grey literature (PRISMA 14, 21) ---
    grey_literature_included: bool = False
    grey_literature_justification: str = ""   # required when False -- excluding grey
                                                # literature is a publication-bias
                                                # amplifier and needs a stated reason
    reporting_bias_assessment: str = ""

    # --- core logic ---
    def to_dict(self, include_secrets: bool = False) -> dict:
        """Serialize. Secrets are dropped by DEFAULT -- every persistence and
        reporting path calls this, so the default must be the safe one. Pass
        include_secrets=True only for an in-memory round-trip."""
        d = asdict(self)
        if not include_secrets:
            for name in SECRET_FIELDS:
                d.pop(name, None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)

    def hydrate_secrets_from_env(self) -> list[str]:
        """Fill blank key fields from the environment (.env is loaded into it by
        slr.py). Since to_dict() never persists keys, a resumed run arrives with
        them blank and this is how they come back. Returns the field names filled,
        so the caller can tell the user what was picked up."""
        filled: list[str] = []
        for name, env_var in SECRET_ENV_VARS.items():
            if (getattr(self, name, "") or "").strip():
                continue
            value = (os.environ.get(env_var) or "").strip()
            if value:
                setattr(self, name, value)
                filled.append(name)
        return filled

    def secret_env(self) -> dict[str, str]:
        """The keys, shaped as the environment variables the stage scripts already
        read. Passing these through the child's environment instead of its argv
        keeps them out of the echoed command line and out of the process table."""
        return {env_var: (getattr(self, name, "") or "").strip()
                for name, env_var in SECRET_ENV_VARS.items()
                if (getattr(self, name, "") or "").strip()}

    def configured_key_names(self) -> list[str]:
        """Names -- never values -- of the keys that are set. For on-screen summaries."""
        return [name.replace("_api_key", "").replace("_", " ")
                for name in SECRET_ENV_VARS
                if (getattr(self, name, "") or "").strip()]

    def search_query(self) -> str:
        """Build the boolean query string sent to search.py.

        keyword_blocks, when present, produces (a OR b) AND (c OR d OR e) --
        the standard PICOC-block pattern (one block per concept, terms within a
        block are synonyms). The old flat `keywords` field ANDs every term
        together, which collapses recall fast past 2-3 terms: five keywords
        become a five-way AND, and a paper has to contain every single one of
        five near-synonyms to match. keyword_blocks is preferred when set;
        `keywords` remains for backward compatibility with existing configs and
        the genuinely single-concept case.
        """
        def _quote(term: str) -> str:
            return f'"{term}"' if " " in term else term

        if self.keyword_blocks:
            clauses = []
            for block in self.keyword_blocks:
                terms = [_quote(t) for t in block if t and t.strip()]
                if not terms:
                    continue
                clause = " OR ".join(terms)
                clauses.append(f"({clause})" if len(terms) > 1 else clause)
            if clauses:
                return " AND ".join(clauses)

        return " AND ".join(_quote(kw) for kw in self.keywords)

    def all_keywords(self) -> list[str]:
        """Every term the review is currently searching on, flattened, from
        whichever of keyword_blocks / keywords is actually in use. For display,
        provenance, and "don't re-suggest a term already in the query"."""
        if self.keyword_blocks:
            return [t for block in self.keyword_blocks for t in block]
        return list(self.keywords)

    def display_keywords(self) -> str:
        """Human-readable rendering of whichever keyword form is in use, for
        summaries and the provenance report. A single shared implementation --
        two ad hoc copies of this previously disagreed on how to handle an
        empty block, and one of them silently misaligned indices when a block
        was empty."""
        if self.keyword_blocks:
            parts = []
            for block in self.keyword_blocks:
                terms = [t for t in block if t and t.strip()]
                if not terms:
                    continue
                parts.append("(" + " OR ".join(terms) + ")" if len(terms) > 1 else terms[0])
            return " AND ".join(parts)
        return ", ".join(self.keywords)
