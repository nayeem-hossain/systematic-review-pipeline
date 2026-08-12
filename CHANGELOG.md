# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-08-13

First tagged release. No earlier version was ever tagged or distributed, so
prior development is summarized thematically below rather than reconstructed
commit-by-commit.

### Added

- Core pipeline: search -> dedup -> screen -> download -> verify-citations ->
  extract, plus the PRISMA 2020 flow diagram and quality-/venue-tier figures.
- Search sources: OpenAlex, Semantic Scholar, Crossref, arXiv, and Unpaywall
  (no key required); IEEE, Scopus (with institutional-token support for
  off-campus use), Springer Nature, PubMed, DOAJ, and CORE (each optionally
  keyed, with rate limits sourced from each provider's own documentation);
  Web of Science Expanded API as a further optional source.
- Guided interactive TUI (`slr.py`) with review-gated, multi-phase snowball
  search, resumable via saved per-stage checkpoints.
- Paste-and-parse AI-assist screening (`scripts/assist.py`) -- no API key
  required, works with any web chatbot -- with per-batch progress reporting
  (batch N of M, records remaining) in both the CLI and the guided loop.
- An always-on provenance system: an append-only run manifest auto-rendered
  into a human-readable `PROVENANCE.md`.
- BibTeX/RIS reference export.
- A field-driven critical-appraisal instrument registry (Cochrane RoB 2,
  ROBINS-I, QUADAS-2, Dyba & Dingsoyr, CASP, JBI, MMAT, AMSTAR 2, ROBIS,
  GRADE/GRADE-CERQual, and others), plus a fillable review-level
  self-appraisal checklist generator.
- Real backward/forward citation snowballing (Wohlin 2014) and auto-drafted
  search-methods paragraphs generated from the pipeline's own recorded
  search strategy.
- Full-text screening workflow and inter-rater agreement (Cohen's kappa).
- A guided extraction-record editor (venue tier, R/A/T/C quality rubric,
  quality tier, and every other extraction field) with an auto-computed
  quality-tier formula: a hard gate on Rigor/Threat-model-completeness plus
  graded scoring on Artifact-availability/Currency, so two reviewers scoring
  the same four ratings always land on the same tier.
- A shared multi-phase PRISMA-count module (`srp/prisma.py`), used by both
  the guided TUI and a new `figures.py --run-dir` standalone mode.
- `pipx`/`pip`-installable packaging (`slr` console command via
  `[project.scripts]`, dependencies resolved automatically) alongside the
  existing git-clone workflow, which remains fully supported.
- A non-blocking update check: a one-line startup notice when a newer
  release exists, plus an on-demand "Check for updates" consolidation-menu
  action that always reports a result. Never auto-updates or blocks.

### Fixed

- A corrupt/undecodable CSV could crash the guided TUI with a raw traceback
  instead of a clean, actionable message.
- AI-assist screening silently fell back to an unoperationalized "plausibly
  relevant" judgement when eligibility criteria were blank; it now refuses
  to run that way unless explicitly overridden.
- Full-text screening could receive an invalid "maybe" (PRISMA's full-text
  stage is terminal, so a stray maybe was previously dropped silently from
  the flow-diagram counts instead of being refused).
- The AI-assist reply parser could be defeated by markdown decorations a
  chatbot added despite being told not to (bullets, bold, table rows), and
  could corrupt phase/snowball-namespaced ids.
- The review gate reported only an aggregate flip count after writing,
  rather than previewing which specific record would flip which way first.
- `figures.py` run standalone silently under-counted PRISMA totals -- and
  read full-text decisions from the wrong file entirely -- on any review
  with more than one search phase.
- Dedup kept whichever duplicate record was seen first, even when a
  different duplicate of the same study actually carried a usable abstract.
- The review gate and full-text screening could be marked "done" while
  records remained undecided, letting a phase silently advance with
  unaccounted-for records still outstanding.
- Scopus's 401/403 handling confidently blamed "COMPLETE-view entitlement"
  on every refusal, even when the real cause was an off-campus request with
  no `SCOPUS_INSTTOKEN` set.

### Changed

- Normalization and decision-ingestion logic (id/DOI/title normalization,
  Cohen's kappa, `.env` loading, AI-decision ingestion) consolidated into
  shared `srp/` modules, replacing several previously-duplicated
  implementations that had drifted from each other.
