# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.3] - 2026-08-13

### Fixed

- **Critical, pip/pipx installs specifically**: every stage script
  (search.py, dedup.py, screen.py, download.py, and others) was launched
  with its working directory forced to wherever `slr.py` itself is
  installed, rather than the directory the user actually ran `slr` from.
  For a pip/pipx install that's deep inside site-packages, nowhere near the
  run folder -- so every `--out`/`--in` path (all relative, e.g.
  `runs/<id>/phase_1/candidates.csv`) resolved against the wrong directory:
  the script's real output silently landed inside the installed package
  instead of the run folder, while `state.json`/`config.json` (written
  in-process, not via a subprocess) correctly landed in the real run
  folder. The tool's own post-search re-read of `candidates.csv` (used to
  compute the hit count recorded in `state.json`) then ALSO looked in the
  wrong place and found nothing -- recording a false zero-hit result even
  when the search itself had actually succeeded. This is almost certainly
  the real explanation for early reports of a run coming back with 0 hits
  and an empty phase folder despite `state.json` claiming success -- not a
  network issue. Unaffected for a git-clone checkout run as `python
  slr.py` from the repo root, since the install directory and the run
  directory happened to coincide there.

## [1.2.2] - 2026-08-13

### Fixed

- "Re-run a phase's search" stopped after search/dedup/prescreen, clearing
  the phase's AI-assist/review-gate markers but never actually continuing
  into them -- leaving a freshly re-populated screening.csv with nothing in
  the consolidation menu able to screen it. It now continues through
  AI-assist TA screening and the human review gate too, the same sequence a
  normal phase run goes through, since the consolidation menu has no other
  action that reaches those steps.

## [1.2.1] - 2026-08-13

### Fixed

- All "run this to update" guidance (the startup nag, the on-demand "Check
  for updates" action, and the README) now says
  `pipx upgrade --force systematic-review-pipeline`. Installing from
  `git+https://...` rebuilds the wheel from source on every upgrade, and the
  Windows launcher `pipx upgrade` generates for `slr.exe` comes out
  byte-different almost every time even when nothing meaningful changed --
  pipx's own shim-safety-check (protecting a shim it doesn't recognize as
  its own) then refuses to touch the PATH launcher, silently leaving the old
  version running. `--force` tells pipx to overwrite it as part of the same
  upgrade, instead of requiring a separate `pipx reinstall`.

## [1.2.0] - 2026-08-13

### Added

- "Re-run a phase's search (with or without changes)" consolidation-menu
  action -- the fix half of "Diagnose this run": re-runs search + dedup +
  prescreen for one phase, either with the current settings or after
  editing mailto/year range/max-per-source/sources first, without
  abandoning the run and starting over. Warns and requires confirmation
  before running, since it overwrites that phase's candidates/dedup/
  screening files and clears any now-stale downstream screening decisions.

### Fixed

- Every table this tool renders (the consolidation-menu legend, the
  diagnose-run funnel, the API-key list) drew no line between rows, making
  adjacent rows hard to tell apart at a glance -- added row separators.

## [1.1.0] - 2026-08-13

### Fixed

- `slr.py` never called `load_dotenv()` -- `.env`-based API key auto-detection
  was silently non-functional for the entire guided TUI, on both a git-clone
  checkout and a `pipx` install, contradicting `.env.example`'s own
  documented claim that it loads automatically.
- `.env`'s default path resolved relative to wherever `srp/env.py` itself was
  installed, rather than the current working directory -- correct by
  accident for a git clone (whose documented workflow already puts you at
  the repo root), but broken for a `pipx` install, where that path landed
  inside `pipx`'s internal venv, nowhere a user would ever find or edit it.
  It now resolves against the current working directory, matching wherever
  `slr` was actually run from.
- The setup wizard's inclusion/exclusion-criteria prompts, and the API-key
  prompt's "[detected in .env]" message, embedded a multi-line example
  directly in the prompt text with no trailing newline, so questionary
  rendered the input cursor in the wrong place on screen.
- Two checkbox prompts (critical-appraisal instrument selection, MMAT design
  selection) gave no indication that Space toggles a choice and Enter
  confirms -- the other checkboxes in the wizard already said so.

### Added

- The startup banner now always shows the running version, and the update
  check leaves a visible trace either way -- a dim confirmation when up to
  date, a nag only when a real newer release exists -- instead of staying
  silent whenever there was nothing to complain about.
- "Diagnose this run" consolidation-menu action: a per-phase funnel of
  search/dedup/prescreen/review-gate counts, flagging the first stage that
  returned zero and any stage marked "done" whose output file isn't actually
  on disk -- the two symptoms a review that came back empty for no visible
  reason actually presents as.
- "Manage API keys" consolidation-menu action: shows the `.env` path
  actually in use, and adds/updates or deletes individual keys (or all of
  them) without hand-editing the file or knowing where `pipx` put anything.
- Every consolidation-menu action now has a one-line description, shown
  once before the menu prompt, instead of a bare list of labels.
- The setup wizard's worked examples (topic, keyword blocks, inclusion/
  exclusion criteria) now use a generic, clearly-labeled illustrative
  example (pair programming vs. code quality) instead of this project's own
  ML-IDS research topic, so they read as an example rather than a hint about
  what to search for.

## [1.0.1] - 2026-08-13

### Fixed

- `pyproject.toml` declared no `license` metadata, even though the package
  is now really `pip`/`pipx`-installable -- added (`MIT`, with `LICENSE` and
  `LICENSE-docs` both bundled into the wheel).

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
