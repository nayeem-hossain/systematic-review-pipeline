# systematic-review-pipeline

A reusable, dependency-light toolkit for running a systematic literature review
(SLR) end to end: protocol scoping, multi-source search, deduplication,
screening, open-access PDF retrieval, and deterministic citation verification.
It pairs a documented 7-stage methodology (PRISMA 2020 + Kitchenham & Charters
2007) with five small, standalone Python scripts that implement the
mechanical parts of that methodology -- so a review stays reproducible instead
of living in a pile of manually-edited spreadsheets.

The worked example used throughout this README is **machine-learning / AI-based
intrusion detection systems (ML-IDS)** -- network intrusion detection, anomaly
detection, and related applications of deep learning, ensembles, transformers,
GANs, and federated learning to network security. Swap the search terms, the
PICOC table, and the inclusion/exclusion criteria for your own topic; the
pipeline and the method don't change.

## Quick start

```bash
git clone <this-repo-url>
cd systematic-review-pipeline

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                 # fill in a real MAILTO address

python search.py \
    --query '"intrusion detection" AND "machine learning"' \
    --year-from 2020 --year-to 2026 \
    --mailto you@example.com \
    --max-per-source 40 \
    --out output/candidates.csv

python dedup.py  --in output/candidates.csv       --out output/candidates_dedup.csv
python screen.py --in output/candidates_dedup.csv --out output/screening.csv
python download.py --in output/candidates_dedup.csv --mailto you@example.com --outdir pdfs
python verify_citations.py --csv output/candidates_dedup.csv --limit 10

# Once a human has filled in ta_decision/ft_decision in screening.csv:
python extract.py --in output/screening.csv --out output/extraction.csv

# Once a human has filled in venue_tier/R/A/T/C/quality_tier in extraction.csv:
python figures.py --quality output/extraction.csv --outdir figures
```

This repository ships with a **live demonstration run** already committed:
`output/` holds the real CSVs produced by an actual ML-IDS search (candidates,
dedup, screening, download log, citation verification), and `pdfs/` holds the
open-access PDFs that `download.py` fetched. `examples/` additionally keeps a
small curated sample of each output -- including a fully-decided screening
sheet, a filled-in extraction/quality-scoring sheet, and the three figures
`figures.py` renders from them (`examples/figures/`) -- so you can see the CSV
and figure shapes at a glance; see `examples/README.md` for the exact commands
used and one real API gotcha hit along the way. If you fork this for your own
review, delete `output/`, `pdfs/`, and `figures/` and regenerate them with the
scripts.

### What you change vs. what you don't

Every script in this repo follows the same convention, stated right after its
module docstring: the **command-line flags are what you're meant to change**
day to day (search terms, thresholds, paths, `--mailto`) -- run
`python <script>.py --help` to see them. The code *below* that point is marked
in two ways: `# --- API internals ---` over anything that builds a request URL
or parses a response from an external API (breaks only if that API's contract
changes), and `# --- core logic ---` over the dedup match loop, the PRISMA
count derivation, and the plotting geometry (safe to leave alone unless you're
deliberately extending the tool). You should not need to edit anything below
either marker for a normal review.

## Repo structure

```
systematic-review-pipeline/
├── README.md              # this file -- methodology + pipeline usage
├── LICENSE                # MIT -- applies to the *.py scripts
├── LICENSE-docs            # CC-BY-4.0 -- applies to README.md / examples/README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── search.py               # query OpenAlex, Semantic Scholar, Crossref, arXiv
├── dedup.py                 # DOI-exact then fuzzy-title deduplication
├── screen.py                 # build the title/abstract + full-text screening sheet
├── download.py                 # resolve + fetch open-access PDFs (Unpaywall, arXiv)
├── verify_citations.py          # fabricated-citation guard: DOI -> Crossref -> diff
├── extract.py                     # build the Stage-4/5 extraction + quality-scoring template
├── figures.py                      # PRISMA flow diagram + quality-/venue-tier charts
├── output/                       # committed demo run: real search / dedup / screen / verify CSVs
├── pdfs/                         # committed demo run: open-access PDFs download.py fetched
├── figures/                       # your own generated figures (regenerate with figures.py)
└── examples/                     # small curated sample of the outputs above
    ├── README.md
    ├── candidates_sample.csv
    ├── candidates_dedup_sample.csv
    ├── screening_sample.csv
    ├── screening_decided_sample.csv    # screening_sample.csv with realistic decisions filled in
    ├── quality_scored_sample.csv       # a filled-in extraction/quality-scoring sheet
    ├── download_log_sample.csv
    ├── citation_verification_sample.csv
    └── figures/                        # prisma_flow / quality_tiers / venue_tiers, .png + .pdf
```

## The end-to-end workflow

```
Protocol  ->  Search  ->  Screen  ->  Extract  ->  Quality-assess  ->  Synthesize  ->  Report
 (PICOC,       (multi-    (title/     (per-study    (R/A/T/C           (narrative      (PRISMA
  RQs,          round      abstract    schema,       rubric,            by RQ,          flow +
  criteria,     Boolean    then       verbatim       Low/Some/          consistency     checklist,
  pre-reg)      queries)   full-text)  numbers)      High per dim)      assessment)     data avail.)
```

`search.py` -> `dedup.py` -> `screen.py` cover Search and the mechanical half
of Screening; the decision columns in `screen.py`'s output are intentionally
left blank for a human. `download.py` retrieves full text for the full-text
screening and extraction stages. `extract.py` builds the Extract +
Quality-assess template (`R`/`A`/`T`/`C` rubric columns, again left blank for
a human). `verify_citations.py` runs continuously, against every citation you
plan to use, from extraction through final manuscript. `figures.py` covers
Report's PRISMA flow diagram and the quality-/venue-tier distribution charts.

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
  Google Scholar, backward/forward citation chasing per Wohlin 2014) -- lower
  barrier, catches preprints Boolean-indexed databases miss, but is less
  systematic and harder for someone else to reproduce exactly.

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

| | IEEE Xplore | ACM Digital Library | Scopus |
|---|---|---|---|
| Field qualifier | Repeat `"Document Title":` before **every** term | Wrap **every** term in its own `[Title: "..."]` bracket | One `TITLE-ABS-KEY(...)` wrapping the *entire* boolean expression |
| Default search field | None -- field must be explicit per term | None -- field must be explicit per term | `TITLE-ABS-KEY` covers title + abstract + author keywords, not title alone |
| Year filter | UI facet or separate filter parameter | UI facet (Publication Date range) | Inline operators: `PUBYEAR > 2017 AND PUBYEAR < 2027` |
| Phrase matching | Double quotes | Double quotes | Double quotes |
| Boolean operators | `AND`/`OR`/`NOT`, conventionally capitalized | `AND`/`OR`/`NOT` | `AND`/`OR`/`AND NOT`, case-insensitive |

Draft each database's string separately from the same thematic block list --
don't try to write one universal string and hope it parses everywhere. Also
decide up front whether you're searching Title-only or Title-Abstract-Keyword:
a database that defaults to a broader field (Scopus's `TITLE-ABS-KEY` vs. IEEE
Xplore/ACM DL's Title-only) will structurally return more records for the same
thematic blocks -- expect and disclose that yield asymmetry rather than treat
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
sources). This is exactly what `dedup.py` automates (see "Running the pipeline" below).

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
2. **A dedicated dedup tool, not manual eyeballing.** `dedup.py` (exact DOI
   match, then `rapidfuzz` token-set-ratio fuzzy title match) is deterministic
   and re-runnable, unlike a human scanning a spreadsheet.
3. **A documented screening log from the first record onward.** Every
   screened record gets an explicit decision + reason at both stages, even
   for the overwhelming majority you exclude in seconds -- this is what makes
   a PRISMA flow diagram auditable rather than asserted.
4. **Verify every downloaded PDF's title page against its expected title
   before it enters extraction.** Automated OA resolution (`download.py`) can
   fetch the wrong PDF -- a similarly-titled paper, a landing page mislabeled
   as a PDF -- often enough to matter. Check the actual file every time.

---

### Stage 4 -- Data extraction

For every study that survives full-text screening, extract a **fixed schema**
of fields, not free-form notes. Fixed fields are what make cross-study
synthesis (Stage 6) and quality scoring (Stage 5) tractable at scale.

**Extraction CSV header:**

```csv
id,title,authors,year,venue,venue_tier,doi_or_url,study_type,thematic_classification,rq_mapping,core_contribution,quantitative_findings,limitations,quality_tier_provisional,extraction_reviewer,extraction_date,notes
```

Column notes:

- `venue_tier`: numeric 1/2/3, see Stage 5 -- keep it separate from
  `quality_tier_provisional`; they are different axes.
- `quantitative_findings`: copy numbers verbatim from the source, with enough
  context (dataset, split, baseline, hardware) to be usable later without
  re-reading the paper. Do not convert units at extraction time -- do
  conversions at synthesis time, with the original preserved alongside.
- `rq_mapping`: which research question(s) this study evidences; a study can
  map to more than one RQ (semicolon-separated).
- One row per study. If a study is later found to be a duplicate of another
  (e.g., a preprint and its camera-ready), merge into one row and note the
  merge in `notes` -- don't keep both as separate synthesis inputs.

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
ratings can't reproduce the same letter tier. Example rule:

```
L = 2 points, S = 1 point, H = 0 points, per dimension (4 dimensions -> 0-8 raw points)

7-8 points -> A       5 points -> B+       3 points -> B-
6 points   -> A-      4 points -> B        0-2 points -> C+/C (split by reviewer
                                                             judgment on which
                                                             dimension(s) drove the
                                                             low score)
```

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

Dependency-light: `requests`, `pandas`, `rapidfuzz`, `matplotlib`. Requires Python 3.9+.

```bash
pip install -r requirements.txt
```

### `search.py` -- query four scholarly APIs, write `candidates.csv`

```bash
python search.py \
    --query '"intrusion detection" AND "machine learning"' \
    --year-from 2020 --year-to 2026 \
    --mailto you@example.com \
    --max-per-source 40 \
    --out output/candidates.csv
```

Queries OpenAlex, Semantic Scholar, Crossref, and arXiv; writes one row per
candidate to `output/candidates.csv` (columns: `id, source, title, authors,
year, venue, doi, url, abstract`). A single source's failure (network error,
exhausted rate limit) is logged to stderr and skipped, not fatal to the whole
run -- Semantic Scholar's unauthenticated pool in particular returns HTTP 429
under load; pass `--s2-api-key` (or set `S2_API_KEY`) for a reliable higher
limit. `--mailto` is required (falls back to the `MAILTO` environment
variable) -- OpenAlex and Crossref use it for their "polite pool" of better
rate limits, per their own API docs.

### `dedup.py` -- de-duplicate by DOI, then fuzzy title match

```bash
python dedup.py --in output/candidates.csv --out output/candidates_dedup.csv \
    --title-threshold 92
```

Exact (normalized) DOI match first; then, for DOI-less records only,
`rapidfuzz` `token_set_ratio` fuzzy title matching within the same
publication year (bucketing by year keeps this roughly linear instead of
O(n²) over the whole corpus). Adds `doi_norm`, `title_norm`, `duplicate_of`,
`dedup_method` columns; canonical (non-duplicate) records have an empty
`duplicate_of`.

### `screen.py` -- build the screening spreadsheet

```bash
python screen.py --in output/candidates_dedup.csv --out output/screening.csv
```

Writes one row per canonical record with blank `ta_decision`/`ta_reason`
(title/abstract stage) and `ft_decision`/`ft_reason` (full-text stage) columns,
plus a `reviewer` column, for a human to fill in. Duplicate this file per
reviewer (or add a second reviewer's decision columns) before computing
inter-rater agreement.

### `download.py` -- resolve and fetch open-access PDFs

```bash
python download.py --in output/candidates_dedup.csv --mailto you@example.com \
    --outdir pdfs --max-downloads 50
```

Tries, per record: an arXiv direct-link pattern (no network lookup needed to
resolve), then Unpaywall (any record with a DOI). `--mailto` is required by
Unpaywall's API on every request. Writes `output/download_log.csv` (columns:
`id, doi, oa_status, method, saved_path`); records with no open-access copy
are logged as such and skipped, not force-downloaded.

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

### `extract.py` -- build the Stage-4/5 extraction + quality-scoring template

```bash
python extract.py --in output/screening.csv --out output/extraction.csv \
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
aggregation formula, decided before scoring begins.

### `verify_citations.py` -- the fabricated-citation guard

```bash
# Check every DOI in a CSV against what it actually resolves to on Crossref
python verify_citations.py --csv output/candidates_dedup.csv \
    --doi-col doi --title-col title --limit 10 \
    --out output/citation_verification.csv

# Check a single ad hoc citation
python verify_citations.py --doi 10.1002/ett.4150 \
    --claimed-title "Network intrusion detection system: A systematic study of machine learning and deep learning approaches"
```

Resolves each DOI against `api.crossref.org/works/<doi>` and fuzzy-matches the
claimed title against what the DOI actually returns (threshold 85/100). Exits
0 only if every checked citation passes -- wire it into a pre-submission check.
Run this against **every** citation before it enters a manuscript, not just
ones that look suspicious: a fabricated citation can sit inside an otherwise
legitimate-looking paper that has already passed title-level screening.

### Figures (after screening + scoring)

`figures.py` generates the Stage 7 reporting figures directly from the
pipeline's own CSVs -- no manual diagramming tool needed. Every figure is
written as both a 300 dpi PNG (for a manuscript/slide) and a vector PDF (for
print/typesetting).

```bash
python figures.py \
    --screening output/screening.csv \
    --dedup output/candidates_dedup.csv \
    --candidates output/candidates.csv \
    --quality output/extraction.csv \
    --outdir figures
```

Three figures land in `--outdir`:

1. **`prisma_flow.png`/`.pdf`** -- the PRISMA 2020 flow diagram (Identification
   -> Screening -> Eligibility -> Included), drawn with matplotlib patches and
   arrows. Counts are derived automatically: records identified from
   `--candidates`; duplicates removed from `--dedup`'s `duplicate_of` column;
   records screened, and excluded/assessed/included counts, from
   `--screening`'s `ta_decision`/`ft_decision` columns (full-text exclusion
   reasons are summarized from `ft_reason`, if present). Pass
   `--identified`/`--duplicates-removed`/`--screened`/`--excluded-ta`/
   `--assessed-ft`/`--excluded-ft`/`--included` to override any single box with
   a final, manually-decided number -- e.g. once the review is complete and the
   published diagram should match the exact numbers in the manuscript. Every
   count actually used (derived or overridden) is printed to stdout.
2. **`quality_tiers.png`/`.pdf`** -- a bar chart of the `A`/`B`/`C`
   `quality_tier` counts from `--quality` (default `output/extraction.csv`).
3. **`venue_tiers.png`/`.pdf`** -- a bar chart of the `T1`/`T2`/`T3`
   `venue_tier` counts from the same file.

Both tier charts render a clear "no data yet" placeholder instead of crashing
if their column is missing or entirely blank -- expected on a first run,
before a human has filled in `extraction.csv`. See `examples/figures/` for
what all three look like once the extraction/quality sheet is actually filled
in (rendered from `examples/screening_decided_sample.csv` and
`examples/quality_scored_sample.csv`; commands in `examples/README.md`).

---

## AI-tools integration

### Where AI fits in the pipeline

| Stage | AI role | Human role |
|---|---|---|
| Search-term generation | Expand/suggest synonym blocks from an initial PICOC | Approve every term before it enters a Boolean string; run the actual queries yourself |
| Title/abstract screening | High-volume first-pass triage against a strict schema | Review every AI decision, especially every "exclude" -- false exclusions are invisible if unchecked |
| Data extraction | Populate the fixed schema (Stage 4) from a PDF's text | Spot-check extracted quantitative findings against the source PDF directly, not just plausibility-check the prose |
| Cross-study synthesis | Draft convergence/conflict summaries from an evidence table you provide | Verify every claimed convergence/conflict against the actual studies; AI tends to over-smooth disagreements into false consensus |
| Section drafting | Draft prose from an evidence table + outline | Full edit pass; run through a readability/humanizing pass (below) |
| Readability/humanizing | Rewrite for plain, non-AI-sounding academic prose | Confirm no meaning was altered in the rewrite |
| Citation verification | Never -- use `verify_citations.py`, not an LLM | Run the DOI-resolution check yourself; treat any mismatch as a hard stop |

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
Return a JSON array of these objects, nothing else -- no prose before or after.

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

Schema: thematic_classification, venue, venue_tier (1/2/3 per: <paste your tier definitions>),
study_type, core_contribution, quantitative_findings (verbatim numbers, with the dataset/
split/baseline context needed to interpret them), rq_mapping (which of RQ1-RQ4 this
evidences), limitations (per the paper's own stated limitations, not your assessment),
quality_tier_provisional (do not fill this in -- quality scoring is a separate human pass).

Paper: <attach or paste full text>
```

Human step: cross-check every `quantitative_findings` entry against the source
PDF directly -- this is the field most likely to contain a subtly wrong number
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
not a measurement-context artifact, and needs separate discussion.

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

Do not use a prompt for this step -- use `verify_citations.py`. An LLM asked
to verify a citation is itself unreliable for the exact failure mode you're
trying to catch (see the integrity guardrails below).

### Integrity guardrails

These are non-negotiable, not best-effort suggestions.

1. **Never trust an LLM-produced (or human-produced) citation without
   independently verifying the DOI resolves to the claimed title and
   authors.** A citation that cites a completely unrelated paper as evidence
   is a real, documented failure mode in systematic reviews -- not a
   hypothetical. Whether it originates from a human author cutting corners or
   an LLM hallucinating a reference, the mitigation is identical: resolve
   every DOI/arXiv ID you cite against a real registry (`verify_citations.py`)
   and confirm title and authors match, before it enters your corpus or your
   own manuscript.
2. **Keep a human in the loop for every single inclusion/exclusion decision.**
   AI-assisted triage can propose a decision; it cannot be the decision. This
   applies at both the title/abstract and full-text stages.
3. **Verify every downloaded PDF's title page against its expected title
   before extraction.** Automated resolution (`download.py`) can fetch the
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
    independent DOI verification (`verify_citations.py`).
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
- [ ] Candidates pulled via `search.py`, unified into one `candidates.csv`
- [ ] De-duplicated via `dedup.py`: DOI exact match, then fuzzy title match
- [ ] Two independent reviewers screen title/abstract; Cohen's kappa computed
      before reconciling
- [ ] Full-text eligibility assessed for everything that survives; every
      downloaded PDF's title page checked against its expected title
- [ ] Complete screening log kept from the first record, not just included ones
- [ ] Fixed-schema extraction for every included study (`extract.py` builds
      the template); quantitative findings copied verbatim with enough
      context to interpret later
- [ ] Quality-rubric scoring (R/A/T/C) with a published aggregation formula;
      per-dimension scores released, not just the aggregate tier
- [ ] Venue tier and quality tier reported as separate axes
- [ ] Narrative/thematic synthesis by RQ, with explicit convergence/conflict
      assessment; meta-analysis only where studies are genuinely comparable
- [ ] Every citation DOI-verified via `verify_citations.py` before it enters
      the manuscript
- [ ] PRISMA flow diagram (`figures.py`) with a reasoned (not bare)
      excluded-count breakdown
- [ ] PRISMA 2020 checklist graded honestly, gaps disclosed rather than hidden
- [ ] Data-availability supplement: search strings, screening log, extraction
      matrix, and full quality-rubric grid all released
- [ ] AI-usage log kept throughout; disclosure statement written from that
      log, not from memory, at submission time

---

## License

This repository's licensing is split by content type:

- **Code** (`search.py`, `dedup.py`, `screen.py`, `download.py`,
  `verify_citations.py`, `extract.py`, `figures.py`) is licensed under the
  **MIT License** -- see [`LICENSE`](LICENSE).
- **Documentation** (this README and `examples/README.md`) is licensed under
  **Creative Commons Attribution 4.0 International (CC-BY-4.0)** -- see
  [`LICENSE-docs`](LICENSE-docs). You're free to reuse, adapt, and redistribute
  the methodology write-up for any purpose, including commercially, as long as
  you give appropriate credit.

Copyright (c) 2026 Md Nayeem Hossain.
