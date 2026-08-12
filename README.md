# systematic-review-pipeline

A reusable, dependency-light toolkit for running a systematic literature review
(SLR) end to end: protocol scoping, multi-source search, deduplication,
screening, open-access PDF retrieval, and deterministic citation verification.
It pairs a documented 7-stage methodology (PRISMA 2020 + Kitchenham & Charters
2007) with a set of small, standalone Python scripts that implement the
mechanical parts of that methodology -- so a review stays reproducible instead
of living in a pile of manually-edited spreadsheets.

The worked example used throughout this README is **machine-learning / AI-based
intrusion detection systems (ML-IDS)** -- network intrusion detection, anomaly
detection, and related applications of deep learning, ensembles, transformers,
GANs, and federated learning to network security. Swap the search terms, the
PICOC table, and the inclusion/exclusion criteria for your own topic; the
pipeline and the method don't change.

## Installing

**Quickest: `pipx install` directly from GitHub** -- no `git clone` needed,
works identically on Windows/macOS/Linux, and pulls in every dependency
(pandas, matplotlib, etc. -- the full `dependencies` list in `pyproject.toml`)
automatically:

```bash
pipx install git+https://github.com/nayeem-hossain/systematic-review-pipeline.git
slr
```

(`pipx` isolates the install in its own environment so it can't collide with
another project's packages; install it once with
`python -m pip install --user pipx` if you don't have it yet. Plain
`pip install git+...` also works if you'd rather not use pipx.)

This installs the exact same code as the git-clone paths below --
`scripts/*.py` ship alongside `slr.py` and remain individually readable and
directly runnable from the installed copy, same as from a clone. Installing
via pipx is a convenience for finding and updating the tool, not a reason to
trust it any less than reading the source directly.

**Keeping up to date.** The guided TUI checks GitHub's latest release once at
startup -- a single non-blocking network call, silent if you're offline or
already current -- and prints a one-line notice if a newer version exists. It
never auto-updates and never blocks you from continuing on an old version (see
`CHANGELOG.md` for why: forcing an update mid-review risks changing dedup or
counting behavior partway through a review's own corpus, which is worse than
staying on the version you started with). Update yourself with:

```bash
pipx upgrade systematic-review-pipeline
```

You can also check on demand from the guided TUI's consolidation menu's
**"Check for updates"** action, which always reports a result (up to date /
outdated / couldn't check) rather than staying silent. Note this only works
going forward: the check can only run from a copy of the tool that already
has the checking code in it. If you installed before this feature existed,
`pipx upgrade` once gets you a copy that checks for itself from then on.

## Two ways to run this

**a) Guided (recommended): `python slr.py`** (or just `slr` if pip/pipx-installed)

An interactive terminal (needs `questionary` + `rich`, see [Running the
pipeline](#running-the-pipeline) below) that walks you through a setup wizard
-- topic, keywords, eligibility criteria, year range, contact email, optional
API keys, which sources to search, how many search phases, which web chatbot
you'll paste screening prompts into, your reviewer name -- then runs each phase
for you: search -> dedup -> prescreen skeleton -> manual-paste AI-assist
screening -> human review gate -> query expansion into the next phase.
Once every phase is done it opens a consolidation menu: merge TA-included
studies across phases, run **citation snowballing** (backward + forward,
Wohlin 2014) and merge its results in, download PDFs, **full-text screening**,
verify citations, build the extraction sheet, generate figures, export
references, export full-text exclusions with reasons, compute **inter-rater
agreement**, draft the search-methods paragraph, write a review
self-appraisal checklist, and write the provenance report.

> **Terminology:** the between-phase step is **query expansion** -- it suggests
> extra keywords drawn from the terms frequent in your included titles. It is not
> citation snowballing (Wohlin 2014), which walks the reference/citation graph.
> The two are not interchangeable: snowballing exists to escape your search
> string's keyword bias, whereas expanding the query with words you already found
> narrows toward that same vocabulary. If your protocol promises snowballing, use
> the consolidation menu's **Citation snowballing** action (or `scripts/snowball.py`
> directly in the manual path) and record it as a separate identification method.

It is **resumable**: re-run `python slr.py`, choose "Resume an existing
review," and it fast-forwards past every already-completed stage using saved
checkpoints. Every review lives under its own `runs/<run-id>/` workspace
(gitignored -- see [The srp/ library](#the-srp-library-config-state-provenance-export)
below).

A pipeline CSV that exists but can't actually be parsed (truncated mid-write,
saved with the wrong encoding) is never treated as if it were simply missing
or empty -- it stops the wizard with a clear message naming the file, instead
of silently reporting "0 records" for a stage that actually failed to read
its input. Fix or remove the named file, then resume the run.

```bash
python slr.py
```

**b) Manual (advanced / transparent): run the stage scripts yourself**

Every stage `slr.py` runs under the hood is also a standalone script under
`scripts/`, invocable directly. This is the same pipeline with nothing
hidden -- useful if you want to inspect or hand-edit intermediate CSVs
between stages, run a single stage in isolation, or wire the pipeline into
your own automation. See [Running the pipeline](#running-the-pipeline) for
the full command sequence.

```bash
git clone <this-repo-url>
cd systematic-review-pipeline

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.lock     # exact pins; requirements.txt = minimums

cp .env.example .env                 # fill in a real MAILTO; add any API keys here
                                     # (loaded automatically; flags override it)

python scripts/search.py \
    --query '"intrusion detection" AND "machine learning"' \
    --year-from 2020 --year-to 2026 \
    --mailto you@example.com \
    --max-per-source 40 \
    --out output/candidates.csv

python scripts/dedup.py  --in output/candidates.csv       --out output/candidates_dedup.csv
python scripts/screen.py --in output/candidates_dedup.csv --out output/screening.csv

# Manual-paste AI-assist screening (no API keys) -- see "AI-assist screening" below
python scripts/assist.py build --in output/candidates_dedup.csv --stage ta \
    --out prompt_ta.txt --topic "ML-IDS systematic review"
# paste prompt_ta.txt into any free web chatbot; save the reply to reply.txt
python scripts/assist.py parse --response reply.txt --into output/screening.csv --stage ta

python scripts/download.py --in output/candidates_dedup.csv --mailto you@example.com --outdir pdfs
python scripts/verify_citations.py --csv output/candidates_dedup.csv --limit 10

# Once a human has filled in ta_decision/ft_decision in screening.csv:
python scripts/extract.py --in output/screening.csv --out output/extraction.csv

# Once a human has filled in venue_tier/R/A/T/C/quality_tier in extraction.csv:
python scripts/figures.py --quality output/extraction.csv --outdir figures

# Export the included set for your reference manager:
python scripts/export.py --in output/extraction.csv --out-bib references.bib \
    --out-ris references.ris --included-only
```

### Tests

```bash
pip install pytest
python -m pytest tests/
```

The suite covers the functions whose silent breakage would put a wrong number in
a published paper: deduplication in both directions (false merges *and* missed
preprint/published pairs), the PRISMA count derivation and its cross-phase
totals, the chatbot-reply parser's refusal to fabricate or misattribute a
decision, BibTeX key collisions, Cohen's kappa against its textbook worked
example, and the guarantee that API keys never reach `runs/<id>/config.json`.
Run it after changing anything under `srp/` or `scripts/`.

A real run of either path writes `output/` and `pdfs/` (manual path) or
`runs/<run-id>/` (guided path) into your working directory -- both are
gitignored. This repository ships with a **committed demonstration run**
instead, under `examples/`: `examples/demo_output/` holds the real CSVs
produced by an actual ML-IDS search (candidates, dedup, screening, download
log, citation verification), and `examples/demo_pdfs/` holds the open-access
PDFs that `scripts/download.py` fetched. `examples/` additionally keeps a small
curated sample of each output -- including a fully-decided screening sheet, a
filled-in extraction/quality-scoring sheet, and the figures `scripts/figures.py`
renders from them (`examples/figures/`) -- so you can see the CSV and figure
shapes at a glance; see `examples/README.md` for the exact commands used and
one real API gotcha hit along the way.

### What you change vs. what you don't

Every script under `scripts/` follows the same convention, stated right after
its module docstring: the **command-line flags are what you're meant to
change** day to day (search terms, thresholds, paths, `--mailto`) -- run
`python scripts/<script>.py --help` to see them. The code *below* that point
is marked in two ways: `# --- API internals ---` over anything that builds a
request URL or parses a response from an external API (breaks only if that
API's contract changes), and `# --- core logic ---` over the dedup match
loop, the PRISMA count derivation, and the plotting geometry (safe to leave
alone unless you're deliberately extending the tool). You should not need to
edit anything below either marker for a normal review. `slr.py` and the
`srp/` library follow the same two markers for the same reason -- the wizard
prompts and consolidation menu are what you interact with; the phase
orchestration and query-expansion logic underneath are marked `# --- core logic ---`
and don't need touching for a normal review either.

## Repository layout

```
systematic-review-pipeline/
├── slr.py                  # guided interactive entry point (run this)
├── srp/                     # importable library
│   ├── config.py             # ReviewConfig (secrets are never persisted)
│   ├── env.py                  # .env loader
│   ├── normalize.py             # THE definition of "same DOI" / "same title"
│   ├── state.py                   # run workspace, atomic checkpoints, decision log
│   ├── decisions.py                 # the single AI-decision ingestion path
│   ├── agreement.py                   # Cohen's kappa + conflict lists
│   ├── appraisal.py                     # field -> critical-appraisal instrument registry
│   ├── methods_report.py                  # auto-draft the search-methods paragraph
│   ├── llm_assist.py                        # screening prompt builder + reply parser
│   ├── provenance.py                          # append-only log + PROVENANCE.md
│   └── export.py                                # BibTeX / RIS
├── tests/                   # pytest suite (`python -m pytest tests/`)
├── scripts/                  # the individual stage CLIs (manual / advanced use)
│   ├── search.py               # query up to 10 scholarly APIs
│   ├── dedup.py                  # DOI-exact then fuzzy-title+author deduplication
│   ├── screen.py                   # build the title/abstract + full-text screening sheet
│   ├── assist.py                     # manual-paste AI-assist screening (build prompt / parse reply)
│   ├── snowball.py                     # real citation snowballing (backward + forward, OpenAlex)
│   ├── download.py                       # resolve + fetch open-access PDFs (arXiv, Unpaywall, OpenAlex, S2, CORE)
│   ├── verify_citations.py                 # fabricated-citation guard: DOI -> Crossref -> diff
│   ├── extract.py                            # build the Stage-4/5 extraction + quality-scoring template
│   ├── figures.py                              # PRISMA flow diagrams + quality-/venue-tier charts
│   └── export.py                                 # export included studies to BibTeX / RIS
├── examples/                # demo inputs, a completed demo run, and generated figures
│   ├── README.md
│   ├── *_sample.csv            # small curated sample of each stage's output
│   ├── demo_output/              # CSV outputs of the ML-IDS demonstration run
│   ├── demo_pdfs/                  # open-access PDFs fetched in the demo run
│   └── figures/                      # prisma_flow, prisma_2020, quality_tiers, venue_tiers (.png + .pdf)
├── .github/workflows/tests.yml  # CI: runs the pytest suite on push/PR
├── pyproject.toml           # package metadata for `srp` (no console-script entry points)
├── requirements.txt         # minimum versions
├── requirements.lock         # exact pins -- use these for a review you'll publish
├── .env.example
├── LICENSE                  # MIT -- applies to the code (slr.py, srp/, scripts/)
├── LICENSE-docs              # CC-BY-4.0 -- applies to README.md / examples/README.md
└── .gitignore                 # ignores output/, pdfs/, runs/ -- a real run's working files
```

## The end-to-end workflow

```
Protocol  ->  Search  ->  Screen  ->  Extract  ->  Quality-assess  ->  Synthesize  ->  Report
 (PICOC,       (multi-    (title/     (per-study    (R/A/T/C           (narrative      (PRISMA
  RQs,          round      abstract    schema,       rubric,            by RQ,          flow +
  criteria,     Boolean    then       verbatim       Low/Some/          consistency     checklist,
  pre-reg)      queries)   full-text)  numbers)      High per dim)      assessment)     data avail.)
```

`scripts/search.py` -> `scripts/dedup.py` -> `scripts/screen.py` cover Search
and the mechanical half of Screening; `scripts/assist.py` optionally
manual-paste-AI-assists the screening decisions, but the decision columns
stay a human call either way. `scripts/snowball.py` adds a second,
non-keyword identification method -- real backward/forward citation
snowballing (Wohlin 2014) seeded from your included set, as distinct from
the between-phase query-expansion loop. `scripts/download.py` retrieves full
text for the full-text screening and extraction stages. `scripts/extract.py`
builds the Extract + Quality-assess template (`R`/`A`/`T`/`C` rubric columns,
again left blank for a human), optionally extended with a field-appropriate
instrument's verbatim domains from `srp/appraisal.py`. `scripts/verify_citations.py`
runs continuously, against every citation you plan to use, from extraction
through final manuscript. `scripts/figures.py` covers Report's PRISMA flow
diagram(s) and the quality-/venue-tier distribution charts; `srp/methods_report.py`
drafts the search-methods paragraph from the same recorded search-strategy
log. `scripts/export.py` covers Report's reference-list handoff to your
citation manager. `slr.py` orchestrates all of the above end to end, plus the
query-expansion loop between search rounds and the always-on provenance log.

---

## The 7-stage method

### Stage 1 -- Protocol & scoping

Fix, before running any search: the review's scope, its research questions,
and its inclusion/exclusion criteria. **PICOC** (Population, Intervention,
Comparison, Outcome, Context) is the Kitchenham & Charters (2007) adaptation of
the clinical-trial PICOS framework for software/systems topics -- use it
instead of PICOS whenever the review isn't about a clinical intervention,
since PICOS's "study design" axis doesn't map cleanly onto systems/security
literature.

**Worked example: ML-IDS PICOC**

| Element | Definition |
|---|---|
| Population | Network intrusion detection systems (NIDS) and related anomaly-detection systems for network security. |
| Intervention | Application of machine learning or AI methods (classical ML, deep learning, ensembles, transformers, GANs, federated learning) to detection, classification, or triage of network intrusions/anomalies. |
| Comparison | Signature-based or rule-based detection baselines, and/or classical (non-deep) ML baselines, where a study reports one. |
| Outcome | Reported detection accuracy/F1/AUC, false-positive rate, computational overhead, robustness to adversarial/evasive traffic, and dataset/reproducibility characteristics. |
| Context | Peer-reviewed and high-quality preprint literature, 2018-2026, in security, networking, and machine-learning venues. |

Derive your research questions (RQs) from this table -- each RQ should trace
to a specific PICOC cell (usually an Intervention sub-area), not be invented
independently of it. A typical ML-IDS review might use: RQ1 (which ML/AI
architectures are applied, and how), RQ2 (what datasets and evaluation
protocols are used, and how comparable are they), RQ3 (what performance and
robustness is reported, under what threat models), RQ4 (cross-cutting:
reproducibility and production-readiness gaps).

**Blank PICOC template**

| Element | Your definition |
|---|---|
| Population | *What systems/subjects/artifacts is this review about?* |
| Intervention | *What technique, algorithm, or approach is being evaluated?* |
| Comparison | *What is it being compared against, where a study reports one?* |
| Outcome | *What outcomes/metrics does the review care about?* |
| Context | *Time window, venue quality floor, language, and any named exceptions.* |

**Inclusion/exclusion criteria template**

Included:
- Primary studies proposing or evaluating an ML/AI method directly in scope.
- Cross-cutting work spanning two or more PICOC-Intervention sub-areas.
- Foundational/survey literature engaged as background evidence, cited but not
  counted as a primary study.

Excluded:
- Unpublished internal/vendor documentation.
- Work in an adjacent field with no specific in-scope angle (e.g., general
  malware classification with no network-intrusion framing).
- Non-English literature without a critical foundational gap.
- Gray literature (vendor blogs, AI-aggregator pages) regardless of topical fit
  -- state this source-quality floor in the protocol up front, not improvised
  mid-screening.

The guided wizard now asks about the grey-literature exclusion decision above
directly (PRISMA item 14 -- excluding it is defensible, but it's also a
publication-bias amplifier, so it needs a stated reason rather than silence),
plus a reporting-bias-assessment question (item 21 -- only meaningful if you
plan to meta-analyze; if you don't, the tool records that the item is scoped
out rather than fabricating a funnel plot for a narrative synthesis), and the
administrative items -- funding, competing interests, language restriction
(items 25, 26, 5). All of these render into `PROVENANCE.md`.

**Pre-register the protocol.** Kitchenham & Charters' guidelines don't mandate
registration the way Cochrane/PRISMA clinical reviews do -- do it anyway.
[OSF Registries](https://osf.io/registries) is the practical default for
CS/security/systems topics: general-purpose, free, and it timestamps a frozen
copy of your PICOC, RQs, search strings, and criteria before you run the first
query. [PROSPERO](https://www.crd.york.ac.uk/prospero/) is the standard
clinical/health registry and will typically reject a registration with no
health outcome -- use OSF instead unless your topic genuinely has a clinical
angle. Registering costs under an hour and gives you a citable, timestamped
artifact proving the RQs and criteria weren't adjusted after seeing results.

---

### Stage 2 -- Search strategy

Two structurally different approaches, usually combined:

- **Boolean queries** run directly against bibliographic databases (IEEE
  Xplore, Scopus, ACM Digital Library, Web of Science) -- high precision on
  field scope, fully reproducible, but requires institutional access.
- **API / web search / snowballing** (arXiv, Semantic Scholar, OpenAlex, DBLP,
  Google Scholar, backward/forward citation chasing per Wohlin 2014 --
  `scripts/snowball.py`, or the guided path's **Citation snowballing** menu
  action) -- lower barrier, catches preprints Boolean-indexed databases miss,
  but is less systematic and harder for someone else to reproduce exactly.

#### (a) General fill-in template

Thematic blocks are ANDed together; synonyms within a block are ORed:

```
(<Field>:"<block-1-term-a>" OR <Field>:"<block-1-term-b>" OR <Field>:"<block-1-term-c>")
AND
(<Field>:"<block-2-term-a>" OR <Field>:"<block-2-term-b>" OR <Field>:"<block-2-term-c>")
[AND (<Field>:"<block-3-term-a>" OR ...)]     # optional third thematic block if your
                                                # topic is a three-way intersection
Filters: <year-from>-<year-to>, <document types>, <language>
```

`<Field>` is `Title` for IEEE Xplore and ACM DL command/advanced search; it is
`TITLE-ABS-KEY` (a composite field covering the whole query) for Scopus. Run
one query per pairwise thematic intersection you actually care about -- don't
cram three-plus blocks into one query with nested ANDs; it collapses recall
fast and gets hard to audit later.

The guided wizard (`python slr.py`) now builds exactly this block structure for
`search.py`'s automated sources too: it asks for one concept block at a time
(synonyms within a block, comma-separated) and ANDs the blocks together --
`(intrusion detection OR IDS) AND (machine learning OR deep learning)` -- rather
than flattening every term you enter into one giant AND chain. Five keywords
used to become a five-way mandatory AND before this; now they form whatever
block structure you actually intend.

#### (b) Worked example: ML-IDS Boolean strings, three databases

Two thematic blocks: **detection-task terms** and **ML/AI-method terms**.

- Block 1 (detection task): `intrusion detection`, `IDS`, `NIDS`,
  `anomaly detection`, `network intrusion`
- Block 2 (ML/AI method): `machine learning`, `deep learning`,
  `artificial intelligence`, `neural network`, `ensemble`, `transformer`,
  `GAN`, `federated learning`

**IEEE Xplore, Command Search syntax:**

```
("Document Title":"intrusion detection" OR
 "Document Title":"IDS" OR
 "Document Title":"NIDS" OR
 "Document Title":"anomaly detection" OR
 "Document Title":"network intrusion")
AND
("Document Title":"machine learning" OR
 "Document Title":"deep learning" OR
 "Document Title":"artificial intelligence" OR
 "Document Title":"neural network" OR
 "Document Title":"ensemble" OR
 "Document Title":"transformer" OR
 "Document Title":"GAN" OR
 "Document Title":"federated learning")
```

Filters: e.g. 2018-2026, Conferences + Journals, English.

**Scopus, Advanced Search syntax:**

```
TITLE-ABS-KEY ( ( "intrusion detection" OR "IDS" OR "NIDS" OR
  "anomaly detection" OR "network intrusion" )
AND ( "machine learning" OR "deep learning" OR "artificial intelligence" OR
  "neural network" OR "ensemble" OR "transformer" OR "GAN" OR
  "federated learning" ) )
AND PUBYEAR > 2017 AND PUBYEAR < 2027
```

**ACM Digital Library, Advanced Search syntax:**

```
[[Title: "intrusion detection"] OR [Title: "IDS"] OR [Title: "NIDS"] OR
 [Title: "anomaly detection"] OR [Title: "network intrusion"]]
AND
[[Title: "machine learning"] OR [Title: "deep learning"] OR
 [Title: "artificial intelligence"] OR [Title: "neural network"] OR
 [Title: "ensemble"] OR [Title: "transformer"] OR [Title: "GAN"] OR
 [Title: "federated learning"]]
```

**Web of Science, Advanced Search syntax** (this is what `search.py --wos-api-key`
actually sends, via `TS=` -- Topic, which covers title + abstract + author
keywords + Keywords Plus):

```
TS=("intrusion detection" OR "IDS" OR "NIDS" OR "anomaly detection" OR
    "network intrusion")
AND
TS=("machine learning" OR "deep learning" OR "artificial intelligence" OR
    "neural network" OR "ensemble" OR "transformer" OR "GAN" OR
    "federated learning")
AND PY=(2018-2026)
```

**Dataset names (CIC-IDS2017, UNSW-NB15, NSL-KDD) and target venues (IEEE
TIFS, IEEE TDSC, USENIX Security, NDSS, ACM CCS, Computers & Security, IEEE
Access) are deliberately not in the Title-field strings above.** Dataset names
almost never appear in a title -- they live in the abstract or keywords -- so
a Title-only Boolean query using them would starve recall. Two better uses:

1. Run a **separate abstract/keyword-field pass** where the database supports
   it (Scopus's `TITLE-ABS-KEY` already covers abstract; for IEEE Xplore, use
   `"Abstract":"CIC-IDS2017"` etc. as a distinct supplementary query, not
   merged into the Title-field pass, since mixing field scopes inside one
   Boolean string silently changes what "AND" means).
2. Use the venue list to seed a **manual/snowballing pass**: hand-check the
   last 2-3 years of TIFS/TDSC/CCS/NDSS/USENIX Security/Computers &
   Security/IEEE Access tables of contents for ML-IDS papers the Boolean
   queries missed. Scopus can also do this directly with
   `AND SRCTITLE("IEEE Transactions on Information Forensics and Security")`
   appended to a query, as a targeted venue-scoped check.

**A genuine methodological caution specific to this domain:** `"IDS"` as a
bare Title-field acronym is a false-positive magnet (matches "Interactive
Display System," "Integrated Delivery System," etc., in unrelated fields).
Screen its hit list in isolation before trusting the combined AND result -- if
the false-positive rate is high, tighten it to `"IDS" AND ("network" OR
"intrusion")` as its own sub-clause, since ML/AI terms alone won't reliably
filter out a false "IDS" hit from an unrelated field that happens to also
mention, say, "ensemble" in an unrelated statistical sense.

#### Database-specific syntax differences

| | IEEE Xplore | ACM Digital Library | Scopus | Web of Science |
|---|---|---|---|---|
| Field qualifier | Repeat `"Document Title":` before **every** term | Wrap **every** term in its own `[Title: "..."]` bracket | One `TITLE-ABS-KEY(...)` wrapping the *entire* boolean expression | One `TS=(...)` per clause (or `TI=`/`AU=`/`SO=` for title/author/source-only) |
| Default search field | None -- field must be explicit per term | None -- field must be explicit per term | `TITLE-ABS-KEY` covers title + abstract + author keywords, not title alone | `TS=` (Topic) covers title + abstract + author keywords + Keywords Plus |
| Year filter | UI facet or separate filter parameter | UI facet (Publication Date range) | Inline operators: `PUBYEAR > 2017 AND PUBYEAR < 2027` | Inline: `PY=(2018-2026)` |
| Phrase matching | Double quotes | Double quotes | Double quotes | Double quotes |
| Boolean operators | `AND`/`OR`/`NOT`, conventionally capitalized | `AND`/`OR`/`NOT` | `AND`/`OR`/`AND NOT`, case-insensitive | `AND`/`OR`/`NOT`, conventionally capitalized |

Draft each database's string separately from the same thematic block list --
don't try to write one universal string and hope it parses everywhere. Also
decide up front whether you're searching Title-only or Title-Abstract-Keyword:
a database that defaults to a broader field (Scopus's `TITLE-ABS-KEY` and Web
of Science's `TS=` vs. IEEE Xplore/ACM DL's Title-only) will structurally
return more records for the same thematic blocks -- expect and disclose that
yield asymmetry rather than treat
it as a bug.

**Run the full battery of databases in the first formal search round**,
informed by the pre-registered protocol from Stage 1, rather than treating
Boolean-database search as a later phase bolted onto an initial informal
web-search pass. Late-discovered, topically-relevant records that then need a
dedicated access-recovery round are exactly the failure mode this avoids.

---

### Stage 3 -- Screening & selection

PRISMA 2020's two-stage screening: **title/abstract** against the
inclusion/exclusion criteria, then **full-text** eligibility for everything
that survives. Before either pass, de-duplicate -- exact DOI match first, then
fuzzy title match for DOI-less records (arXiv preprints, some gray-adjacent
sources). This is exactly what `scripts/dedup.py` automates (see "Running the pipeline" below).

**Illustrative screening funnel** (a generic, illustrative shape -- not real
study results -- to show the level of detail a PRISMA flow diagram needs):

```
Identification                                Screening & Eligibility
  n = X records  ─────────────────────────►  De-duplication (dedup.py)
  (across N search rounds/sources)            + title/abstract screening
                                               + full-text eligibility
                                                         │
                        ┌────────────────────────────────┼──────────────────┐
                        ▼                                                   ▼
              Excluded: n = ...                                   Included: n = ...
              - Round 1: <n> (reason, denominator)
              - Round 2: <n> (reason, denominator)
```

Every number in the excluded box should be traceable to a specific process
step in your methodology section -- name the mechanism (deduplication,
keyword filtering, screening) and give the raw denominator it came from, not
just a bare total.

**Improvements worth building in from the start, not bolted on later:**

1. **Dual independent reviewers with Cohen's kappa.** Have each reviewer
   record their title/abstract decision **before** any discussion -- a
   `reviewer` column per decision, not a merged consensus-only record (see
   `screen.py`'s `screening.csv` schema below), then compute kappa on the two
   independent decision columns. A kappa below about 0.6 signals the criteria
   need tightening before you screen further, not just "agree to disagree."
2. **A dedicated dedup tool, not manual eyeballing.** `scripts/dedup.py`
   (exact DOI match, then `rapidfuzz` token_sort_ratio fuzzy title match) is
   deterministic and re-runnable, unlike a human scanning a spreadsheet.
3. **A documented screening log from the first record onward.** Every
   screened record gets an explicit decision + reason at both stages, even
   for the overwhelming majority you exclude in seconds -- this is what makes
   a PRISMA flow diagram auditable rather than asserted.
4. **Verify every downloaded PDF's title page against its expected title
   before it enters extraction.** Automated OA resolution (`scripts/download.py`) can
   fetch the wrong PDF -- a similarly-titled paper, a landing page mislabeled
   as a PDF -- often enough to matter. Check the actual file every time.

---

### Stage 4 -- Data extraction

For every study that survives full-text screening, extract a **fixed schema**
of fields, not free-form notes. Fixed fields are what make cross-study
synthesis (Stage 6) and quality scoring (Stage 5) tractable at scale.

**Extraction CSV header** (exactly what `scripts/extract.py` writes -- see
"Running the pipeline" below for the full command):

```csv
id,title,authors,year,venue,doi,thematic_class,study_type,contribution,key_findings,rq_mapping,limitations,venue_tier,R,A,T,C,quality_tier,extraction_reviewer,extraction_date,notes
```

Column notes:

- `venue_tier`: numeric 1/2/3, see Stage 5 -- keep it separate from
  `quality_tier`; they are different axes.
- `R`/`A`/`T`/`C`: the per-dimension Rigor/Artifact/Threat-model/Currency
  ratings (`Low`/`Some`/`High` concern) that `quality_tier` is aggregated
  from -- see Stage 5's rubric.
- `key_findings`: copy numbers verbatim from the source, with enough context
  (dataset, split, baseline, hardware) to be usable later without re-reading
  the paper. Do not convert units at extraction time -- do conversions at
  synthesis time, with the original preserved alongside.
- `rq_mapping`: which research question(s) this study evidences; a study can
  map to more than one RQ (semicolon-separated).
- One row per study. If a study is later found to be a duplicate of another
  (e.g., a preprint and its camera-ready), merge into one row and note the
  merge in `notes` -- don't keep both as separate synthesis inputs.

`extract.py` only builds the sheet with blank rows; filling it in (and
correcting a later mistake -- a wrong `R` rating, a typo in `key_findings`)
is a human step. In the guided path, do this through the consolidation menu's
**Review/correct an extraction record** action rather than hand-editing the
CSV: `venue_tier`/`R`/`A`/`T`/`C`/`quality_tier` are edited through a menu so
an invalid value can't be typed, free-text fields keep their current value as
the default so pressing Enter never blanks them, and every change is logged
to `provenance.jsonl` -- unlike a raw CSV edit, which leaves no record of what
changed or when. Editing R/A/T/C recomputes `quality_tier` automatically per
Stage 5's formula.

---

### Stage 5 -- Quality assessment

A structured per-study quality rating, distinct from inclusion/exclusion. It
tells the synthesis stage *how much weight* to put on a given study's
findings, and should be visible to the reader as a rubric, not an implicit
"trust me" judgment.

**The four-dimension rubric (R/A/T/C)**, adapted from Kitchenham & Charters
(2007) and Dybå & Dingsøyr (2008) software-engineering quality criteria -- not
a clinical instrument (don't reach for a medical risk-of-bias tool for a
systems/ML topic; it won't have the right axes):

| Code | Dimension | Criteria |
|---|---|---|
| **R** | Rigor | Appropriateness of methodology for the claims made; statistical validity; threats-to-validity discussion; experimental scale. |
| **A** | Artifact availability | Source code, dataset, and hyperparameter availability; independent verifiability. |
| **T** | Threat-model completeness | Explicit adversary/attack model (e.g., adaptive/adversarial-evasion traffic vs. static test set); stated assumptions; disclosed limitations. |
| **C** | Currency | Use of current datasets/standards (e.g., not solely relying on the outdated KDD Cup 99), or justified deviation. |

Each dimension is rated per study on a three-point scale, styled after the
Cochrane RoB-2 traffic-light convention:

- **L** (Low concern / green) -- dimension fully satisfied
- **S** (Some concerns / yellow) -- partially satisfied, minor gaps
- **H** (High concern / red) -- significant gaps or missing

Score primary studies only -- exclude foundational/definitional references,
standards documents, and studies explicitly engaged as competing/related
surveys rather than primary evidence.

**Publish an explicit, mechanical aggregation formula, decided before scoring
begins** -- otherwise a different reviewer applying the same four L/S/H
ratings can't reproduce the same letter tier. This project's formula, implemented
in `srp/quality_tier.py` and applied automatically by the guided TUI's "Review/
correct an extraction record" step whenever R/A/T/C are all set:

```
L = 2 points, S = 1 point, H = 0 points, per dimension.

Hard gate first, no compensation: R = H or T = H -> tier is C, full stop --
a fatal rigor or threat-model gap should not be buyable back by strong
artifact availability or currency. (A = H / C = H do NOT gate -- missing
artifacts and stale datasets are common enough in ML-security papers to be
graded via points, not disqualifying.)

Otherwise, sum all four dimensions (0-8 raw points) and map:
  7-8 points -> A
  5-6 points -> B
  0-4 points -> C
```

Three tiers (A/B/C), not a finer A-/B+/B-/C+ scale -- with 4 dimensions x 3
levels, a 7-bucket scale implies more precision than the inputs support, and
removes the earlier version's "0-2 points -> C+/C, split by reviewer judgment"
step entirely: given any R/A/T/C combination there is exactly one correct
tier, no judgment call left in the formula itself. `quality_tier` can still be
set by hand for a documented reason -- the guided editor logs that as a
distinct "manual override" event (with the computed tier it disagreed with)
rather than letting it silently diverge, and recomputes from the formula again
the next time any of R/A/T/C is edited.

Publish whatever rule you actually use, before applying it -- not
reconstructed after the fact to match intuitions.

**Quality tiers are not venue tiers -- keep the two axes separate.** It's
tempting to conflate "published somewhere prestigious" with "methodologically
strong." Don't:

- **Venue tier** (a property of *where* something was published): Tier-1
  flagship peer-reviewed venues (e.g., IEEE S&P, USENIX Security, ACM CCS,
  NDSS); Tier-2 preprints and workshop papers not yet at a flagship venue;
  Tier-3 other peer-reviewed venues, standards documents, or theses.
- **Quality tier** (a property of *how rigorously the work was done*): the
  A/A-.../C+/C letter grade from the R/A/T/C rubric above.

A Tier-3-venue paper can score A on rigor/artifacts/threat-model/currency (a
well-instrumented, open-source, current-dataset study that hasn't yet reached
a flagship venue), and a Tier-1-venue paper can score C on artifact
availability (a top-venue paper with no released code or dataset split).
Report both axes, and don't let one stand in for the other in your synthesis
prose. Retain C-tier studies (quality-flagged, not excluded) when they're
topically in scope -- exclusion and quality are different decisions.

**Release the per-dimension scores, not just the aggregate tier**, as
supplementary material (a table of study x {R,A,T,C} -> {L,S,H}) -- it's what
lets a reader see *why* a study scored B+ rather than A, and it's the raw
material Cohen's kappa needs.

**Quality-rubric scoring sheet template:**

```csv
study_id,rigor_R,artifact_A,threat_model_T,currency_C,raw_points,aggregate_tier,reviewer,notes
```

### Field-driven critical appraisal (PRISMA item 11)

R/A/T/C above is this project's own bespoke rubric, purpose-built for ML-security
papers -- it is not a validated, externally citable instrument, and PRISMA item
11 expects one. The guided wizard now asks which field your review is in and
auto-recommends a real, cited instrument for it: Dybå & Dingsøyr (2008) for
software engineering/CS/cybersecurity, Cochrane RoB 2 for randomized trials,
ROBINS-I for non-randomized studies, QUADAS-2 for diagnostic-accuracy studies,
CASP for qualitative research, MMAT for mixed-methods/social-science reviews,
JBI for nursing/allied health, Drummond for economic evaluations, and so on --
see `srp/appraisal.py` for the full registry, every domain transcribed verbatim
from its cited source. Use R/A/T/C *and* the field instrument together if you
want both the domain-specific security lens and an externally defensible check;
`extract.py --instruments <key>` appends the chosen instrument's verbatim
domains as extra extraction-sheet columns, alongside R/A/T/C, not instead of it.

Certainty of evidence (PRISMA items 15, 22) is handled the same way: GRADE for
quantitative findings (works without meta-analysis too -- see Murad et al. 2017),
GRADE-CERQual for qualitative synthesis findings, or an explicit stated reason
when the field has none (software engineering does not; see the auto-composed
justification in `PROVENANCE.md`). A review-level self-check instrument (AMSTAR
2, ROBIS, DARE, or the Campbell/MECCIR standards) appraises the review itself
rather than its primary studies -- these three things (primary-study appraisal,
certainty of evidence, review self-appraisal) are genuinely different objects,
and conflating them is a common mistake. Its name and citation are recorded in
`PROVENANCE.md` either way; the consolidation menu's **Write review
self-appraisal checklist** action additionally writes
`review_self_appraisal_<key>.md`, a fillable table with the chosen instrument's
actual domains as rows -- the review-level counterpart to how
`extract.py --instruments <key>` appends real columns for a primary-study
instrument. Rating and justification are left blank for you to fill in against
the finished manuscript; nothing here is scored automatically.

**Piloting and double extraction** (Cochrane 5.4, Kitchenham 6.4): pass
`--pilot N` to `extract.py` to also sample N studies into a second,
identically-shaped sheet for a second reviewer to extract independently; then
`extract.py --compare-a <sheet1> --compare-b <sheet2>` reports Cohen's kappa and
every disagreement per categorical column (R/A/T/C, quality_tier, venue_tier,
and any appraisal-instrument columns).

---

### Stage 6 -- Synthesis

Once studies are extracted and quality-scored, synthesize findings
**organized by research question**, not study-by-study. Two families exist:

- **Meta-analysis** -- statistically pools comparable quantitative results
  (same metric, same units, comparable methodology) into a combined effect
  estimate. Requires genuine comparability across studies.
- **Narrative/thematic synthesis** (SWiM-style -- Synthesis Without
  Meta-analysis) -- groups findings thematically by RQ, states what's
  consistent vs. what conflicts, and explains *why* (differing datasets,
  threat models, metrics) rather than pooling numbers that aren't actually the
  same thing.

**When NOT to meta-analyze:** whenever studies report the same *kind* of
claim (e.g., "transformer-based detectors outperform tree-based detectors on
recall for rare attack classes") but measure it on different datasets
(CIC-IDS2017 vs. UNSW-NB15 vs. NSL-KDD), different splits, or different
operating points (offline batch evaluation vs. online/streaming detection). If
two studies' reported numbers for the "same" claim differ by an order of
magnitude because one measured a clean held-out test set and the other
measured performance under adversarial evasion, report this as *directionally*
consistent (direction agrees) rather than pooling the two numbers or treating
either as a precisely comparable point estimate -- a naive meta-analysis here
manufactures false precision.

**Explicit consistency assessment.** For any claim two or more studies bear
on, state outright whether they **converge** or **conflict**, and if they
conflict, say what methodological difference plausibly explains it. Structure
this as a table: claim -> studies bearing on it -> direction of each study's
finding -> consistent/conflicting -> explanation if conflicting.

**Organize by RQ, not by study.** A useful test: if you removed the study
citations from a synthesis paragraph, the paragraph should still make a
coherent claim about the field -- citations should support a stated position,
not be the entire content of the paragraph.

---

### Stage 7 -- Reporting

**PRISMA 2020 flow diagram.** Four boxes: **Identification** (total records
found, across however many search rounds/sources) -> **Screening &
Eligibility** (the process applied) -> two terminal boxes, **Excluded** (with
a reasoned breakdown, not a bare number) and **Included** (the final count).
Every number in the excluded box should name the mechanism and denominator it
came from -- see Stage 3's funnel shape above.

**PRISMA 2020 checklist.** Map your review against the 27-item PRISMA 2020
checklist item by item: **Yes** (fully reported), **Partial** (reported, at a
coarser grain than the item asks for), **NA** (genuinely not applicable --
e.g., meta-analysis-specific items when you did a narrative synthesis), or
**No** (not reported). Grade honestly against what the manuscript and its
supplement actually contain -- a checklist that's all "Yes" on a first attempt
is a sign you're grading against an aspirational review, not the one you
wrote.

**Data-availability / supplement plan.** Decide, before you finish, exactly
what gets released alongside the manuscript:

- A per-study table: every included study, its thematic classification, and
  its aggregate quality tier.
- The verbatim search query strings and applied filters, per database, and the
  date each was run.
- The full screening log (every record, both stages, decision + reason +
  reviewer).
- The full extraction matrix (Stage 4's schema, one row per study).
- The full quality-rubric grid (per-study, per-dimension scores, not just the
  aggregate tier).

Release all of the above -- it's what lets a reader independently check your
synthesis rather than just trust it.

---

## Running the pipeline

Dependency-light: the individual stage scripts under `scripts/` need only
`requests`, `pandas`, `rapidfuzz`, and `matplotlib`. The guided `slr.py`
entry point additionally needs `questionary` and `rich` for its terminal UI
-- both are already in `requirements.txt`. Requires Python 3.10+ (some type
hints use the 3.10 `X | None` union syntax; see `pyproject.toml`'s
`requires-python`).

```bash
pip install -r requirements.txt
```

This section documents each stage script individually, for the manual /
advanced path. `python slr.py` (see [Two ways to run this](#two-ways-to-run-this))
runs the same scripts for you, in order, with checkpointed resume.

### `scripts/search.py` -- query up to ten scholarly APIs, write `candidates.csv`

```bash
python scripts/search.py \
    --query '"intrusion detection" AND "machine learning"' \
    --year-from 2020 --year-to 2026 \
    --mailto you@example.com \
    --max-per-source 40 \
    --out output/candidates.csv
```

Writes one row per candidate to `output/candidates.csv` (columns: `id, source,
title, authors, year, venue, doi, url, abstract`). A single source's failure
(network error, exhausted rate limit) is logged to stderr and skipped, not
fatal to the whole run. `--mailto` is required (falls back to the `MAILTO`
environment variable) -- OpenAlex and Crossref use it for their "polite pool"
of better rate limits, and NCBI requires it on every PubMed call.

#### Sources and their optional API keys

**Every API key is optional.** Six sources need no key at all. For the four
that do, a missing key means that source is *skipped with a note on stderr* --
never an error, and never fatal to the run. Select a subset with `--sources`
(default: all ten).

| Source | Key | Without a key | Documented rate limit |
|---|---|---|---|
| `openalex` | none | full access | polite pool via `--mailto` |
| `crossref` | none | full access | polite pool via `--mailto` |
| `arxiv` | none | full access | ~1 request/3 s (informal) |
| `doaj` | none | full access | 2 requests/s |
| `semanticscholar` | `--s2-api-key` | works, shared anonymous pool (~100 req/5 min across *all* keyless users) | 1 request/s with a key |
| `pubmed` | `--pubmed-api-key` | works at 3 requests/s | 10 requests/s with a key |
| `ieee` | `--ieee-api-key` | **skipped** | 10 calls/s, 200 calls/day |
| `scopus` | `--scopus-api-key` | **skipped** | 9 requests/s, 20,000/week |
| `springer` | `--springer-api-key` | **skipped** | 100 req/min, 500/day (free Basic tier) |
| `core` | `--core-api-key` | **skipped** | 25 req/min personal, 10 req/min academic |
| `wos` | `--wos-api-key` | **skipped** | 2/3/5 req/s (Basic/Advanced/Premium), plus an annual full-record quota |

Each key also falls back to an environment variable (`S2_API_KEY`,
`IEEE_API_KEY`, `SCOPUS_API_KEY`, `SPRINGER_API_KEY`, `PUBMED_API_KEY`,
`CORE_API_KEY`, `WOS_API_KEY`), which `.env` supplies -- see `.env.example`. Precedence is
**flag > exported env var > `.env`**. The throttles above are enforced in
`search.py` as documented constants, each carrying its source URL in a comment;
HTTP 429 is retried with backoff on every source.

Keys are never written into your run folder. `runs/<id>/config.json` holds no
credentials, because that folder is the artifact you are meant to share with
co-authors and attach as a reproducibility appendix. A key typed at the guided
prompt lasts for that session only; **`.env` is the durable home for a key** and
is what a resumed run reads them back from. Keys reach the stage scripts through
the subprocess environment rather than the command line, so they stay out of your
terminal scrollback and out of the process table on a shared machine.

#### The search-strategy log (PRISMA items 6 and 7)

Every run writes `<out>_search_strategy.csv` (the guided path puts it at
`runs/<id>/phase_N/search_strategy.csv`) with one row per source: the **exact
query string sent to that source**, the UTC timestamp it was sent, how many
records came back, how many that source says exist, and whether the result was
truncated.

You need this and it cannot be reconstructed afterwards. PRISMA item 7 requires
the full search strategy for each database including filters and limits, and
item 6 the date last searched -- and the query is *transformed per source*
(arXiv gets `all:{q}`, Scopus gets `TITLE-ABS-KEY(...) AND PUBYEAR > ...`,
Crossref gets `query.bibliographic`, DOAJ gets a Lucene range). Those have
materially different field scopes, and `candidates.csv` records only a `source`
label.

The log also makes **truncation visible**. `--max-per-source` caps every source,
and that cap was previously reported as "records identified". It is not: it is a
relevance-ranked sample drawn by each API's own undocumented ranking. When a
source is truncated, `search.py` says so on stderr and the guided path warns you
and logs which sources to provenance. Raise `--max-per-source` for a real search
round, or report the truncation explicitly in your methods.

One thing the log will show you immediately: a query you wrote as `"a" AND "b"`
can report millions of "available" records on Crossref, because
`query.bibliographic` scores relevance rather than applying your boolean. Read
the `total_available` column before trusting any source's hit count as a
database-level result.

#### Five things worth knowing before you rely on these

**IEEE, Scopus, and Web of Science need an institutional subscription.** None
is realistically obtainable by an unaffiliated researcher: IEEE limits API
access to "current IEEE customers", a Scopus key is authenticated against your
institution's IP range, and a Web of Science Expanded API key requires a
separate licensing agreement with Clarivate on top of your institution's WoS
subscription. Get the access sorted before planning a review around them.

**Scopus only works on campus, unless you have an institutional token.** Pass
`--scopus-insttoken` (or set `SCOPUS_INSTTOKEN`) to use a Scopus key from off
your university network; Elsevier support must enable it for your account.

**Scopus abstracts cost throughput.** Abstracts live only in Scopus's COMPLETE
view, which caps pages at 25 records instead of 200. `search.py` defaults to
`--scopus-view COMPLETE` and automatically falls back to `STANDARD` if your key
is refused -- in which case Scopus rows arrive with an **empty abstract**, and
title-only screening is all they can support.

**IEEE's 200 calls/day is enforced per run, not per day.** `search.py` keeps no
cross-run counter, so several runs in one day can still exhaust the quota.

**Web of Science's annual full-record quota is real and shared across every
run.** Unlike IEEE's per-run call counter, this one isn't tracked locally at
all -- a Basic-tier institutional key typically gets 50,000 full records/year
across your whole institution, not just this tool. Keep `--max-per-source`
modest while testing.

### `scripts/dedup.py` -- de-duplicate by DOI, then fuzzy title + author match

```bash
python scripts/dedup.py --in output/candidates.csv --out output/candidates_dedup.csv \
    --title-threshold 92
```

Exact (normalized) DOI match first; then `rapidfuzz` `token_sort_ratio` fuzzy
title matching across **all** remaining records, corroborated by a shared author
surname. Adds `doi_norm`, `title_norm`, `duplicate_of`, `dedup_method` columns;
canonical (non-duplicate) records have an empty `duplicate_of`.

This step deletes records before a human ever sees them and its output is the
PRISMA "duplicates removed" number, so the matching rules are deliberately
asymmetric -- a missed duplicate costs one manual catch during screening, a false
merge silently deletes a study from your review forever. Three rules follow from
that:

- **`token_sort_ratio`, not `token_set_ratio`.** The latter scores 100 whenever one
  title's token set is a subset of the other's, so "Anomaly Detection in IoT
  Networks" and "Anomaly Detection in IoT Networks Using Federated Learning" scored
  a perfect match and the second paper was deleted. No threshold could fix that.
- **A length guard** (`--min-length-ratio`, default 0.75). A title that is a strict
  extension of another ("... : A Survey") is a different paper.
- **A shared author surname is required** for any fuzzy merge. Generic titles are
  common in this field -- the sample corpus alone contains two distinct book
  chapters both titled "Machine Learning for Intrusion Detection", by different
  authors -- and title similarity alone is not evidence they are the same work. If
  either record has no parseable author, the merge is refused.

Fuzzy matching runs across year boundaries and across records that already have
DOIs, because the most common duplicate in a real review is an arXiv preprint and
its published version, which differ in **both** year and DOI. Those merges are
labelled `fuzzy_title_crossdoi_<score>` so you can audit them.

**Canonical-record selection**, once a cluster of duplicates is identified, is a
strict priority order: has a DOI (stays citable) > has an abstract (screenable
without hunting down the full text) > first-seen. DOI-presence is deliberately
primary -- `references.bib` is built from the canonical record, and an uncitable
survivor is a bigger loss than a thin one -- so a DOI-bearing record stays
canonical even if a DOI-less duplicate happens to carry a richer abstract.
Abstract-presence only breaks ties among records that are otherwise equal on
citability; it exists because keyed sources like Scopus/IEEE are frequently
abstract-less (entitlement-gated), so without this a paper found only by two
abstract-less sources would silently end up with no abstract for TA screening,
even when the corpus elsewhere had one.

### `scripts/screen.py` -- build the screening spreadsheet

```bash
python scripts/screen.py --in output/candidates_dedup.csv --out output/screening.csv
```

Writes one row per canonical record with blank `ta_decision`/`ta_reason`
(title/abstract stage) and `ft_decision`/`ft_reason` (full-text stage) columns,
an `abstract` column (so both you and the AI-assist prompt can screen on it), and
a `reviewer` column. Duplicate this file per reviewer before computing
inter-rater agreement -- see [Inter-rater agreement](#inter-rater-agreement-cohens-kappa).

**Re-running is safe.** If the output already exists, decisions already recorded
in it are carried over onto the rebuilt sheet, matched by record id, and the
counts are reported. Adding a search source and re-running the pipeline is an
ordinary thing to do, and it used to destroy every decision in the file silently,
with exit code 0. Pass `--force` to deliberately start over from blank; it tells
you how many decisions it is discarding. A record that is no longer canonical
(now flagged a duplicate) is dropped, and if it carried a decision you get a
warning rather than silence.

### `scripts/assist.py` -- manual-paste AI-assist screening

No API keys, no cost, no automated calls to any LLM provider. `assist.py
build` compiles a ready-to-paste screening prompt with the undecided records
embedded in it; you paste that prompt into **any** free web chatbot (ChatGPT,
Claude, Gemini, Copilot, or any other), save its reply to a text file, and
`assist.py parse` reads the reply back and writes `include`/`exclude`/`maybe`
decisions plus a one-sentence reason into `screening.csv`.

```bash
python scripts/assist.py build --in output/candidates_dedup.csv --stage ta \
    --out prompt_ta.txt --topic "ML-IDS systematic review"
# -> paste prompt_ta.txt into your chatbot of choice; save the reply to reply.txt

python scripts/assist.py parse --response reply.txt --into output/screening.csv \
    --stage ta --prompt prompt_ta.txt
```

`build` also writes `prompt_ta.txt.ids.json` alongside the prompt file -- exactly
the ids sent in that batch. Pass that same prompt path to `parse` via `--prompt`
so it can validate the reply against the batch actually sent, not every id in the
whole sheet; without `--prompt`, `parse` still works but warns that a hallucinated
or echoed id belonging to some other undecided row elsewhere in the sheet
wouldn't be caught.

`--stage` is `ta` (title/abstract) or `ft` (full text) -- run `build`/`parse`
once per stage. `--batch-size` (default 20) and `--start` let you keep each
paste small enough for a chatbot's context window and work through a large
corpus in batches without re-sending already-screened rows. `build` already
skips any row with a decision filled in, so re-running it after a partial
`parse` only re-prompts what's still undecided.

**Screen against your protocol's criteria, not "relevance".** Pass
`--criteria-file` (or `--criteria`, or `--run <run-dir>` to read them from the
review's `config.json`) so the prompt applies your pre-specified eligibility
criteria verbatim. Without one of those, `build` **refuses to write the
prompt** (exit code 2): the fallback would be a generic "include if plausibly
relevant" instruction, which is exactly the unstructured judgement
pre-registration exists to prevent, and a review screened that way cannot
claim to have applied its stated criteria. Pass `--allow-generic-fallback` if
you genuinely intend to screen that way anyway -- it still runs, with a loud
warning on stderr, but it is now an explicit choice rather than a silent
default. The guided path (`slr.py`) asks the same question interactively
before running AI-assist with no criteria configured. The criteria file may
use `INCLUDE:` / `EXCLUDE:` section headers.

**The AI only assists -- it does not decide.** Every `assist.py`-parsed
decision is a *proposed* decision, written into the same `ta_decision`/
`ft_decision` columns a human would fill in by hand, with `reviewer` set to
`ai-assisted` so it's traceable later. The actual inclusion/exclusion call
happens at the human review gate (`slr.py`'s "Human review gate" step in the
guided path; a manual read-through of `screening.csv` in the manual path) --
see the integrity guardrails below.

Four things the parser refuses to do, because each one silently corrupts the
screening log:

- **It will not decide a record that was not in the batch.** Replies are
  reconciled against the ids actually sent (the guided path always does this
  correctly; the manual CLI needs `--prompt` -- see above); an id the model
  invents or echoes from an earlier batch is reported and dropped, not written
  onto whatever record happens to carry that number.
- **It will not overwrite a decision that is already recorded.** Re-parsing a stale
  reply used to replace a human's decision with the model's while leaving the
  human's initials on the row. Pass `--overwrite` if you mean it; attribution then
  moves to `ai-assisted` along with the decision.
- **It will not accept a "maybe" at the full-text stage.** `ft_decision` is
  PRISMA's terminal stage (include or exclude only) -- the prompt doesn't offer
  MAYBE there, and a chatbot that answers one anyway has it refused rather than
  silently written, where it would vanish from the PRISMA counts with no
  exclusion reason recorded.
- **It will not drop anything silently.** Unparseable lines, out-of-batch ids,
  contradictory duplicate decisions, and studies that got no reply back are all
  counted and reported.

This is a different tool for a different job than the "AI-tools integration"
section further down: that section is about using an LLM to draft or read
prose (search-term expansion, section drafting, synthesis summaries);
`assist.py` is specifically the screening-assist step, and unlike those tasks
it needs no account, API key, or paid subscription -- any free web chatbot
works.

### Full-text screening -- where "included" is actually decided

In the guided path (`python slr.py`), the consolidation menu's **Full-text
screening** step walks you through the merged TA-included set one study at a
time and records `ft_decision` / `ft_reason` into `included_final.csv`. An
exclusion requires a reason, because PRISMA item 16b requires you to cite the
reports you excluded at full text and say why.

This matters more than it sounds. PRISMA 2020 defines the "Studies included in
synthesis" box as studies that survived **full-text eligibility assessment**.
Title/abstract includes are papers you thought looked promising, not papers you
read. The PRISMA `included` count is therefore sourced from `ft_decision ==
include` only: until you record full-text decisions, `included` is 0 and the
tool tells you why rather than quietly reporting your TA-includes as included
studies.

If you prefer, edit the `ft_decision` / `ft_reason` columns of
`included_final.csv` by hand and re-run the step to fold them into the audit
trail.

### Inter-rater agreement (Cohen's kappa)

Two reviewers screen the same records independently, then you compute kappa
**before** reconciling. Kitchenham SS6.3 and Cochrane SS4.6.1 both require
this, and single-reviewer screening is a named validity threat -- the first
question a peer reviewer asks about a review's screening is what the agreement
was.

To do it: copy a phase's `screening.csv`, have the second reviewer fill in
`ta_decision` without seeing the first reviewer's calls, then use the guided
path's **Inter-rater agreement** menu step and point it at both files. You get
Cohen's kappa with its Landis & Koch band, raw percent agreement, a confusion
matrix, and `conflicts_<stage>.csv` listing every disagreement to reconcile.
Say in your methods how you resolved them (discussion, or a third reviewer).

Only records **both** reviewers decided are compared -- that is what kappa is
defined over; records only one of them touched are missing data, not
disagreement. One caveat the tool enforces rather than hides: if both reviewers
used a single identical label (everything included, say), chance agreement is
100%, kappa is mathematically undefined, and reporting it as 1.0 would be a
misreported statistic. In that case you get the raw agreement and an
explanation instead of a number.

### `scripts/download.py` -- resolve and fetch open-access PDFs

```bash
python scripts/download.py --in output/candidates_dedup.csv --mailto you@example.com \
    --outdir pdfs --max-downloads 50
```

Tries, per record, in order: an arXiv direct-link pattern (no network lookup
needed to resolve), then Unpaywall, OpenAlex, Semantic Scholar, and CORE (any
record with a DOI; CORE also falls back to a title search). `--mailto` is
required by Unpaywall's API on every request and used as the polite-pool
contact for OpenAlex. CORE is optional -- pass `--core-api-key` (or set
`CORE_API_KEY` in `.env`, see `.env.example`) to enable it; without a key
that source is skipped. `--sources` overrides the order/set of sources tried
(default `arxiv,unpaywall,openalex,semanticscholar,core`).

Writes `output/download_log.csv` (columns: `id, doi, oa_status, method,
saved_path`); records with no open-access copy are logged as such and
skipped, not force-downloaded. Every record that ends without a saved PDF is
also written to `--report` (default `output/manual_download_needed.csv`,
columns: `id, doi, title, url, tried, reason`) so you know exactly which
PDFs need manual retrieval, and why each automated source failed.

**A real gotcha:** Unpaywall specifically rejects `@example.com`/`@example.org`
placeholder addresses with an HTTP 422 ("please use your own email address").
OpenAlex and Crossref don't mind a placeholder address, so this only surfaces
once you run `download.py` -- use a real, deliverable mailbox. See
`examples/README.md` for the exact error hit during this repo's own
verification run.

**Do not skip the manual verification step after this script runs.**
Automated resolution gets the wrong PDF often enough to matter. Check every
downloaded PDF's title page against the expected title before it enters
extraction.

### `scripts/extract.py` -- build the Stage-4/5 extraction + quality-scoring template

```bash
python scripts/extract.py --in output/screening.csv --out output/extraction.csv \
    --include-col ft_decision
```

Filters `output/screening.csv` down to the rows a human marked `include` in
`--include-col` (default `ft_decision`, the full-text stage; falls back to
`ta_decision` if `ft_decision` is missing or entirely blank, so the script is
also runnable as a dry run right after `screen.py`). `authors` is joined in by
`id` from `--candidates` (default `output/candidates_dedup.csv`), since the
screening sheet itself doesn't carry it. Writes `output/extraction.csv` with
columns `id, title, authors, year, venue, doi, thematic_class, study_type,
contribution, key_findings, rq_mapping, limitations, venue_tier, R, A, T, C,
quality_tier` -- the first six are pre-filled metadata; everything else is the
Stage-4/5 template, left blank for a human reviewer.

`venue_tier` is `T1`/`T2`/`T3` per the README Stage 5 definitions above.
`R`/`A`/`T`/`C` are each rated `Low`/`Some`/`High` concern -- **R**igor,
**A**rtifact availability, **T**hreat-model completeness, and **C**urrency
(see Stage 5 for the full rubric) -- and `quality_tier` is the aggregate
`A`/`B`/`C` letter grade a reviewer derives from them via a published
aggregation formula, decided before scoring begins. `extraction_reviewer`,
`extraction_date`, and `notes` are always appended last, for piloting/audit
traceability (see Stage 5's "Field-driven critical appraisal" above).

```bash
# Append a field-appropriate appraisal instrument's verbatim domains
python scripts/extract.py --in output/screening.csv --instruments dyba_dingsoyr

# Pilot: sample 10 studies into a second sheet for independent double extraction
python scripts/extract.py --in output/screening.csv --pilot 10

# Compare two completed extraction sheets -- Cohen's kappa + conflicts per column
python scripts/extract.py --compare-a extraction.csv --compare-b extraction_pilot.csv
```

### `scripts/snowball.py` -- real citation snowballing (backward + forward)

```bash
python scripts/snowball.py --seeds runs/<id>/final_dedup.csv --mailto you@example.com \
    --direction both --max-per-seed 50 --out output/candidates_snowball.csv
```

This is Wohlin (2014) citation snowballing -- distinct from `slr.py`'s
between-phase "query expansion," which mines keywords from included titles and
was previously (incorrectly) also called snowballing. This script walks the
actual citation graph via OpenAlex: **backward**, reading each seed study's
reference list; **forward**, finding papers that cite each seed. Point
`--seeds` at `included_final.csv` or `final_dedup.csv`; results are written in
the same `Candidate` shape as `search.py`'s output, with a `snowball_from`
column recording which seed(s) surfaced each record.

Run the output through `dedup.py` and `screen.py` exactly like a database
search's hits -- a citation hit is an identification candidate, not an
automatic include. In the guided path, use the consolidation menu's **Citation
snowballing** action (which does this for you, into `runs/<id>/snowball/`) and
then **Merge citation-snowball results into included set** once you've screened
them, which namespaces their ids (`sb_N`) and tags them `phase="snowball"` so
they can never collide with a database-search id and stay distinguishable in
PRISMA reporting as their own identification method.

A title-search fallback (used when a seed has no DOI) is guarded by a
`rapidfuzz` similarity check against the seed's own title -- OpenAlex's title
search always returns its single best hit, even for a title with no genuine
match, so an ungated fallback would silently resolve an unrelated paper and
pull in its entire citation subgraph.

### `scripts/verify_citations.py` -- the fabricated-citation guard

```bash
# Check every DOI in a CSV against what it actually resolves to on Crossref
python scripts/verify_citations.py --csv output/candidates_dedup.csv \
    --doi-col doi --title-col title --limit 10 \
    --out output/citation_verification.csv

# Check a single ad hoc citation
python scripts/verify_citations.py --doi 10.1002/ett.4150 \
    --claimed-title "Network intrusion detection system: A systematic study of machine learning and deep learning approaches"
```

Resolves each DOI against `api.crossref.org/works/<doi>` and fuzzy-matches the
claimed title against what the DOI actually returns (threshold 85/100). Exits
0 only if every checked citation passes -- wire it into a pre-submission check.
Run this against **every** citation before it enters a manuscript, not just
ones that look suspicious: a fabricated citation can sit inside an otherwise
legitimate-looking paper that has already passed title-level screening.

### Figures (after screening + scoring)

`scripts/figures.py` generates the Stage 7 reporting figures directly from
the pipeline's own CSVs -- no manual diagramming tool needed. Every figure is
written as both a 300 dpi PNG (for a manuscript/slide) and a vector PDF (for
print/typesetting).

```bash
python scripts/figures.py \
    --screening output/screening.csv \
    --dedup output/candidates_dedup.csv \
    --candidates output/candidates.csv \
    --quality output/extraction.csv \
    --outdir figures
```

`--screening`/`--dedup`/`--candidates` each read exactly ONE file, and
`--screening`'s `ft_decision` is read from that same file -- correct for a
single-phase review (or the worked example above, where one sheet carries
both `ta_decision` and `ft_decision`). **A guided-TUI run with more than one
search phase (`n_phases > 1`) needs `--run-dir` instead**, or `identified`/
`screened`/`excluded_ta` silently undercount (only one phase's files are
read) and `excluded_ft`/`included` silently read as 0 (`ft_decision` lives in
the run's `included_final.csv`, never in any single phase's `screening.csv`):

```bash
python scripts/figures.py --run-dir runs/<run-id> \
    --quality runs/<run-id>/extraction.csv --outdir figures
```

This reads every `phase_N/` subdirectory under `--run-dir` plus its
`included_final.csv` and sums across all of them -- the same logic the
guided TUI's "Generate PRISMA + tier figures" action already uses
(`srp/prisma.py`, one shared implementation for both entry points). Either
way, an unbalanced result (e.g. `included` exceeding `assessed_ft`) prints a
`PRISMA check:` warning to stderr rather than silently shipping a
self-contradicting diagram.

Four figures land in `--outdir`:

1. **`prisma_flow.png`/`.pdf`** -- the simplified PRISMA 2020 flow diagram
   (Identification -> Screening -> Eligibility -> Included), drawn with
   matplotlib patches and arrows. Counts are derived automatically: records
   identified from `--candidates`; duplicates removed from `--dedup`'s
   `duplicate_of` column; records screened, and excluded/assessed/included
   counts, from `--screening`'s `ta_decision`/`ft_decision` columns
   (full-text exclusion reasons are summarized from `ft_reason`, if
   present). Pass `--identified`/`--duplicates-removed`/`--screened`/
   `--excluded-ta`/`--assessed-ft`/`--excluded-ft`/`--included` to override
   any single box with a final, manually-decided number -- e.g. once the
   review is complete and the published diagram should match the exact
   numbers in the manuscript. Every count actually used (derived or
   overridden) is printed to stdout.
2. **`prisma_2020.png`/`.pdf`** -- the **official PRISMA 2020 three-column
   template**: previous studies | new studies via databases & registers |
   new studies via other methods. Note that the later phases are additional
   *database* searches with an expanded query, not identification by another
   method -- if you populate the "other methods" column, it should be from a
   genuine non-database pass (citation snowballing, contacting authors, grey
   literature), which you do by hand. Every number the pipeline actually
   knows -- identified, duplicates removed, screened, excluded at TA,
   assessed at full text, included -- is filled in automatically, exactly
   like `prisma_flow.png`. Boxes the pipeline has no way to know
   (registers, automation tools used, a previous-version count, and most of
   the "other methods" column) are left as `(n = )`, exactly like the
   fillable official template, for the reviewer to complete by hand. See
   `examples/figures/prisma_2020.png` for a rendered example.
3. **`quality_tiers.png`/`.pdf`** -- a bar chart of the `A`/`B`/`C`
   `quality_tier` counts from `--quality` (default `output/extraction.csv`).
4. **`venue_tiers.png`/`.pdf`** -- a bar chart of the `T1`/`T2`/`T3`
   `venue_tier` counts from the same file.

Both tier charts render a clear "no data yet" placeholder instead of crashing
if their column is missing or entirely blank -- expected on a first run,
before a human has filled in `extraction.csv`. See `examples/figures/` for
what all four look like once the extraction/quality sheet is actually filled
in (rendered from `examples/screening_decided_sample.csv` and
`examples/quality_scored_sample.csv`; commands in `examples/README.md`).

### `scripts/export.py` -- export references for your citation manager

```bash
python scripts/export.py --in output/extraction.csv --out-bib references.bib \
    --out-ris references.ris --included-only
```

Turns the final included-studies set into `references.bib` (BibTeX, for
LaTeX/Overleaf) and `references.ris` (RIS, for Zotero/Mendeley/EndNote).
`--in` accepts `output/extraction.csv` (has `authors`) or, before extraction
has run, `output/screening.csv` (no `authors` column, so exported entries
will be missing it). `--included-only` filters to rows marked `include` in
`ft_decision` (falling back to `ta_decision` if `ft_decision` isn't present
or is entirely blank). `--formats` (default `bibtex,ris`) restricts output to
one format if you only need one.

---

## The `srp/` library (config, state, provenance, export)

`slr.py` and `scripts/assist.py`/`scripts/export.py` are built on a small
importable library under `srp/`, rather than reimplementing run bookkeeping
per script:

- **`srp/config.py`** -- `ReviewConfig`: the review's settings (topic,
  keywords/keyword blocks, year range, sources, thresholds, reviewer name,
  which chatbot is doing AI-assist, research field and chosen appraisal
  instruments) as one JSON-serializable dataclass, saved to and loaded from
  `runs/<run-id>/config.json`; secrets are hydrated from the environment and
  never persisted to that file.
- **`srp/env.py`** -- loads `.env` into the process environment (flag >
  exported env var > `.env` precedence; see "Sources and their optional API
  keys" above).
- **`srp/normalize.py`** -- the single definition of "same DOI" / "same title"
  (`normalize_doi()`, `normalize_title()`, `record_key()`), shared by
  `scripts/dedup.py` and `srp/state.py` so the two can never silently disagree
  on what counts as a duplicate.
- **`srp/state.py`** -- `RunState`: a per-review workspace under
  `runs/<run-id>/`, with a `state.json` of per-phase/per-stage checkpoints
  (what `slr.py`'s "Resume" option fast-forwards past) and an append-only
  `decisions.jsonl` decision log. That log doubles as a cross-phase
  "already-judged" cache keyed by normalized DOI or title
  (`record_key()`) -- so when phase 2's snowball search re-surfaces a record
  already screened in phase 1, it's silently skipped rather than re-asked
  about.
- **`srp/decisions.py`** -- the single ingestion path for a proposed decision
  (from `scripts/assist.py` or the guided AI-assist step) into `ta_decision`/
  `ft_decision`, and the shared "maybe proceeds like include" policy
  (`pick_progressed()`) that `extract.py`, `export.py`, and the PRISMA count
  derivation all read from, so they can't silently diverge on which rows count
  as included.
- **`srp/agreement.py`** -- Cohen's kappa, its Landis & Koch band, and the
  confusion matrix behind [Inter-rater agreement](#inter-rater-agreement-cohens-kappa)
  and `extract.py --compare-a/--compare-b`.
- **`srp/appraisal.py`** -- the field -> critical-appraisal-instrument
  registry behind [Field-driven critical appraisal](#field-driven-critical-appraisal-prisma-item-11):
  `instrument_columns()` for primary-study extraction-sheet columns,
  `render_review_self_appraisal()` for the review-level checklist file, and
  `compose_appraisal_disclosure()` for the `PROVENANCE.md` paragraph.
- **`srp/quality_tier.py`** -- `compute_quality_tier()`, the mechanical R/A/T/C
  aggregation formula from Stage 5 above. Used by the consolidation menu's
  "Review/correct an extraction record" action to auto-fill `quality_tier`
  whenever R/A/T/C are all set, and to detect when a hand-set `quality_tier`
  disagrees with the formula (logged as a manual override, not silently
  overwritten or silently accepted).
- **`srp/prisma.py`** -- `derive_prisma_counts_for_run()`, the multi-phase
  PRISMA-count derivation (sum identified/screened/excluded-ta across every
  phase, read excluded-ft/included from the run's `included_final.csv`) and
  `prisma_residuals()`'s balance check. The single implementation behind both
  `slr.py`'s guided "Generate PRISMA + tier figures" action and
  `scripts/figures.py --run-dir` (see "Figures" above) -- extracted after
  the standalone script's own single-sheet derivation was found to silently
  zero `excluded_ft`/`included` on a real multi-phase run, since it has no
  way to know `ft_decision` lives in a different file.
- **`srp/methods_report.py`** -- drafts the search-methods paragraph and its
  supplementary tables from the recorded search-strategy log and PRISMA
  counts (the consolidation menu's "Draft methods paragraph" action).
- **`srp/provenance.py`** -- `Provenance`: an always-on run manifest. Every
  search query, its date, source, and hit count; every dedup pass; every
  AI-assisted screening batch and its parsed counts; every human review-gate
  override -- all logged as one JSON line per event to
  `runs/<run-id>/provenance.jsonl`, then rendered to a human-readable
  `runs/<run-id>/PROVENANCE.md` (via the consolidation menu's "Write
  provenance report", or `Provenance.render_markdown()` directly). This is
  your PRISMA methods audit trail -- it includes an explicit
  **AI-assistance disclosure** that names the specific chatbot used for
  screening (from the `assist_tool_name` you gave the setup wizard), or
  states plainly that no AI assistance was used if you left it blank.
- **`srp/export.py`** -- the BibTeX/RIS field-formatting logic shared by
  `scripts/export.py` and `slr.py`'s consolidation-menu export step.
- **`srp/llm_assist.py`** -- the manual-paste prompt builder and reply
  parser behind `scripts/assist.py` (see above) and `slr.py`'s AI-assist
  phase step.

None of this needs to be imported directly for the manual `scripts/`
workflow -- `scripts/export.py` always works CSV-only, and `scripts/assist.py`
only touches `srp/state.py`'s cross-phase cache if you pass it `--run
<run-dir>`; without that flag it also works CSV-only.

---

## AI-tools integration

This section is about using an LLM to help you **write and read** -- expand
search terms, draft prose, summarize synthesis findings. That's a different
job from `scripts/assist.py`'s manual-paste **screening** assist (see
"Running the pipeline" above): screening-assist needs no account or key and
writes its output straight into `screening.csv` as a proposed decision for
the human review gate; the tasks below are open-ended drafting/reading help
that stays outside the pipeline's CSVs entirely.

### Where AI fits in the pipeline

| Stage | AI role | Human role |
|---|---|---|
| Search-term generation | Expand/suggest synonym blocks from an initial PICOC | Approve every term before it enters a Boolean string; run the actual queries yourself |
| Title/abstract screening | High-volume first-pass triage against a strict schema (via `scripts/assist.py`, manual-paste, no API key) | Review every AI decision, especially every "exclude" -- false exclusions are invisible if unchecked; confirm at the review gate |
| Data extraction | Populate the fixed schema (Stage 4) from a PDF's text | Spot-check extracted quantitative findings against the source PDF directly, not just plausibility-check the prose |
| Cross-study synthesis | Draft convergence/conflict summaries from an evidence table you provide | Verify every claimed convergence/conflict against the actual studies; AI tends to over-smooth disagreements into false consensus |
| Section drafting | Draft prose from an evidence table + outline | Full edit pass; run through a readability/humanizing pass (below) |
| Readability/humanizing | Rewrite for plain, non-AI-sounding academic prose | Confirm no meaning was altered in the rewrite |
| Citation verification | Never -- use `scripts/verify_citations.py`, not an LLM | Run the DOI-resolution check yourself; treat any mismatch as a hard stop |

### Tool picks by task

| Task | Tools |
|---|---|
| Discovery / search-term expansion / related-work mapping | [Elicit](https://elicit.com), [Consensus](https://consensus.app), [Semantic Scholar](https://www.semanticscholar.org), [Research Rabbit](https://www.researchrabbit.ai), [Connected Papers](https://www.connectedpapers.com), [Undermind](https://undermind.ai), [Perplexity](https://www.perplexity.ai), [Scite](https://scite.ai) |
| Screening / extraction / synthesis drafting | ChatGPT (GPT-4o / o-series), Claude, Google Gemini |
| Language / readability polish | [Grammarly](https://www.grammarly.com), [Writefull](https://www.writefull.com), [Paperpal](https://paperpal.com) |
| Reference management | [Zotero](https://www.zotero.org), with the [Better BibTeX](https://retorque.re/zotero-better-bibtex/) plugin for stable citation keys and DOI-based dedup |

A strong reasoning model is worth it for synthesis, extraction, and section
drafting -- these tasks require holding an evidence table's structure and
cross-referencing many studies at once; a weaker model will quietly drop or
misattribute findings. A cheaper/faster model is fine for high-volume
title/abstract triage across hundreds of records, *as long as* every exclusion
remains reviewable and a sample of both included and excluded decisions gets
human-checked -- not just the included ones, since that's where false
exclusions hide.

### Prompt templates

**(a) Generating/expanding Boolean search terms**

```
You are helping expand a systematic literature review's search terms. The review's PICOC
scoping is:
  Population: <paste>
  Intervention: <paste>
  Comparison: <paste>
  Outcome: <paste>
  Context: <paste>

I have this initial thematic block: [<term1>, <term2>, <term3>]

Propose up to 8 additional synonyms/near-synonyms/common alternate phrasings that authors in
this field use for the SAME concept (not adjacent concepts). For each, state which of the
seed terms it is a synonym/variant of, and flag any term that is also a common false-positive
source in an unrelated field. Do not propose terms that broaden the concept -- only lexical
variants of the same concept. Output as a table: term | variant-of | false-positive-risk (Y/N,
reason).
```

Human step: review every proposed term against the PICOC before it enters any
Boolean string; reject anything that's actually a scope expansion disguised as
a synonym.

**(b) Batch title/abstract screening**

```
You are screening candidate records for a systematic literature review at the title/abstract
stage. Apply ONLY the criteria below -- do not use outside judgment about topic importance.

INCLUDE if: <paste inclusion criteria verbatim>
EXCLUDE if: <paste exclusion criteria verbatim>
MAYBE if: the abstract does not give enough information to decide against the criteria above
  (do not guess -- use MAYBE, a human will resolve it at full-text stage).

Output STRICT JSON, one object per record, matching this schema exactly:
{
  "id": <int>,
  "decision": "include" | "exclude" | "maybe",
  "reason": "<one sentence citing which specific criterion drove the decision>"
}
Return a JSON array of these objects, nothing else -- no prose before or after. The "id" must
be copied EXACTLY from each record below -- do not renumber, re-sequence, add, omit, merge,
or reorder the records. Record ids are not necessarily sequential (e.g. 12, 47, 61); answering
positionally (treating the first record as id 1) silently misattributes the decision to a
different, unrelated record.

Do not include a decision for any record where the abstract is missing or under 20 words --
mark those "maybe" with reason "insufficient abstract" instead of guessing from the title alone.

Records:
<id, title, abstract, venue, year for each candidate>
```

Human step: spot-check a random sample of both "include" and "exclude"
decisions (not just borderline "maybe" ones); a systematic false-exclusion
pattern (e.g., excluding everything that doesn't use exact terminology) is the
main failure mode to watch for.

**(c) Structured data extraction**

```
Extract the following fields from the attached paper into the exact schema below. Use only
information stated in the paper -- do not infer or estimate a number that is not explicitly
given. If a field is not addressed in the paper, write "not reported," do not leave it blank
and do not guess.

Schema: thematic_class, venue, venue_tier (1/2/3 per: <paste your tier definitions>),
study_type, contribution, key_findings (verbatim numbers, with the dataset/
split/baseline context needed to interpret them), rq_mapping (which of RQ1-RQ4 this
evidences), limitations (per the paper's own stated limitations, not your assessment),
quality_tier (do not fill this in -- quality scoring is a separate human pass).

Paper: <attach or paste full text>
```

Human step: cross-check every `key_findings` entry against the source PDF
directly -- this is the field most likely to contain a subtly wrong number
(right order of magnitude, wrong dataset split, wrong baseline) that reads as
plausible.

**(d) Cross-study consistency synthesis**

```
I have N studies bearing on this claim: "<state the specific claim, e.g., 'transformer-based
NIDS architectures outperform tree-based baselines on recall for rare/minority attack
classes'>"

For each study below, I've given: study id, the specific finding relevant to this claim,
and its measurement context (dataset, split, evaluation protocol).

<list studies with finding + context>

Assess: do these studies converge or conflict on DIRECTION (not magnitude)? If magnitudes
differ substantially, explain the likely methodological driver (different dataset/split/
evaluation protocol) rather than treating the difference as a real disagreement. Explicitly
flag if any pair of studies conflicts even on direction -- that is a genuine disagreement,
not a measurement-context artifact, and needs separate discussion. If you cannot identify a
plausible methodological driver from the context given, say so explicitly rather than
proposing one speculatively -- an invented-but-plausible-sounding explanation is worse than
admitting the discrepancy is unexplained.

Do not average or pool the numbers. State whether meta-analysis would even be appropriate
here, and why or why not.
```

Human step: verify the "methodological driver" explanation against the actual
papers before it goes in the manuscript -- an LLM will readily generate a
plausible-sounding explanation for a discrepancy that isn't the real one.

**(e) Drafting a section from an evidence table**

```
Draft the RQ<N> synthesis section from the evidence table below. Requirements:
- Organize by sub-theme within the RQ, not study-by-study.
- Every claim must cite the specific study id(s) that support it.
- State explicit convergence/conflict per the consistency-assessment output above.
- Do not introduce any claim, number, or study characterization not present in the evidence
  table -- if the table doesn't cover something, don't fill the gap from general knowledge.
- Flag, in a trailing [NEEDS HUMAN CHECK] note, any sentence where you are not fully certain
  the evidence table supports the specific phrasing used.

Evidence table: <paste extraction-matrix rows relevant to this RQ>
```

Human step: resolve every `[NEEDS HUMAN CHECK]` flag before this draft moves
forward; treat an unflagged draft with the same scrutiny anyway.

**(f) Readability/humanizing pass**

```
Rewrite the following academic prose to read as plainly and directly as a careful human
author would write it. Remove: AI-vocabulary tells (delve, leverage, underscore, pivotal,
seamless, crucial, robust used as filler, "it is important to note that"), formulaic
rule-of-three constructions, uniform sentence-length rhythm, and negative-parallelism
padding ("not only... but also," "it's not just X, it's Y"). Preserve every factual claim,
citation, number, and hedge/qualifier exactly -- this is a style edit, not a content edit.
If a sentence's meaning would change by simplifying it, leave it as is and flag it instead
of guessing.

Text: <paste section draft>
```

Human step: diff the rewrite against the original for any dropped hedge,
qualifier, or citation -- a style pass that silently drops a "may" or a
citation is a content bug wearing a style-edit disguise.

**(g) Citation verification**

Do not use a prompt for this step -- use `scripts/verify_citations.py`. An LLM
asked to verify a citation is itself unreliable for the exact failure mode
you're trying to catch (see the integrity guardrails below).

### Integrity guardrails

These are non-negotiable, not best-effort suggestions.

1. **Never trust an LLM-produced (or human-produced) citation without
   independently verifying the DOI resolves to the claimed title and
   authors.** A citation that cites a completely unrelated paper as evidence
   is a real, documented failure mode in systematic reviews -- not a
   hypothetical. Whether it originates from a human author cutting corners or
   an LLM hallucinating a reference, the mitigation is identical: resolve
   every DOI you cite against a real registry (`scripts/verify_citations.py`,
   which checks Crossref -- a bare arXiv identifier with no DOI isn't
   resolvable by it) and confirm title and authors match, before it enters
   your corpus or your own manuscript.
2. **Keep a human in the loop for every single inclusion/exclusion decision.**
   AI-assisted triage (`scripts/assist.py` included) can propose a decision;
   it cannot be the decision. This applies at both the title/abstract and
   full-text stages -- `slr.py`'s human review gate exists specifically to
   enforce this in the guided path. Both the review gate and full-text
   screening mechanically refuse to mark themselves complete -- and the
   pipeline's resume pointer refuses to advance past them -- while any record
   still has no decision, with no override; the phase cannot silently move on
   with records left unaccounted for.
3. **Verify every downloaded PDF's title page against its expected title
   before extraction.** Automated resolution (`scripts/download.py`) can fetch the
   wrong file -- a similarly-titled paper, or a landing page mislabeled as a
   PDF. Check the actual PDF, every time, before it enters your extraction
   matrix.
4. **Log every AI-assisted decision.** If an LLM proposed a screening
   decision, an extracted field, or a drafted paragraph, record that it did --
   a `decision_source` or `ai_assisted` column in your screening/extraction
   spreadsheets costs nothing to add and is exactly what the next guardrail
   needs.
5. **Track AI-usage disclosure requirements per venue.** Many venues now
   require an explicit AI-usage disclosure statement (what tools were used,
   for what stages, with what human oversight). Keep a running record of where
   AI assisted throughout the pipeline -- not reconstructed from memory at
   submission time -- so the disclosure statement can be written accurately.

---

## Integrity & reproducibility checklist

- **Provenance-tag every reported number.** Every quantitative claim in your
  synthesis should be traceable to one of: **Measured** (you or a cited study
  directly measured it), **Derived** (computed from measured numbers via a
  stated formula), **Modeled** (from a simulation, not a real system), or
  **Estimated** (an approximation with named uncertainty). Don't let a Modeled
  or Estimated number read as if it were Measured.
- **Keep a complete screening log**, from the first record screened -- every
  title/abstract and full-text decision, its reason, and its reviewer.
- **Archive the exact queries and their execution dates.** Save the verbatim
  Boolean strings per database, the filters applied, and the date each was
  run -- database indices change over time.
- **Pin dataset/tool versions.** If your review depends on a dataset snapshot
  (a specific OpenAlex/Crossref export, a specific database export CSV), keep
  the raw export file, not just a derived candidates list.
- **No-fabrication rules, stated plainly:**
  - Never state a number you have not verified against its source.
  - Never let an LLM-drafted paragraph's citations enter a manuscript without
    independent DOI verification (`scripts/verify_citations.py`).
  - Never silently drop a discrepancy between two accounting views of the same
    total (e.g., per-round included counts vs. final corpus count) --
    reconcile and state the reconciliation explicitly.
  - Never present a Modeled or simulation-only number as if it were an
    empirical measurement.

## End-to-end checklist

- [ ] PICOC scoping written down, RQs derived from it
- [ ] Protocol pre-registered (OSF Registries) before the first query is run
- [ ] Inclusion/exclusion criteria written down, including a source-quality floor
- [ ] Boolean query strings drafted for every accessible database, per that
      database's actual syntax
- [ ] Every query's exact string, filters, and execution date archived
- [ ] Candidates pulled via `scripts/search.py`, unified into one `candidates.csv`
- [ ] De-duplicated via `scripts/dedup.py`: DOI exact match, then fuzzy title match
- [ ] Two independent reviewers screen title/abstract; Cohen's kappa computed
      before reconciling
- [ ] Full-text eligibility assessed for everything that survives; every
      downloaded PDF's title page checked against its expected title
- [ ] Complete screening log kept from the first record, not just included ones
- [ ] Fixed-schema extraction for every included study (`scripts/extract.py`
      builds the template); quantitative findings copied verbatim with enough
      context to interpret later
- [ ] Quality-rubric scoring (R/A/T/C) with a published aggregation formula;
      per-dimension scores released, not just the aggregate tier
- [ ] A field-appropriate, externally citable appraisal instrument selected
      (`srp/appraisal.py`) alongside R/A/T/C, matching the review's actual field
- [ ] If a review-level self-check instrument applies (AMSTAR 2, ROBIS, DARE,
      MECCIR), its checklist filled in against the finished manuscript
      (consolidation menu's "Write review self-appraisal checklist")
- [ ] Certainty of evidence assessed (GRADE / GRADE-CERQual) or its absence
      explicitly justified for the field
- [ ] Venue tier and quality tier reported as separate axes
- [ ] Citation snowballing (backward + forward, `scripts/snowball.py`) run and
      screened as its own identification method -- distinct from database
      search and from title-term query expansion -- if the protocol calls for it
- [ ] Narrative/thematic synthesis by RQ, with explicit convergence/conflict
      assessment; meta-analysis only where studies are genuinely comparable
- [ ] Every citation DOI-verified via `scripts/verify_citations.py` before it
      enters the manuscript
- [ ] PRISMA flow diagram(s) (`scripts/figures.py`) with a reasoned (not bare)
      excluded-count breakdown
- [ ] PRISMA 2020 checklist graded honestly, gaps disclosed rather than hidden
- [ ] Search-methods paragraph drafted from the pipeline's own recorded search
      strategy (consolidation menu's "Draft methods paragraph"), not retyped
      by hand, so truncated sources carry their caveat automatically
- [ ] Grey-literature inclusion/exclusion and reporting-bias-assessment
      decisions stated explicitly (PRISMA items 14, 21), not left silent
- [ ] Data-availability supplement: search strings, screening log, extraction
      matrix, and full quality-rubric grid all released
- [ ] AI-usage log kept throughout; disclosure statement written from that
      log, not from memory, at submission time

---

## License

This repository's licensing is split by content type:

- **Code** (`slr.py`, `srp/`, and every script under `scripts/` -- `search.py`,
  `dedup.py`, `screen.py`, `assist.py`, `download.py`, `verify_citations.py`,
  `extract.py`, `figures.py`, `export.py`) is licensed under the **MIT
  License** -- see [`LICENSE`](LICENSE).
- **Documentation** (this README and `examples/README.md`) is licensed under
  **Creative Commons Attribution 4.0 International (CC-BY-4.0)** -- see
  [`LICENSE-docs`](LICENSE-docs). You're free to reuse, adapt, and redistribute
  the methodology write-up for any purpose, including commercially, as long as
  you give appropriate credit.

Copyright (c) 2026 Md Nayeem Hossain.
