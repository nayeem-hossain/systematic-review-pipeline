"""
slr.py -- guided interactive terminal (TUI) for the systematic-review pipeline.

Walks a researcher through a review-gated multi-phase systematic literature
review: search -> dedup -> screening skeleton -> manual-paste AI-assist
screening (paste into any free web chatbot, no API keys) -> human review
gate -> query expansion -> next phase, for as many phases as chosen, then a
consolidation menu (merge, download PDFs, full-text screening, verify
citations, build the extraction sheet, generate PRISMA/tier figures, export
references, inter-rater agreement, write the provenance report). This is the
top-level entry point:

    python slr.py

Every review lives under its own runs/<run_id>/ workspace (see srp.state);
per-phase/stage checkpoints make the wizard safe to stop and resume at any
point.

Terminology note: the between-phase step is QUERY EXPANSION (suggesting extra
keywords from the terms frequent in your included titles), not citation
snowballing. It was previously labelled "snowball", which named a different and
stronger method than the one implemented -- see run_query_expansion().
"""
# ===========================================================================
# WHAT YOU CHANGE (safe): nothing here for normal use -- run `python slr.py`
#   and answer the prompts. Advanced flags: `python slr.py --help`.
# WHAT YOU DON'T CHANGE (unless you are extending the tool):
#   the parts marked "# --- core logic ---" (phase orchestration, expansion).
# ===========================================================================
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

try:
    import questionary
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    MISSING_UI_DEPS = False
except ImportError:
    questionary = None
    Console = None
    Panel = None
    Table = None
    MISSING_UI_DEPS = True

from srp.agreement import compare_reviewers
from srp.appraisal import (FIELD_PROFILES, INSTRUMENTS, PRIMARY_STUDY,
                            REVIEW_SELF_CHECK, compose_appraisal_disclosure,
                            render_review_self_appraisal)
from srp.config import ReviewConfig
from srp.decisions import AI_REVIEWER, apply_decisions, pick_progressed, ta_proceeds_mask
from srp.env import load_dotenv, parse_env_text, set_env_var, unset_env_var
from srp.methods_report import (PhaseSearchRecord, SourceStrategyRow,
                                 render_search_methods, render_search_strategy_table)
from srp.prisma import PhaseFrames, derive_prisma_counts_for_run, prisma_residuals
from srp.provenance import Provenance
from srp.quality_tier import compute_quality_tier
from srp.state import RunState, record_key
from srp.update_check import is_newer, latest_release_version
from srp import __version__ as _VERSION
from srp import llm_assist
from srp import export as srp_export

_RELEASES_URL = "https://github.com/nayeem-hossain/systematic-review-pipeline/releases"
_CHANGELOG_URL = "https://github.com/nayeem-hossain/systematic-review-pipeline/blob/main/CHANGELOG.md"

_SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = _SCRIPT_DIR / "scripts"
_BATCH_SIZE = 20
_EXCLUDE_SAMPLE_SIZE = 20

# Required for a phase to advance the resume pointer. assist_ta is deliberately
# excluded: screening entirely by hand is a legitimate, stronger choice, and a
# phase must be able to complete without any AI assist. The stage key for
# query expansion stays "snowball" (see run_query_expansion) for resume
# compatibility with runs started before the rename.
_REQUIRED_PHASE_STAGES = ("search", "dedup", "prescreen", "review_gate")

# (stage key, output file relative to the phase dir (None if the stage has no
# file of its own), the counts key that best represents "how many records
# came out of this stage", human label) -- the funnel _menu_diagnose_run walks.
_PHASE_FUNNEL_STAGES = (
    ("search", "candidates.csv", "n_hits", "Search"),
    ("dedup", "candidates_dedup.csv", "n_out", "Dedup (unique kept)"),
    ("prescreen", "screening.csv", "n_rows", "Screening skeleton"),
    ("review_gate", None, "n_included", "Review gate (included after TA)"),
)

# Which config field holds the key for each keyed source, so a resumed run can
# report which keyed sources will be silently skipped (key not rehydrated).
_KEYED_SOURCE_FIELD = {
    "ieee": "ieee_api_key", "scopus": "scopus_api_key",
    "springer": "springer_api_key", "core": "core_api_key",
    "wos": "wos_api_key",
}

# (human label, .env var name) for every API key the setup wizard's _ask_secret
# prompts for -- the source list _menu_manage_api_keys offers, so both stay in
# sync by construction rather than by remembering to update two places.
_MANAGED_API_KEYS = (
    ("Semantic Scholar", "S2_API_KEY"),
    ("PubMed", "PUBMED_API_KEY"),
    ("CORE", "CORE_API_KEY"),
    ("IEEE Xplore", "IEEE_API_KEY"),
    ("Scopus", "SCOPUS_API_KEY"),
    ("Scopus institutional token", "SCOPUS_INSTTOKEN"),
    ("Springer Nature", "SPRINGER_API_KEY"),
    ("Web of Science", "WOS_API_KEY"),
)

# Every source name search.py understands -- the canonical order the setup
# wizard's own sources checkbox uses, reused so _edit_search_settings offers
# the identical list rather than a second copy that could drift.
_ALL_SOURCE_NAMES = (
    "openalex", "semanticscholar", "crossref", "arxiv", "pubmed", "doaj",
    "ieee", "scopus", "springer", "core", "wos",
)

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "in", "on", "to", "with", "by",
    "from", "at", "as", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "we", "our", "their",
    "which", "what", "how", "into", "over", "under", "between", "within",
    "via", "toward", "towards", "new", "novel", "paper", "using", "use",
    "used", "based", "approach", "approaches", "study", "studies", "review",
    "surveys", "survey", "analysis", "system", "systems", "method", "methods",
    "model", "models", "framework", "frameworks", "evaluation", "comparative",
    "comprehensive", "towards",
}


class CorruptCsvError(Exception):
    """Raised when a CSV exists but cannot be parsed -- distinct from
    "missing" or "legitimately empty" (a header-only sheet, or one that
    doesn't exist yet). A corrupt file used to be indistinguishable from an
    absent one, so a stage that read it reported an authoritative "0
    included" instead of refusing to proceed on unreadable data."""


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------
def _script_path(name: str) -> str:
    return str(SCRIPTS_DIR / name)


def _slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def _unique_run_id(runs_dir: Path, slug: str) -> str:
    existing = set(RunState.list_runs(runs_dir))

    def _taken(candidate: str) -> bool:
        return candidate in existing or (Path(runs_dir) / candidate).exists()

    if not _taken(slug):
        return slug
    i = 2
    while True:
        candidate = f"{slug}-{i}"
        if not _taken(candidate):
            return candidate
        i += 1


def _id_to_str(v) -> str:
    if isinstance(v, float) and v == v and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _disp(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v != v:  # NaN
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _native(v):
    """Unwrap a numpy/pandas scalar (e.g. int64) into a plain Python type so
    it survives json.dumps in RunState.record_decision."""
    item = getattr(v, "item", None)
    return item() if callable(item) else v


def _read_csv_safe(path) -> pd.DataFrame:
    """Missing, zero-byte, and header-only files are legitimately empty and
    return an empty DataFrame. A corrupt or undecodable file is NOT -- it
    raises CorruptCsvError rather than being silently treated the same as an
    empty/absent one."""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    except (pd.errors.ParserError, UnicodeDecodeError, OSError) as e:
        raise CorruptCsvError(f"{path} could not be parsed: {e}") from e


def _undecided_ta_ids(screening_path) -> set:
    df = _read_csv_safe(screening_path)
    if df.empty or "ta_decision" not in df.columns or "id" not in df.columns:
        return set()
    mask = df["ta_decision"].isna() | (df["ta_decision"].astype(str).str.strip() == "")
    return {_id_to_str(v) for v in df.loc[mask, "id"]}


def _extract_expansion_terms(titles: list, existing_keywords: list, top_n: int = 8) -> list:
    """Tokenize included titles, drop stopwords + existing keywords, return
    the top ~top_n most frequent 1-2 word terms by frequency."""
    existing_lower = {kw.strip().lower() for kw in existing_keywords if str(kw).strip()}
    counts: Counter = Counter()
    for title in titles:
        words = [w for w in re.findall(r"[a-z0-9]+", str(title).lower())
                 if len(w) > 2 and w not in _STOPWORDS]
        for w in words:
            if w not in existing_lower:
                counts[w] += 1
        for i in range(len(words) - 1):
            bigram = f"{words[i]} {words[i + 1]}"
            if bigram not in existing_lower:
                counts[bigram] += 1
    return [term for term, _ in counts.most_common(top_n)]


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def _ask_int(prompt: str, default: int, minimum: int | None = None):
    def _validate(text: str):
        text = text.strip()
        if not text:
            return True
        if not re.match(r"^-?\d+$", text):
            return "Enter a whole number."
        if minimum is not None and int(text) < minimum:
            return f"Must be >= {minimum}."
        return True

    answer = questionary.text(prompt, default=str(default), validate=_validate).ask()
    if answer is None:
        return None
    answer = answer.strip()
    return int(answer) if answer else default


def _ask_secret(label: str, env_var: str) -> str:
    """Prompt for an optional API key. Shows "[detected in .env]" if the
    environment variable is already set (typically via .env), and pressing
    Enter reuses it. Never aborts the wizard -- Ctrl-C or a blank answer both
    just mean "skip this key"; the typed/detected value is never echoed back
    or persisted to config.json."""
    detected = os.environ.get(env_var, "")
    if detected:
        prompt = f"{label}\n  [detected in .env -- press Enter to use it]:\n"
    else:
        prompt = f"{label} (optional):"
    answer = (questionary.text(prompt, default="").ask() or "").strip()
    return answer or detected


def _should_run_stage(state: RunState, phase: int, stage: str, label: str) -> bool:
    if state.is_stage_done(phase, stage):
        rerun = questionary.confirm(
            f"Phase {phase}: '{label}' already completed -- re-run?", default=False,
        ).ask()
        return bool(rerun)
    return True


def run_subprocess(cmd: list, console: Console, description: str, success_codes=(0,),
                    secret_env: dict | None = None) -> bool:
    """Run a stage CLI script, showing a spinner, and on failure offer
    retry / skip / abort instead of crashing the whole wizard.

    secret_env is merged into the subprocess's environment, never the command
    line -- API keys stay out of terminal scrollback and the process table on
    a shared machine.
    """
    env = {**os.environ, **secret_env} if secret_env else None
    while True:
        console.print(f"[dim]$ {' '.join(cmd)}[/]")
        result = None
        try:
            with console.status(f"[cyan]{description}...[/]", spinner="dots"):
                result = subprocess.run(cmd, capture_output=True, text=True,
                                         cwd=str(_SCRIPT_DIR), env=env)
        except OSError as e:
            console.print(Panel(str(e), title=f"[red]FAILED to launch:[/] {description}",
                                 border_style="red"))

        if result is not None and result.stdout and result.stdout.strip():
            console.print(result.stdout.strip(), style="dim")

        if result is not None and result.returncode in success_codes:
            console.print(f"[green]done[/] {description}")
            return True

        if result is not None:
            lines = (result.stderr or "").strip().splitlines()
            stderr_tail = "\n".join(lines[-20:]) if lines else "(no stderr output)"
            title = f"[red]FAILED:[/] {description} (exit {result.returncode})"
        else:
            stderr_tail = "(process could not be started)"
            title = f"[red]FAILED:[/] {description}"
        console.print(Panel(stderr_tail, title=title, border_style="red"))

        choice = questionary.select("What next?", choices=["Retry", "Skip this step", "Abort"]).ask()
        if choice is None or choice == "Abort":
            console.print("[red]Aborted.[/]")
            raise SystemExit(1)
        if choice == "Skip this step":
            return False
        # "Retry" -> loop again


# ---------------------------------------------------------------------------
# B. new-review setup wizard
# ---------------------------------------------------------------------------
def new_review_wizard(console: Console, runs_dir: Path):
    console.rule("[bold]New review setup[/]")
    console.print(Panel(
        "Answer each prompt to configure your review. A one-line hint appears above every "
        "field, and fields that have a default show it in (parentheses) -- just press Enter "
        "to accept it. Optional fields can be left blank. Press Ctrl+C to cancel.",
        title="How this works", border_style="cyan"))

    def _h(msg: str) -> None:
        """Print a dim, one-line usage hint above the next prompt."""
        console.print(f"[dim]{msg}[/]")

    _h("The subject of your review, in a few words. Names the run folder, seeds the search, "
       "and labels the AI screening prompt.   e.g.  Effects of pair programming on code "
       "quality (illustrative -- use your own topic)")
    topic = questionary.text("Review topic:").ask()
    if topic is None or not topic.strip():
        console.print("[yellow]Cancelled.[/]")
        return None

    _h("Build your query as CONCEPT BLOCKS, one per PICOC concept -- Population, "
       "Intervention, Comparison, Outcome, Context (skip whichever don't apply "
       "to your review; e.g. the intervention, then the outcome). Within a "
       "block, list synonyms -- these are OR'd together. Blocks are AND'd "
       "against each other. Cramming every synonym into one big AND chain "
       "collapses recall fast past 2-3 terms; (a OR b) AND (c OR d) is what a "
       "real search string should look like.\n"
       "      Block 1 e.g.  pair programming, collaborative programming\n"
       "      Block 2 e.g.  code quality, defect density\n"
       "      (illustrative -- use terms for your own topic)\n"
       "      Leave a block blank to stop adding blocks.")
    keyword_blocks: list[list[str]] = []
    block_num = 1
    while True:
        raw = questionary.text(
            f"Block {block_num} -- synonym terms (comma-separated, blank to finish):",
        ).ask()
        if raw is None:
            console.print("[yellow]Cancelled.[/]")
            return None
        terms = [t.strip() for t in raw.split(",") if t.strip()]
        if not terms:
            break
        keyword_blocks.append(terms)
        block_num += 1
    if not keyword_blocks:
        console.print("[yellow]No keywords entered.[/]")

    _h("Your protocol's ELIGIBILITY CRITERIA. These are what screening applies -- both "
       "your own judgement and the AI-assist prompt. Fix them now, before you see any "
       "results: deciding what counts as eligible after you have seen the hits is how "
       "reviews drift toward the answer their author expected.\n"
       "      Separate multiple criteria with ';' or newlines. Leave blank only if you "
       "intend to screen by hand against criteria you keep elsewhere.")
    inclusion_criteria = questionary.text(
        "Inclusion criteria (a study must meet ALL):\n"
        "      e.g.  peer-reviewed OR conference paper; empirically compares pair "
        "programming against solo programming; reports a code-quality or defect metric\n"
        "      (illustrative -- use criteria for your own topic)\n",
        default="",
    ).ask()
    if inclusion_criteria is None:
        console.print("[yellow]Cancelled.[/]")
        return None
    exclusion_criteria = questionary.text(
        "Exclusion criteria (exclude if ANY applies):\n"
        "      e.g.  not in English; no empirical evaluation; survey or review paper; "
        "industry blog post with no reported methodology\n",
        default="",
    ).ask()
    if exclusion_criteria is None:
        console.print("[yellow]Cancelled.[/]")
        return None
    if not inclusion_criteria.strip() and not exclusion_criteria.strip():
        console.print(
            "[yellow]No eligibility criteria recorded.[/] AI-assist screening will fall "
            "back to a generic 'is this plausibly relevant' prompt, which is NOT a "
            "defensible screening criterion for a systematic review. You can add them "
            "later by editing config.json in the run folder.")

    _h("PRISMA 2020 item 24a asks for your protocol registration number, or an explicit "
       "statement that the review was not registered. Either is acceptable; silence is "
       "not. Leave blank to record 'not registered'.   e.g.  OSF: 10.17605/OSF.IO/XXXXX")
    registration_id = questionary.text("Protocol registration ID (optional):", default="").ask()
    if registration_id is None:
        console.print("[yellow]Cancelled.[/]")
        return None

    _h("Does your search include grey literature (preprints, technical reports, theses, "
       "industry whitepapers, blog posts) alongside peer-reviewed venues? Excluding it by "
       "default is a defensible choice, but it is also a publication-bias amplifier "
       "(PRISMA item 14) -- it needs a stated reason either way, not silence.")
    grey_lit_answer = questionary.confirm(
        "Does this review include grey literature?", default=False).ask()
    if grey_lit_answer is None:  # Ctrl-C
        console.print("[yellow]Cancelled.[/]")
        return None
    grey_lit = bool(grey_lit_answer)
    grey_lit_justification = ""
    if not grey_lit:
        grey_lit_justification = (questionary.text(
            "One-line reason grey literature is excluded (optional -- a default reason "
            "is recorded if you leave this blank):", default="").ask() or "").strip()
        if not grey_lit_justification:
            grey_lit_justification = (
                "Grey literature was excluded to prioritize peer-reviewed quality control; "
                "no formal multivocal-review protocol (e.g. Garousi, Felderer & Mantyla "
                "2019) was applied in this review.")

    _h("PRISMA item 21 asks for a reporting-bias assessment (e.g. a funnel plot, Egger's "
       "test) -- but those methods need pooled effect sizes from a meta-analysis. Will you "
       "meta-analyze quantitative results across studies (pool them into a single effect "
       "estimate)? Say no if you plan a narrative synthesis instead -- pooling "
       "heterogeneous datasets/metrics manufactures false precision (see this README's own "
       "'When NOT to meta-analyze').")
    meta_analyze_answer = questionary.confirm(
        "Will this review meta-analyze (pool) quantitative results?", default=False).ask()
    if meta_analyze_answer is None:
        console.print("[yellow]Cancelled.[/]")
        return None
    will_meta_analyze = bool(meta_analyze_answer)
    if will_meta_analyze:
        reporting_bias_assessment = (questionary.text(
            "Describe the reporting-bias assessment you will perform (e.g. funnel plot + "
            "Egger's test on the primary outcome):", default="").ask() or "").strip()
        if not reporting_bias_assessment:
            reporting_bias_assessment = (
                "Meta-analysis is planned, but no specific reporting-bias assessment "
                "method was recorded -- add one (e.g. a funnel plot and Egger's test) "
                "before the synthesis stage.")
    else:
        reporting_bias_assessment = (
            "This review synthesizes findings narratively rather than through "
            "meta-analysis; a formal reporting-bias assessment (funnel plot, Egger's "
            "test) is not applicable without pooled effect sizes, per PRISMA 2020 item "
            "21's own scope.")

    _h("PRISMA items 25/26: funding and competing interests for THIS review (not the "
       "included studies). Leave blank if not applicable.")
    funding_statement = (questionary.text("Funding statement (optional):", default="").ask() or "")
    competing_interests = (questionary.text("Competing interests (optional):", default="").ask() or "")

    _h("PRISMA item 5: any language restriction on eligible studies. Leave blank for none.")
    language_restriction = (questionary.text(
        "Language restriction (optional, e.g. 'English only'):", default="").ask() or "")

    _h("Which field is this review in? This picks the critical-appraisal instrument PRISMA "
       "item 11 requires -- Cochrane RoB 2 is meaningless for a software-engineering "
       "benchmark study, and Dyba & Dingsoyr's checklist is meaningless for a randomized "
       "trial. Pick the closest match; every option maps to a citable, field-appropriate "
       "instrument, not a generic placeholder.")
    field_labels = {profile.label: key for key, profile in FIELD_PROFILES.items()}
    field_choice_label = questionary.select("Research field:", choices=list(field_labels)).ask()
    if field_choice_label is None:
        console.print("[yellow]Cancelled.[/]")
        return None
    field_choice = field_labels[field_choice_label]
    profile = FIELD_PROFILES[field_choice]

    primary_instruments = list(profile.primary_study_instruments)
    if profile.primary_study_instruments:
        names = ", ".join(INSTRUMENTS[k].name for k in profile.primary_study_instruments
                           if k in INSTRUMENTS)
        console.print(f"[dim]Recommended for {profile.label}: {names}[/]")
        use_recommended = questionary.confirm(
            "Use the recommended instrument(s) for critical appraisal?", default=True).ask()
        if not use_recommended:
            choices = [
                questionary.Choice(inst.name, value=key,
                                    checked=key in profile.primary_study_instruments)
                for key, inst in INSTRUMENTS.items() if inst.level == PRIMARY_STUDY
            ]
            console.print(
                "[dim]Space toggles an instrument, Enter confirms your selection.[/]")
            picked = questionary.checkbox(
                "Choose instrument(s) instead:", choices=choices).ask()
            if picked is not None:
                primary_instruments = picked
    else:
        console.print(f"[yellow]{profile.justification}[/]")

    if "mmat_screening" in primary_instruments:
        console.print(
            "[dim]Space toggles a design, Enter confirms your selection.[/]")
        mmat_designs = questionary.checkbox(
            "Your included studies' design(s) (MMAT needs the matching sub-checklist for "
            "each -- select all that apply):",
            choices=[
                questionary.Choice("Qualitative", value="mmat_qualitative"),
                questionary.Choice("Randomized controlled trial", value="mmat_rct"),
                questionary.Choice("Non-randomized quantitative", value="mmat_nonrandomized"),
                questionary.Choice("Quantitative descriptive", value="mmat_descriptive"),
                questionary.Choice("Mixed methods", value="mmat_mixed"),
            ],
        ).ask() or []
        primary_instruments = list(primary_instruments) + list(mmat_designs)

    certainty_framework = profile.certainty_framework
    if certainty_framework:
        cname = INSTRUMENTS[certainty_framework].name
        use_certainty = questionary.confirm(
            f"Assess certainty of evidence using {cname}?", default=True).ask()
        if not use_certainty:
            certainty_framework = ""

    appraisal_justification = compose_appraisal_disclosure(
        field_choice, primary_instruments, certainty_framework, profile.review_level_instrument)

    _h("Publication-year range for the search (inclusive on both ends).")
    year_from = _ask_int("Year from:", default=2020)
    if year_from is None:
        console.print("[yellow]Cancelled.[/]")
        return None
    year_to = _ask_int("Year to:", default=2026)
    if year_to is None:
        console.print("[yellow]Cancelled.[/]")
        return None

    _h("A real, deliverable email. OpenAlex and Crossref use it for faster polite-pool "
       "access, and Unpaywall requires it to look up open-access PDFs. Do not use an "
       "@example.com placeholder.")
    mailto = questionary.text(
        "Contact email (used for API polite-pool access):",
        validate=lambda s: True if "@" in s else "Must contain @",
    ).ask()
    if mailto is None:
        console.print("[yellow]Cancelled.[/]")
        return None

    _h("Every API key below is OPTIONAL -- leave any of them blank. OpenAlex, Crossref, "
       "arXiv, DOAJ, PubMed and Semantic Scholar all work with no key at all. A key either "
       "raises a rate limit or unlocks a source; a source whose key is blank is simply "
       "skipped, never an error. Press Enter to skip each one.\n"
       "Anything already in your .env is shown as [detected] and used automatically -- "
       "just press Enter. Keys are NEVER written into the run folder, so a key you type "
       "here lasts only for this session; put it in .env to survive a resume.")
    s2_api_key = _ask_secret(
        "Semantic Scholar API key (raises the rate limit above the free shared pool; "
        "semanticscholar.org/product/api)", "S2_API_KEY")
    pubmed_api_key = _ask_secret(
        "NCBI/PubMed API key (raises PubMed from 3 to 10 requests/sec; "
        "ncbi.nlm.nih.gov/account)", "PUBMED_API_KEY")
    core_api_key = _ask_secret(
        "CORE API key (unlocks CORE as both a search source and a PDF source; "
        "core.ac.uk/services/api)", "CORE_API_KEY")
    ieee_api_key = _ask_secret(
        "IEEE Xplore API key (unlocks IEEE; needs an institutional subscription; "
        "developer.ieee.org)", "IEEE_API_KEY")
    scopus_api_key = _ask_secret(
        "Scopus/Elsevier API key (unlocks Scopus; needs an institutional subscription; "
        "dev.elsevier.com)", "SCOPUS_API_KEY")
    scopus_insttoken = ""
    if scopus_api_key.strip():
        _h("Scopus keys are authenticated by your institution's IP range, so they only "
           "work on campus. An institutional token (requested from Elsevier support) lets "
           "the key work off-campus. Leave blank if you only run on campus.")
        scopus_insttoken = _ask_secret(
            "Scopus institutional token (only needed off-campus)", "SCOPUS_INSTTOKEN")
    springer_api_key = _ask_secret(
        "Springer Nature API key (unlocks Springer; free self-signup at "
        "dev.springernature.com)", "SPRINGER_API_KEY")
    wos_api_key = _ask_secret(
        "Web of Science Expanded API key (unlocks Web of Science; needs an institutional "
        "subscription; developer.clarivate.com)", "WOS_API_KEY")

    _h("Which literature databases to query. Space toggles a source, Enter confirms. The "
       "keyless sources are pre-checked. IEEE / Scopus / Springer / CORE / WoS need the matching "
       "key above -- if you check one without a key it is skipped with a note.")
    sources = questionary.checkbox(
        "Sources to search:",
        choices=[
            questionary.Choice("openalex", checked=True),
            questionary.Choice("semanticscholar", checked=True),
            questionary.Choice("crossref", checked=True),
            questionary.Choice("arxiv", checked=True),
            questionary.Choice("pubmed", checked=True),
            questionary.Choice("doaj", checked=True),
            questionary.Choice("ieee", checked=bool(ieee_api_key.strip())),
            questionary.Choice("scopus", checked=bool(scopus_api_key.strip())),
            questionary.Choice("springer", checked=bool(springer_api_key.strip())),
            questionary.Choice("core", checked=bool(core_api_key.strip())),
            questionary.Choice("wos", checked=bool(wos_api_key.strip())),
        ],
    ).ask()
    if not sources:
        sources = ["openalex", "semanticscholar", "crossref", "arxiv", "pubmed", "doaj"]

    _h("Cap on records fetched from each source, per phase. Keep it small (40-60) while "
       "testing; raise it for a full run.")
    max_per_source = _ask_int("Max results per source:", default=60, minimum=1)
    if max_per_source is None:
        console.print("[yellow]Cancelled.[/]")
        return None

    _h("How many review-gated search rounds to run. 1 = a single search. With more than "
       "1, after you review each phase's included studies the tool suggests new keywords "
       "drawn from their titles and searches again.\n"
       "      Note this is QUERY EXPANSION, not citation snowballing: it follows words, "
       "not references. If your protocol promises snowballing (Wohlin 2014), use the "
       "consolidation menu's Citation snowballing action (or scripts/snowball.py directly) "
       "and record it as a separate identification method.")
    n_phases = _ask_int("Number of search phases:", default=1, minimum=1)
    if n_phases is None:
        console.print("[yellow]Cancelled.[/]")
        return None

    _h("Fuzzy title-match cutoff for near-duplicate detection (0-100). 92 is a safe "
       "default; lower merges more aggressively but risks false matches.")
    title_threshold = _ask_int("Fuzzy title-dedup threshold (0-100):", default=92, minimum=0)
    if title_threshold is None:
        console.print("[yellow]Cancelled.[/]")
        return None

    _h("Free text -- ANY web chatbot works; the paste-and-parse workflow does not depend "
       "on the tool. This is only recorded in the provenance file as your AI-assistance "
       "disclosure. Examples: ChatGPT, Claude, Gemini, Microsoft Copilot, DeepSeek, Qwen, "
       "Mistral (Le Chat), Perplexity, HuggingChat. Leave blank to screen fully by hand.")
    assist_tool_name = (questionary.text(
        "Which web chatbot will you paste screening prompts into? (free text -- any tool; "
        "e.g. ChatGPT / Claude / Gemini / Copilot / DeepSeek / Qwen)", default="").ask() or "")

    _h("Your name or initials, stamped onto decisions and the audit trail. Optional.")
    reviewer = (questionary.text("Reviewer name/initials:", default="").ask() or "")

    slug = _slugify(topic) or "review"
    run_id = _unique_run_id(runs_dir, slug)

    cfg = ReviewConfig(
        topic=topic.strip(), keyword_blocks=keyword_blocks, year_from=year_from,
        year_to=year_to, mailto=mailto.strip(),
        s2_api_key=s2_api_key.strip(), pubmed_api_key=pubmed_api_key.strip(),
        core_api_key=core_api_key.strip(), ieee_api_key=ieee_api_key.strip(),
        scopus_api_key=scopus_api_key.strip(), scopus_insttoken=scopus_insttoken.strip(),
        springer_api_key=springer_api_key.strip(), wos_api_key=wos_api_key.strip(),
        sources=sources, max_per_source=max_per_source, n_phases=n_phases,
        title_threshold=title_threshold, assist_tool_name=assist_tool_name.strip(),
        reviewer=reviewer.strip(),
        inclusion_criteria=inclusion_criteria.strip(), exclusion_criteria=exclusion_criteria.strip(),
        registration_id=registration_id.strip(),
        research_field=field_choice, primary_study_instruments=primary_instruments,
        certainty_framework=certainty_framework, review_level_instrument=profile.review_level_instrument,
        appraisal_justification=appraisal_justification,
        funding_statement=funding_statement.strip(), competing_interests=competing_interests.strip(),
        language_restriction=language_restriction.strip(),
        grey_literature_included=grey_lit, grey_literature_justification=grey_lit_justification,
        reporting_bias_assessment=reporting_bias_assessment,
    )

    instrument_names = ", ".join(INSTRUMENTS[k].name for k in primary_instruments
                                  if k in INSTRUMENTS) or "(none)"
    certainty_name = INSTRUMENTS[certainty_framework].name if certainty_framework else "(none / not applicable)"
    summary_lines = [
        f"Run id: {run_id}",
        f"Topic: {cfg.topic}",
        f"Keywords: {cfg.display_keywords() or '(none)'}",
        f"Years: {cfg.year_from}-{cfg.year_to}",
        f"Sources: {', '.join(cfg.sources)}",
        f"API keys set: {', '.join(cfg.configured_key_names()) or '(none -- keyless sources only)'}",
        f"Max per source: {cfg.max_per_source}",
        f"Phases: {cfg.n_phases}",
        f"Title-dedup threshold: {cfg.title_threshold}",
        f"AI-assist tool: {cfg.assist_tool_name or '(none)'}",
        f"Reviewer: {cfg.reviewer or '(none)'}",
        f"Field: {profile.label}",
        f"Appraisal instrument(s): {instrument_names}",
        f"Certainty framework: {certainty_name}",
        f"Grey literature included: {'yes' if grey_lit else 'no'}"
        + ("" if grey_lit else f" ({grey_lit_justification})"),
        f"Search query: {cfg.search_query()}",
    ]
    console.print(Panel("\n".join(summary_lines), title="Review summary", border_style="cyan"))

    create_answer = questionary.confirm("Create this review?", default=True).ask()
    if not create_answer:
        console.print("[yellow]Cancelled.[/]")
        return None

    state = RunState.create(runs_dir, run_id, cfg.to_dict())
    prov = Provenance(state.run_dir / "provenance.jsonl")
    prov.log("review_created", run_id=run_id, topic=cfg.topic, keywords=cfg.display_keywords())
    console.print(f"[green]Created run[/] {run_id} at {state.run_dir}")

    return state, cfg, prov


# ---------------------------------------------------------------------------
# C. phase loop
# ---------------------------------------------------------------------------
# --- core logic ---
def _run_search_dedup_prescreen(state: RunState, cfg: ReviewConfig, prov: Provenance,
                                 console: Console, phase: int, pdir: Path, query: str) -> None:
    """Runs search.py -> dedup.py -> screen.py for one phase, marking each
    stage's counts on success. Shared by run_phase_loop's normal forward pass
    and _menu_rerun_search's redo, so the exact same three subprocess calls
    run either way -- a fix to one path can't silently drift from the other."""
    # 1. SEARCH
    if _should_run_stage(state, phase, "search", "Search"):
        candidates_path = pdir / "candidates.csv"
        strategy_path = pdir / "search_strategy.csv"
        cmd = [
            sys.executable, _script_path("search.py"),
            "--query", query,
            "--year-from", str(cfg.year_from),
            "--year-to", str(cfg.year_to),
            "--mailto", cfg.mailto,
            "--max-per-source", str(cfg.max_per_source),
            "--sources", ",".join(cfg.sources),
            "--out", str(candidates_path),
            "--strategy-log", str(strategy_path),
        ]
        if cfg.scopus_insttoken:
            cmd += ["--scopus-insttoken", cfg.scopus_insttoken]
        if run_subprocess(cmd, console, f"Phase {phase}: searching sources",
                           secret_env=cfg.secret_env()):
            n_hits = len(_read_csv_safe(candidates_path))
            state.mark_stage(phase, "search", counts={"n_hits": n_hits})
            prov.log("search_run", phase=phase, query=query, sources=cfg.sources, n_hits=n_hits)

            strategy_df = _read_csv_safe(strategy_path)
            if not strategy_df.empty and "truncated" in strategy_df.columns:
                truncated = strategy_df.loc[
                    strategy_df["truncated"].astype(str).str.lower().eq("true"), "source"
                ].tolist()
                if truncated:
                    console.print(
                        f"[yellow]Truncated by --max-per-source ({cfg.max_per_source}):[/] "
                        f"{', '.join(truncated)}. Those sources returned only the "
                        f"top-ranked slice, so this phase's hit count is a sample "
                        f"rather than a complete search. See {strategy_path.name}.")

    # 2. DEDUP
    if _should_run_stage(state, phase, "dedup", "Dedup"):
        candidates_path = pdir / "candidates.csv"
        dedup_path = pdir / "candidates_dedup.csv"
        cmd = [
            sys.executable, _script_path("dedup.py"),
            "--in", str(candidates_path), "--out", str(dedup_path),
            "--title-threshold", str(cfg.title_threshold),
        ]
        if run_subprocess(cmd, console, f"Phase {phase}: de-duplicating"):
            n_in = len(_read_csv_safe(candidates_path))
            dedup_df = _read_csv_safe(dedup_path)
            n_dupes = int(dedup_df["duplicate_of"].notna().sum()) if "duplicate_of" in dedup_df.columns else 0
            n_out = len(dedup_df) - n_dupes
            state.mark_stage(phase, "dedup", counts={"n_in": n_in, "n_out": n_out, "n_dupes": n_dupes})
            prov.log("dedup_run", phase=phase, n_in=n_in, n_out=n_out, n_dupes=n_dupes)

    # 3. PRESCREEN skeleton
    if _should_run_stage(state, phase, "prescreen", "Prescreen skeleton"):
        dedup_path = pdir / "candidates_dedup.csv"
        screening_path = pdir / "screening.csv"
        cmd = [
            sys.executable, _script_path("screen.py"),
            "--in", str(dedup_path), "--out", str(screening_path),
        ]
        if run_subprocess(cmd, console, f"Phase {phase}: building screening skeleton"):
            n_rows = len(_read_csv_safe(screening_path))
            state.mark_stage(phase, "prescreen", counts={"n_rows": n_rows})
            prov.log("prescreen_run", phase=phase, n_rows=n_rows)


def run_phase_loop(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    n_phases = cfg.n_phases
    phase = state.state.get("current_phase", 1)

    while phase <= n_phases:
        console.rule(f"[bold]Phase {phase} / {n_phases}[/]")
        pdir = state.phase_dir(phase)

        query_key = f"phase_{phase}_query"
        query = state.state.get(query_key)
        if not query:
            query = cfg.search_query()
            state.state[query_key] = query
            state.save()

        _run_search_dedup_prescreen(state, cfg, prov, console, phase, pdir, query)

        # 4. AI-ASSIST TA (manual paste loop)
        if _should_run_stage(state, phase, "assist_ta", "AI-assist TA screening"):
            run_ai_assist_loop(state, cfg, prov, phase, pdir, console)

        # 5. HUMAN REVIEW GATE
        if _should_run_stage(state, phase, "review_gate", "Human review gate"):
            run_review_gate(state, cfg, prov, phase, pdir, console)

        # 6. QUERY EXPANSION (only if there is a next phase)
        if phase < n_phases:
            if _should_run_stage(state, phase, "snowball", "Query expansion"):
                run_query_expansion(state, cfg, prov, phase, pdir, console)

        missing = [s for s in _REQUIRED_PHASE_STAGES if state.stage_status(phase, s) != "done"]
        if missing:
            console.print(Panel(
                f"Phase {phase} is incomplete -- these stages did not finish: "
                f"{', '.join(missing)}.\n\nNot advancing to the next phase, so this one "
                f"stays resumable. Re-run `python slr.py` and resume this run to retry "
                f"them.", title="[yellow]Phase not complete[/]", border_style="yellow"))
            prov.log("phase_incomplete", phase=phase, missing=missing)
            return

        if state.state.get("current_phase", 1) <= phase:
            state.state["current_phase"] = phase + 1
            state.save()
        phase += 1

    console.print(Panel(f"All {n_phases} phase(s) complete.", title="Phase loop finished",
                         border_style="green"))


# --- core logic ---
def run_ai_assist_loop(state: RunState, cfg: ReviewConfig, prov: Provenance, phase: int,
                        pdir: Path, console: Console) -> None:
    dedup_path = pdir / "candidates_dedup.csv"
    screening_path = pdir / "screening.csv"
    skipped_ids: set = set()
    totals: Counter = Counter()
    tool_name = cfg.assist_tool_name or "your web chatbot"

    # Refuse to build a "plausibly relevant"-style generic prompt without an
    # explicit, once-per-phase override: this fallback recreates the exact
    # unoperationalized judgement call a systematic review's screening exists to
    # foreclose. A printed warning that AI-assist still runs past is not a real
    # guardrail -- see srp/llm_assist.py's DEFAULT_CRITERIA docstring.
    criteria = llm_assist.compose_criteria("ta", cfg.inclusion_criteria, cfg.exclusion_criteria)
    if not criteria:
        console.print(
            "[yellow]No eligibility criteria are set in this review's config.[/] Without "
            "them, AI-assist screening falls back to a generic 'is this plausibly "
            "relevant' judgement -- not your protocol's criteria, and not defensible "
            "eligibility-criteria screening for a systematic review."
        )
        proceed = questionary.confirm(
            "Run AI-assist screening anyway with the generic fallback?", default=False,
        ).ask()
        if not proceed:
            console.print(
                "[yellow]AI-assist screening skipped for this phase.[/] Add "
                "inclusion_criteria/exclusion_criteria to config.json, then re-run this "
                "step -- or screen screening.csv by hand instead."
            )
            return

    while True:
        dedup_df = _read_csv_safe(dedup_path)
        if dedup_df.empty:
            console.print("[yellow]No candidates to screen (candidates_dedup.csv is empty or missing).[/]")
            break
        if "duplicate_of" in dedup_df.columns:
            dedup_df = dedup_df[dedup_df["duplicate_of"].isna()]

        screening_df = _read_csv_safe(screening_path)
        decided_ids: set = set()
        if not screening_df.empty and "ta_decision" in screening_df.columns:
            mask = screening_df["ta_decision"].notna() & (screening_df["ta_decision"].astype(str).str.strip() != "")
            decided_ids = {_id_to_str(v) for v in screening_df.loc[mask, "id"]}

        cross_phase_decided = state.decided_keys("ta")

        undecided_rows = []
        for _, row in dedup_df.iterrows():
            rid = _id_to_str(row.get("id"))
            if rid in decided_ids or rid in skipped_ids:
                continue
            rk = record_key(row.get("doi", ""), row.get("title", ""))
            if rk and rk in cross_phase_decided:
                continue
            undecided_rows.append(row)

        if not undecided_rows:
            console.print("[green]No undecided studies left for TA screening in this phase.[/]")
            break

        total_undecided = len(undecided_rows)
        batch = undecided_rows[:_BATCH_SIZE]
        start = len(dedup_df) - len(undecided_rows)
        records = [
            {"id": row.get("id"), "title": row.get("title", ""), "abstract": row.get("abstract", ""),
             "year": row.get("year", ""), "venue": row.get("venue", "")}
            for row in batch
        ]
        remaining_after = total_undecided - len(records)
        total_batches = -(-total_undecided // _BATCH_SIZE)
        batch_label = f"phase={phase} stage=ta rows={start}-{start + len(records) - 1}"
        prompt = llm_assist.build_screening_prompt(
            records, stage="ta", topic=cfg.topic, criteria=criteria,
            batch_label=batch_label,
        )
        prompt_path = pdir / f"prompt_ta_{start}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        # Pre-create the reply file so the user only ever has to paste + save into
        # an existing file, not create one from scratch at the exact right path.
        default_reply = pdir / f"reply_ta_{start}.txt"
        if not default_reply.exists():
            default_reply.write_text("", encoding="utf-8")

        console.print(Panel(
            f"{total_undecided} record(s) undecided for TA screening in phase {phase} "
            f"({total_batches} batch(es) including this one). This batch covers "
            f"{len(records)}, leaving {remaining_after} undecided after it.\n\n"
            f"Paste the contents of [bold]{prompt_path}[/] into [bold]{tool_name}[/].\n"
            f"An empty reply file is already waiting at [bold]{default_reply}[/] -- paste "
            f"the chatbot's answer into it and save (one line per study, e.g.\n"
            f"  12 | INCLUDE | on-topic pair-programming study).",
            title=f"Phase {phase}: AI-assist TA screening -- batch of {len(records)}",
            border_style="cyan",
        ))
        if prompt_path.stat().st_size < 3000:
            if questionary.confirm("Show the prompt text now?", default=False).ask():
                console.print(Panel(prompt, title=str(prompt_path)))

        action = questionary.select(
            "How do you want to proceed with this batch?",
            choices=[
                "I've pasted it and saved the reply -- give reply file path",
                "Skip AI-assist for this batch (leave for manual human screening)",
                "Stop AI-assist screening for this phase",
            ],
        ).ask()
        if action is None or action == "Stop AI-assist screening for this phase":
            break
        if action == "Skip AI-assist for this batch (leave for manual human screening)":
            skipped_ids.update(_id_to_str(row.get("id")) for row in batch)
            console.print(f"[yellow]Skipped {len(batch)} row(s) -- left blank for manual screening.[/]")
            continue

        reply_path = None
        while reply_path is None:
            reply_str = questionary.text(
                "Path to the chatbot reply file:", default=str(default_reply),
            ).ask()
            if reply_str is None:
                break
            candidate = Path(reply_str)
            problem = None
            if not candidate.exists():
                problem = f"{candidate} does not exist yet."
            else:
                try:
                    if not candidate.read_text(encoding="utf-8").strip():
                        problem = f"{candidate} is still empty -- paste the reply into it and save first."
                except OSError as e:
                    problem = f"Could not read {candidate}: {e}"
            if problem is None:
                reply_path = candidate
            else:
                retry = questionary.confirm(f"{problem} Try again?", default=True).ask()
                if not retry:
                    break

        if reply_path is None:
            console.print("[yellow]No reply file provided -- batch left undecided.[/]")
            if not questionary.confirm("Screen another batch?", default=True).ask():
                break
            continue

        try:
            text = reply_path.read_text(encoding="utf-8")
        except OSError as e:
            console.print(f"[red]Could not read {reply_path}: {e}[/]")
            continue

        # valid_ids pins the reply to the batch we actually sent: an id the model
        # invents or echoes from an earlier batch is refused, not written onto
        # whatever record happens to carry that number.
        batch_ids = [row.get("id") for row in batch]
        parsed = llm_assist.parse_screening_response(text, valid_ids=batch_ids)
        for problem in parsed.problems():
            console.print(f"[yellow]Reply check:[/] {problem}")

        applied = apply_decisions(screening_path, parsed, state, phase, stage="ta")
        totals.update(applied.counts)

        table = Table(title="Batch result")
        table.add_column("decision")
        table.add_column("count", justify="right")
        for decision, n in sorted(applied.counts.items()):
            table.add_row(decision, str(n))
        console.print(table)
        for problem in applied.problems():
            console.print(f"[yellow]Apply check:[/] {problem}")

        prov.log(
            "assist_response_parsed", phase=phase, n_decided=len(applied.matched),
            n_include=applied.counts.get("include", 0), n_exclude=applied.counts.get("exclude", 0),
            tool=cfg.assist_tool_name,
        )

        remaining_now = len(_undecided_ta_ids(screening_path) - skipped_ids)
        console.print(f"[dim]{remaining_now} record(s) still undecided for TA screening "
                      f"in phase {phase}.[/]")

        if not questionary.confirm("Screen another batch?", default=True).ask():
            break

    remaining = _undecided_ta_ids(screening_path) - skipped_ids
    if remaining:
        console.print(Panel(
            f"{len(remaining)} record(s) in phase {phase} still have no title/abstract "
            f"decision, so AI-assist screening is NOT marked complete.\n\nEither screen "
            f"them (re-run this step), or decide them by hand in {screening_path.name}. "
            f"They are excluded from the review gate until they have a decision.",
            title="[yellow]Screening incomplete[/]", border_style="yellow",
        ))
        prov.log("assist_incomplete", phase=phase, n_undecided=len(remaining),
                  n_skipped_for_manual=len(skipped_ids))
        return

    state.mark_stage(phase, "assist_ta", counts=dict(totals))
    if skipped_ids:
        console.print(f"[dim]{len(skipped_ids)} record(s) were skipped for manual "
                      f"screening.[/]")


# --- core logic ---
def run_review_gate(state: RunState, cfg: ReviewConfig, prov: Provenance, phase: int,
                     pdir: Path, console: Console) -> None:
    screening_path = pdir / "screening.csv"
    screening_df = _read_csv_safe(screening_path)
    if screening_df.empty or "ta_decision" not in screening_df.columns:
        console.print("[yellow]No screening data yet for this phase's review gate.[/]")
        state.mark_stage(phase, "review_gate", counts={"n_included": 0, "n_overridden": 0})
        prov.log("phase_review", phase=phase, n_included=0, n_overridden=0)
        return

    for col in ("ta_decision", "ta_reason"):
        if col in screening_df.columns:
            screening_df[col] = screening_df[col].astype(object)

    decisions = screening_df["ta_decision"].astype(str).str.strip().str.lower()
    reviewers = (screening_df["reviewer"].astype(str).str.strip().str.lower()
                 if "reviewer" in screening_df.columns
                 else pd.Series([""] * len(screening_df), index=screening_df.index))
    # "maybe" proceeds to full-text assessment exactly like "include" -- see
    # srp.decisions.TA_PROCEED_DECISIONS for why. included_df is therefore the
    # PROCEED set, not literally "decided == include".
    included_mask = ta_proceeds_mask(decisions)
    excluded_mask = decisions.eq("exclude")
    ai_mask = reviewers.eq(AI_REVIEWER)
    included_df = screening_df[included_mask]
    ai_excluded_df = screening_df[excluded_mask & ai_mask]
    n_maybe_in_proceed = int(decisions.eq("maybe").sum())

    def _show(df: pd.DataFrame, title: str, show_decision: bool = False) -> None:
        table = Table(title=title)
        cols = ["id", "title", "year", "venue"]
        if show_decision:
            cols.append("decision")
        cols.append("reason")
        for col in cols:
            table.add_column(col)
        for _, row in df.iterrows():
            values = [_disp(row.get("id", "")), _disp(row.get("title", ""))[:70],
                      _disp(row.get("year", "")), _disp(row.get("venue", ""))[:25]]
            if show_decision:
                values.append(_disp(row.get("ta_decision", "")))
            values.append(_disp(row.get("ta_reason", ""))[:40])
            table.add_row(*values)
        console.print(table)

    if included_df.empty:
        console.print(f"[yellow]No TA-included/maybe studies in phase {phase} yet.[/]")
    else:
        label = "AI proposed to INCLUDE"
        if n_maybe_in_proceed:
            label += f" or flagged MAYBE ({n_maybe_in_proceed} maybe -- both proceed to full text)"
        _show(included_df, f"Phase {phase}: {label} ({len(included_df)})",
              show_decision=bool(n_maybe_in_proceed))

    # Show the AI's EXCLUDES too. The gate used to display only includes and only
    # accept include->exclude flips, so it could exclusively shrink the corpus: a
    # model that systematically excluded papers using unfamiliar terminology
    # produced a coherent, invisible bias and the reviewer saw only a plausible
    # include list. False exclusions are the main failure mode of LLM-assisted
    # screening and this gate was structurally incapable of catching one.
    n_excl_sample = 0
    sample_size = 0
    if not ai_excluded_df.empty:
        sample_size = min(_EXCLUDE_SAMPLE_SIZE, len(ai_excluded_df))
        # Deterministic sample: seeded by phase so re-running the gate shows the same
        # records, and the sample is reproducible from the provenance log.
        sample = ai_excluded_df.sample(n=sample_size, random_state=phase) \
            if sample_size < len(ai_excluded_df) else ai_excluded_df
        n_excl_sample = len(sample)
        console.print(
            f"[dim]The AI excluded {len(ai_excluded_df)} record(s). A false exclusion is "
            f"invisible unless you look, so below is a "
            f"{'complete list' if sample_size == len(ai_excluded_df) else f'random sample of {sample_size}'} "
            f"to check. Anything you rescue here goes back into the review.[/]")
        _show(sample, f"Phase {phase}: AI proposed to EXCLUDE "
                       f"(showing {n_excl_sample} of {len(ai_excluded_df)})")
        if sample_size < len(ai_excluded_df) and questionary.confirm(
                f"Show all {len(ai_excluded_df)} excluded records?", default=False).ask():
            _show(ai_excluded_df, f"Phase {phase}: all AI exclusions ({len(ai_excluded_df)})")
            n_excl_sample = len(ai_excluded_df)

    console.print(
        "[dim]This is your decision gate. Flip anything you disagree with, in either "
        "direction. Press Enter to leave a list unchanged.   e.g.  7, 12, 30[/]")

    to_exclude = questionary.text(
        "Ids to flip INCLUDE/MAYBE -> exclude (blank = accept them as they are):",
        default="").ask()
    if to_exclude is None:  # Ctrl-C
        console.print("[yellow]Review gate cancelled -- nothing recorded.[/]")
        return
    to_include = questionary.text(
        "Ids to flip EXCLUDE -> include (blank = accept the exclusions as they are):",
        default="").ask()
    if to_include is None:
        console.print("[yellow]Review gate cancelled -- nothing recorded.[/]")
        return

    flip_to_exclude = {s.strip() for s in to_exclude.split(",") if s.strip()}
    flip_to_include = {s.strip() for s in to_include.split(",") if s.strip()}

    def _flip(idx, row, new_decision: str) -> None:
        screening_df.at[idx, "ta_decision"] = new_decision
        prior = _disp(screening_df.at[idx, "ta_reason"])
        screening_df.at[idx, "ta_reason"] = (prior + " [reviewer override]").strip()
        if "reviewer" in screening_df.columns:
            screening_df.at[idx, "reviewer"] = cfg.reviewer or "human-review"
        state.record_decision(
            record_key=record_key(row.get("doi", ""), row.get("title", "")),
            id=_native(row.get("id")),
            decision=new_decision,
            stage="ta",
            phase=phase,
            reason="reviewer override",
            source="human-review",
            title=row.get("title", ""),
            doi=row.get("doi", ""),
        )

    n_to_exclude = n_to_include = 0
    unknown: list[str] = []
    if flip_to_exclude or flip_to_include:
        for col in ("reviewer",):
            if col in screening_df.columns:
                screening_df[col] = screening_df[col].astype(object)
        # Resolve which rows actually match a typed id BEFORE writing anything --
        # a typo landing on a different, valid id in the same batch used to be
        # applied silently, with only an aggregate count shown afterward. The
        # gate already treats an invisible false-exclusion as worth a dedicated
        # display one screen earlier; a mistyped id deserves the same visibility.
        pending: list[tuple] = []
        seen_ids = set()
        for idx, row in screening_df.iterrows():
            rid = _id_to_str(row.get("id", ""))
            seen_ids.add(rid)
            if rid in flip_to_exclude and included_mask.get(idx, False):
                pending.append((idx, row, "exclude"))
            elif rid in flip_to_include and excluded_mask.get(idx, False):
                pending.append((idx, row, "include"))
        unknown = sorted((flip_to_exclude | flip_to_include) - seen_ids)

        proceed = True
        if pending:
            preview = Table(title=f"About to flip {len(pending)} record(s)")
            for col in ("id", "title", "-> new decision"):
                preview.add_column(col)
            for _, row, new_decision in pending:
                preview.add_row(_disp(row.get("id", "")), _disp(row.get("title", ""))[:70],
                                 new_decision)
            console.print(preview)
            proceed = bool(questionary.confirm(
                f"Apply these {len(pending)} flip(s)?", default=True).ask())

        if pending and proceed:
            for idx, row, new_decision in pending:
                _flip(idx, row, new_decision)
                if new_decision == "exclude":
                    n_to_exclude += 1
                else:
                    n_to_include += 1
            screening_df.to_csv(screening_path, index=False, encoding="utf-8")
        elif pending:
            console.print("[yellow]Flips not applied -- nothing written.[/]")

    if unknown:
        console.print(f"[yellow]{len(unknown)} id(s) not found in this phase and ignored: "
                       f"{', '.join(unknown)}[/]")

    n_included_final = len(included_df) - n_to_exclude + n_to_include
    console.print(f"[green]{n_included_final}[/] studies included for phase {phase} "
                  f"({n_to_exclude} flipped to exclude, {n_to_include} rescued from exclude).")

    n_undecided = int((screening_df["ta_decision"].isna() |
                        (screening_df["ta_decision"].astype(str).str.strip() == "")).sum())
    if n_undecided:
        console.print(Panel(
            f"{n_undecided} record(s) in phase {phase} still have no title/abstract "
            f"decision, so the review gate is NOT marked complete.\n\nScreen them "
            f"(AI-assist or by hand in {screening_path.name}), then re-run this step. "
            f"A phase cannot be gated -- and the pipeline cannot advance past it -- "
            f"while records remain unaccounted for.",
            title="[yellow]Review gate incomplete[/]", border_style="yellow",
        ))
        prov.log("review_gate_incomplete", phase=phase, n_undecided=n_undecided)
        return

    state.mark_stage(phase, "review_gate",
                      counts={"n_included": n_included_final, "n_overridden": n_to_exclude + n_to_include})
    prov.log("phase_review", phase=phase, n_included=n_included_final,
              n_overridden=n_to_exclude + n_to_include)


# --- core logic ---
def run_query_expansion(state: RunState, cfg: ReviewConfig, prov: Provenance, phase: int,
                         pdir: Path, console: Console) -> None:
    screening_df = _read_csv_safe(pdir / "screening.csv")
    titles = []
    if not screening_df.empty and "ta_decision" in screening_df.columns:
        proceed_mask = ta_proceeds_mask(screening_df["ta_decision"])
        titles = screening_df.loc[proceed_mask, "title"].dropna().astype(str).tolist()

    terms = _extract_expansion_terms(titles, cfg.all_keywords())
    picked: list = []
    if terms:
        console.print(
            "[dim]These candidate terms were pulled from this phase's included titles. "
            "Space toggles a term, Enter confirms. Your base keywords are always kept.[/]")
        picked = questionary.checkbox(
            f"Phase {phase} -> {phase + 1}: pick expansion keyword(s) to ADD to the search query:",
            choices=terms,
        ).ask() or []
    else:
        console.print("[yellow]No candidate expansion terms found from this phase's included titles.[/]")

    console.print(
        "[dim]Optionally type your own extra term(s) to add beyond those above "
        "(comma-separated), or press Enter to skip.[/]")
    extra = questionary.text("Extra term(s) to add (comma-separated, optional):", default="").ask() or ""
    picked = list(picked) + [t.strip() for t in extra.split(",") if t.strip()]

    if cfg.keyword_blocks:
        new_blocks = list(cfg.keyword_blocks) + [[t] for t in picked]
        next_cfg = replace(cfg, keyword_blocks=new_blocks)
    else:
        next_cfg = replace(cfg, keywords=list(cfg.keywords) + picked)
    next_query = next_cfg.search_query()
    state.state[f"phase_{phase + 1}_query"] = next_query
    console.print(f"[green]Next phase query:[/] {next_query}")

    # The stage key stays "snowball" for resume compatibility even though the
    # user-facing label and provenance event call it query expansion.
    state.mark_stage(phase, "snowball", counts={"n_added": len(picked)})
    prov.log("query_expansion", from_phase=phase, added_keywords=picked)

    state.state["current_phase"] = phase + 1
    state.save()


# ---------------------------------------------------------------------------
# D. consolidation menu
# ---------------------------------------------------------------------------
def _merge_included_across_phases(state: RunState, cfg: ReviewConfig) -> pd.DataFrame:
    frames = []
    for phase in range(1, cfg.n_phases + 1):
        pdir = state.phase_dir(phase)
        screening_df = _read_csv_safe(pdir / "screening.csv")
        if screening_df.empty or "ta_decision" not in screening_df.columns:
            continue
        included = screening_df[ta_proceeds_mask(screening_df["ta_decision"])].copy()
        if included.empty:
            continue

        dedup_df = _read_csv_safe(pdir / "candidates_dedup.csv")
        if not dedup_df.empty and "id" in dedup_df.columns:
            url_by_id = dict(zip(dedup_df["id"], dedup_df.get("url", "")))
            authors_by_id = dict(zip(dedup_df["id"], dedup_df.get("authors", "")))
            included["url"] = included["id"].map(url_by_id).fillna("")
            included["authors"] = included["id"].map(authors_by_id).fillna("")
        else:
            included["url"] = ""
            included["authors"] = ""
        included["id"] = [f"p{phase}_{i}" for i in included["id"]]
        included["phase"] = phase
        frames.append(included)

    if not frames:
        return pd.DataFrame(columns=[
            "id", "title", "year", "venue", "doi", "ta_decision", "ta_reason",
            "ft_decision", "ft_reason", "reviewer", "url", "authors", "phase",
        ])

    merged = pd.concat(frames, ignore_index=True)
    merged["record_key"] = merged.apply(
        lambda r: record_key(r.get("doi", ""), r.get("title", "")), axis=1,
    )
    return merged.drop_duplicates(subset="record_key", keep="first").reset_index(drop=True)


def _is_merge_stale(state: RunState, cfg: ReviewConfig, included_path: Path) -> bool:
    if not included_path.exists():
        return True
    merged_mtime = included_path.stat().st_mtime
    for phase in range(1, cfg.n_phases + 1):
        screening_path = state.phase_dir(phase) / "screening.csv"
        if screening_path.exists() and screening_path.stat().st_mtime > merged_mtime:
            return True
    return False


def _ensure_merged(state: RunState, cfg: ReviewConfig, console: Console, force: bool = False) -> pd.DataFrame:
    run_dir = state.run_dir
    included_path = run_dir / "included_final.csv"
    final_dedup_path = run_dir / "final_dedup.csv"

    if not force and included_path.exists() and not _is_merge_stale(state, cfg, included_path):
        return _read_csv_safe(included_path)

    if included_path.exists() and _is_merge_stale(state, cfg, included_path) and not force:
        console.print("[dim]Screening decisions changed since the last merge -- "
                       "rebuilding included_final.csv[/]")

    merged = _merge_included_across_phases(state, cfg)
    merged.to_csv(included_path, index=False, encoding="utf-8")

    dedup_cols = ["id", "doi", "url", "title", "authors", "year", "venue"]
    present = [c for c in dedup_cols if c in merged.columns]
    merged[present].to_csv(final_dedup_path, index=False, encoding="utf-8")

    console.print(f"[green]Merged[/] {len(merged)} included studies across {cfg.n_phases} phase(s) "
                  f"-> {included_path.name}")
    return merged


def _menu_merge(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    merged = _ensure_merged(state, cfg, console, force=True)
    console.print(f"[bold]{len(merged)}[/] unique TA-included studies written to "
                  f"{state.run_dir / 'included_final.csv'}")
    console.print("[dim]These passed title/abstract screening only. They are not yet "
                  "'included studies' -- run full-text screening next.[/]")
    prov.log("merge_included", n_included=len(merged))


def _menu_full_text_screening(state: RunState, cfg: ReviewConfig, prov: Provenance,
                               console: Console) -> None:
    included_path = state.run_dir / "included_final.csv"
    merged = _ensure_merged(state, cfg, console)
    if merged.empty:
        console.print("[yellow]No TA-included studies to assess yet.[/]")
        return

    undecided = merged[
        merged.get("ft_decision", pd.Series(dtype=object)).isna() |
        (merged.get("ft_decision", pd.Series(dtype=object)).astype(str).str.strip() == "")
    ] if "ft_decision" in merged.columns else merged

    console.print(Panel(
        f"{len(merged)} study/studies passed title/abstract screening.\n"
        f"{len(merged) - len(undecided)} already have a full-text decision; "
        f"{len(undecided)} still need one.\n\nRead each full text, then record INCLUDE or "
        f"EXCLUDE. An exclusion needs a reason: PRISMA item 16b requires you to cite the "
        f"reports you excluded at full text and say why.",
        title="Full-text eligibility assessment", border_style="cyan"))

    if undecided.empty:
        console.print("[green]All studies already have a full-text decision.[/]")
    else:
        choice = questionary.select(
            "How do you want to record full-text decisions?",
            choices=[
                "Go through them here, one at a time",
                f"I'll edit ft_decision / ft_reason in {included_path.name} myself",
                "Back",
            ],
        ).ask()
        if choice is None or choice == "Back":
            return
        if choice.startswith("I'll edit"):
            console.print(f"[dim]Edit the ft_decision column in {included_path} (include / "
                           f"exclude) and ft_reason for every exclusion, then run this step "
                           f"again to record it in the audit trail.[/]")
            return

        for col in ("ft_decision", "ft_reason"):
            if col in merged.columns:
                merged[col] = merged[col].astype(object)
            else:
                merged[col] = ""

        n_done = 0
        for idx in undecided.index:
            row = merged.loc[idx]
            console.print(Panel(
                f"[bold]{_disp(row.get('title', ''))}[/]\n{_disp(row.get('authors', ''))}\n"
                f"{_disp(row.get('year', ''))}  {_disp(row.get('venue', ''))}\n"
                f"doi: {_disp(row.get('doi', ''))}\nurl: {_disp(row.get('url', ''))}",
                title=f"[{_disp(row.get('id', ''))}]  ({n_done + 1} of {len(undecided)})",
                border_style="white"))
            decision = questionary.select(
                "Full-text decision:",
                choices=["include", "exclude", "skip (decide later)", "stop for now"],
            ).ask()
            if decision is None or decision == "stop for now":
                break
            if decision.startswith("skip"):
                continue
            reason = ""
            if decision == "exclude":
                reason = questionary.text(
                    "Reason for excluding (required -- PRISMA item 16b):",
                    validate=lambda s: True if s.strip() else "A reason is required",
                ).ask()
                if reason is None:
                    break
            merged.at[idx, "ft_decision"] = decision
            merged.at[idx, "ft_reason"] = reason
            state.record_decision(
                record_key=record_key(row.get("doi", ""), row.get("title", "")),
                id=_native(row.get("id")), decision=decision, stage="ft", phase=0,
                reason=reason, source="human-review",
                title=row.get("title", ""), doi=row.get("doi", ""),
            )
            n_done += 1

        merged.to_csv(included_path, index=False, encoding="utf-8")
        console.print(f"[green]Recorded {n_done} full-text decision(s).[/]")

    final = _read_csv_safe(included_path)
    ft = final.get("ft_decision", pd.Series(dtype=object)).astype(str).str.strip().str.lower()
    n_inc = int(ft.eq("include").sum())
    n_exc = int(ft.eq("exclude").sum())
    n_left = len(final) - n_inc - n_exc
    console.print(f"[bold]{n_inc}[/] included after full text, [bold]{n_exc}[/] excluded, "
                  f"[bold]{n_left}[/] still undecided.")
    prov.log("full_text_screening", n_included=n_inc, n_excluded=n_exc, n_undecided=n_left)

    if n_left:
        console.print(Panel(
            f"{n_left} record(s) still have no full-text decision, so full-text "
            f"screening is NOT marked complete.\n\nRead the rest and record a "
            f"decision, then re-run this step.",
            title="[yellow]Full-text screening incomplete[/]", border_style="yellow",
        ))
        prov.log("full_text_screening_incomplete", n_undecided=n_left)
        return

    state.mark_stage(1, "full_text_screening", counts={"n_included": n_inc, "n_excluded": n_exc})


def _menu_download(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    _ensure_merged(state, cfg, console)
    run_dir = state.run_dir
    final_dedup_path = run_dir / "final_dedup.csv"
    outdir = run_dir / "pdfs"
    report_path = run_dir / "manual_download_needed.csv"
    log_path = run_dir / "download_log.csv"

    cmd = [
        sys.executable, _script_path("download.py"),
        "--in", str(final_dedup_path),
        "--mailto", cfg.mailto,
        "--outdir", str(outdir),
        "--report", str(report_path),
        "--log", str(log_path),
    ]
    if run_subprocess(cmd, console, "Downloading PDFs for the final set",
                       secret_env=cfg.secret_env()):
        manual = _read_csv_safe(report_path)
        total = _read_csv_safe(final_dedup_path)
        n_manual = len(manual)
        n_downloaded = max(len(total) - n_manual, 0)
        console.print(f"[bold]{n_downloaded}[/] downloaded, [bold]{n_manual}[/] need manual "
                      f"retrieval -> {report_path}")
        prov.log("download_pdfs", n_downloaded=n_downloaded, n_manual=n_manual)


def _menu_citation_snowball(state: RunState, cfg: ReviewConfig, prov: Provenance,
                             console: Console) -> None:
    merged = _ensure_merged(state, cfg, console)
    if merged.empty:
        console.print("[yellow]No included studies yet to snowball from -- run a search "
                       "phase and the review gate first.[/]")
        return

    direction = questionary.select(
        "Snowball direction:", choices=["both", "backward", "forward"]).ask()
    if direction is None:
        return
    max_per_seed = _ask_int("Max references/citations to pull per seed study:", default=50, minimum=1)
    if max_per_seed is None:
        return

    snowball_dir = state.run_dir / "snowball"
    snowball_dir.mkdir(exist_ok=True)
    candidates_path = snowball_dir / "candidates.csv"
    cmd = [
        sys.executable, _script_path("snowball.py"),
        "--seeds", str(state.run_dir / "final_dedup.csv"),
        "--mailto", cfg.mailto,
        "--direction", direction,
        "--max-per-seed", str(max_per_seed),
        "--out", str(candidates_path),
    ]
    if not run_subprocess(cmd, console, "Citation snowballing (backward/forward)"):
        return

    n_found = len(_read_csv_safe(candidates_path))
    if n_found == 0:
        console.print("[yellow]No new citation candidates found.[/]")
        prov.log("citation_snowball", direction=direction, n_found=0)
        return

    dedup_path = snowball_dir / "candidates_dedup.csv"
    cmd = [
        sys.executable, _script_path("dedup.py"),
        "--in", str(candidates_path), "--out", str(dedup_path),
        "--title-threshold", str(cfg.title_threshold),
    ]
    if not run_subprocess(cmd, console, "De-duplicating snowball candidates"):
        return

    screening_path = snowball_dir / "screening.csv"
    cmd = [
        sys.executable, _script_path("screen.py"),
        "--in", str(dedup_path), "--out", str(screening_path),
    ]
    if not run_subprocess(cmd, console, "Building snowball screening sheet"):
        return

    n_unique = len(_read_csv_safe(dedup_path))
    console.print(
        f"[green]{n_unique}[/] unique citation candidate(s) -> {screening_path}\n"
        f"Screen this file the same way you screened the main search (edit ta_decision by "
        f"hand, or point scripts/assist.py at it), then use 'Merge citation-snowball "
        f"results' to fold the includes into the review's included set.")
    prov.log("citation_snowball", direction=direction, n_found=n_found, n_unique=n_unique)


def _menu_merge_snowball(state: RunState, cfg: ReviewConfig, prov: Provenance,
                          console: Console) -> None:
    snowball_screening = state.run_dir / "snowball" / "screening.csv"
    screening_df = _read_csv_safe(snowball_screening)
    if screening_df.empty or "ta_decision" not in screening_df.columns:
        console.print("[yellow]No snowball screening sheet found -- run 'Citation "
                       "snowballing' first.[/]")
        return

    proceed = screening_df[ta_proceeds_mask(screening_df["ta_decision"])].copy()
    if proceed.empty:
        console.print("[yellow]No included/maybe rows in the snowball screening sheet.[/]")
        return

    included_path = state.run_dir / "included_final.csv"
    existing = _read_csv_safe(included_path)
    if existing.empty:
        console.print("[yellow]No included_final.csv yet -- run 'Merge TA-included "
                       "studies across phases' first.[/]")
        return

    proceed["id"] = [f"sb_{i}" for i in proceed["id"]]
    proceed["phase"] = "snowball"
    for col in ("ft_decision", "ft_reason"):
        if col not in proceed.columns:
            proceed[col] = ""

    existing_keys = set(existing.apply(
        lambda r: record_key(r.get("doi", ""), r.get("title", "")), axis=1))
    proceed["record_key"] = proceed.apply(
        lambda r: record_key(r.get("doi", ""), r.get("title", "")), axis=1)
    new_rows = proceed[~proceed["record_key"].isin(existing_keys)].drop(columns=["record_key"])
    n_dupe = len(proceed) - len(new_rows)

    if new_rows.empty:
        console.print(f"[yellow]Every snowball include is already in the included set "
                       f"(record_key match) -- {n_dupe} duplicate(s), nothing added.[/]")
        return

    common_cols = [c for c in existing.columns if c in new_rows.columns]
    combined = pd.concat([existing, new_rows[common_cols]], ignore_index=True)
    combined.to_csv(included_path, index=False, encoding="utf-8")
    console.print(f"[green]Added {len(new_rows)} citation-snowball study/studies[/] to "
                  f"included_final.csv ({n_dupe} were already present).")
    prov.log("merge_citation_snowball", n_added=len(new_rows), n_duplicate=n_dupe)


def _menu_verify_citations(state: RunState, cfg: ReviewConfig, prov: Provenance,
                            console: Console) -> None:
    _ensure_merged(state, cfg, console)
    run_dir = state.run_dir
    cmd = [
        sys.executable, _script_path("verify_citations.py"),
        "--csv", str(run_dir / "included_final.csv"),
        "--out", str(run_dir / "citation_verification.csv"),
    ]
    # returncode 1 just means some DOIs failed verification -- still a completed run;
    # only a non-0/1 code means the guard itself did not run.
    if not run_subprocess(cmd, console, "Verifying citation DOIs", success_codes=(0, 1)):
        console.print("[red]Citation verification did not complete.[/] Do not treat this "
                       "as a pass -- the fabricated-citation guard has not run.")
        prov.log("citation_verification", status="did_not_run")
        return

    results = _read_csv_safe(run_dir / "citation_verification.csv")
    if results.empty or "status" not in results.columns:
        console.print("[yellow]No verification results were written to "
                       "citation_verification.csv[/] -- the guard has not actually run.")
        prov.log("citation_verification", status="no_results")
        return

    statuses = results["status"].astype(str).str.strip().str.upper()
    n_failed = int(statuses.eq("FAIL").sum())
    prov.log("citation_verification", status="ran", n_checked=len(results), n_failed=n_failed)
    if n_failed:
        console.print(f"[red]{n_failed} citation(s) FAILED verification[/] -- these do not "
                       f"match Crossref and must not enter your manuscript unchecked. See "
                       f"citation_verification.csv.")
    else:
        console.print(f"[green]All {len(results)} checked citation(s) verified[/] against "
                      f"Crossref.")


def _menu_extract(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    _ensure_merged(state, cfg, console)
    run_dir = state.run_dir
    cmd = [
        sys.executable, _script_path("extract.py"),
        "--in", str(run_dir / "included_final.csv"),
        "--out", str(run_dir / "extraction.csv"),
        "--candidates", str(run_dir / "final_dedup.csv"),
    ]
    if cfg.primary_study_instruments:
        cmd += ["--instruments", ",".join(cfg.primary_study_instruments)]
    if run_subprocess(cmd, console, "Building extraction sheet"):
        n_rows = len(_read_csv_safe(run_dir / "extraction.csv"))
        prov.log("build_extraction_sheet", n_rows=n_rows)


_EXTRACTION_EDIT_FIELDS = [
    "thematic_class", "study_type", "contribution", "key_findings",
    "rq_mapping", "limitations", "venue_tier", "R", "A", "T", "C",
    "quality_tier", "notes",
]
_EXTRACTION_ENUM_CHOICES = {
    "venue_tier": ["T1", "T2", "T3"],
    "R": ["L", "S", "H"], "A": ["L", "S", "H"], "T": ["L", "S", "H"], "C": ["L", "S", "H"],
    "quality_tier": ["A", "B", "C"],
}


def _menu_review_extraction(state: RunState, cfg: ReviewConfig, prov: Provenance,
                             console: Console) -> None:
    """Guided, one-record-at-a-time editor for extraction.csv -- the intended
    alternative to hand-editing the CSV. venue_tier/R/A/T/C/quality_tier are
    edited through a select menu so an invalid value cannot be typed; free-text
    fields keep the current value as the default so pressing Enter never blanks
    them. Every change is logged to provenance, closing the gap where extraction
    edits previously left no audit trail (unlike screening decisions). Editing
    R/A/T/C recomputes quality_tier via compute_quality_tier() (README Stage 5's
    mechanical formula); editing quality_tier directly is treated as a manual
    override and logged as such when it disagrees with the computed value."""
    extraction_path = state.run_dir / "extraction.csv"
    df = _read_csv_safe(extraction_path)
    if df.empty:
        console.print("[yellow]No extraction.csv yet -- run 'Build extraction sheet' first.[/]")
        return

    for field in _EXTRACTION_EDIT_FIELDS + ["extraction_reviewer", "extraction_date"]:
        if field in df.columns:
            df[field] = df[field].astype(object)
        else:
            df[field] = ""

    reviewer = cfg.reviewer or "human-review"

    while True:
        table = Table(title="Extraction records")
        for col in ("id", "title", "venue_tier", "R", "A", "T", "C", "quality_tier"):
            table.add_column(col)
        for _, row in df.iterrows():
            table.add_row(
                _disp(row.get("id", "")), _disp(row.get("title", ""))[:50],
                _disp(row.get("venue_tier", "")), _disp(row.get("R", "")),
                _disp(row.get("A", "")), _disp(row.get("T", "")), _disp(row.get("C", "")),
                _disp(row.get("quality_tier", "")),
            )
        console.print(table)

        study_id = questionary.text(
            "Study id to review/correct (blank to finish):", default="").ask()
        if not study_id or not study_id.strip():
            break
        study_id = study_id.strip()

        matches = df.index[df["id"].astype(str).str.strip() == study_id]
        if len(matches) == 0:
            console.print(f"[red]No study with id '{study_id}'.[/]")
            continue
        idx = matches[0]

        while True:
            row = df.loc[idx]
            detail = "\n".join(
                f"[bold]{field}[/]: {_disp(row.get(field, '')) or '(blank)'}"
                for field in _EXTRACTION_EDIT_FIELDS
            )
            console.print(Panel(
                detail,
                title=f"[{_disp(row.get('id', ''))}] {_disp(row.get('title', ''))[:60]}",
                border_style="white",
            ))

            field = questionary.select(
                "Which field would you like to edit?",
                choices=_EXTRACTION_EDIT_FIELDS + ["Done with this study"],
            ).ask()
            if field is None or field == "Done with this study":
                break

            old_value = _disp(row.get(field, ""))
            if field in _EXTRACTION_ENUM_CHOICES:
                new_value = questionary.select(
                    f"{field} (currently: {old_value or '(blank)'}):",
                    choices=_EXTRACTION_ENUM_CHOICES[field],
                ).ask()
            else:
                new_value = questionary.text(f"{field}:", default=old_value).ask()
            if new_value is None:
                continue
            new_value = str(new_value).strip()

            if new_value == old_value:
                continue  # nothing changed -- nothing to write or log

            df.at[idx, field] = new_value
            prov.log(
                "extraction_field_edited", study_id=study_id, field=field,
                old_value=old_value, new_value=new_value, reviewer=reviewer,
            )
            console.print(f"[green]Updated {field}[/] for id {study_id}: "
                          f"'{old_value or '(blank)'}' -> '{new_value}'.")

            if field == "quality_tier":
                r, a, t, c = (df.at[idx, "R"], df.at[idx, "A"],
                              df.at[idx, "T"], df.at[idx, "C"])
                computed = compute_quality_tier(r, a, t, c)
                if computed is not None and new_value.upper() != computed:
                    console.print(
                        f"[yellow]This overrides the computed tier ('{computed}' from "
                        f"R/A/T/C) -- logged as a manual override.[/]")
                    prov.log(
                        "quality_tier_manual_override", study_id=study_id,
                        computed=computed, override=new_value, reviewer=reviewer,
                    )
            elif field in ("R", "A", "T", "C"):
                r, a, t, c = (df.at[idx, "R"], df.at[idx, "A"],
                              df.at[idx, "T"], df.at[idx, "C"])
                computed = compute_quality_tier(r, a, t, c)
                if computed is not None:
                    prior = _disp(df.at[idx, "quality_tier"])
                    df.at[idx, "quality_tier"] = computed
                    if prior and prior != computed:
                        console.print(
                            f"[yellow]quality_tier recomputed from R/A/T/C: "
                            f"'{prior}' -> '{computed}'.[/]")
                    prov.log(
                        "quality_tier_recomputed", study_id=study_id,
                        quality_tier=computed, r=r, a=a, t=t, c=c,
                    )

            df.at[idx, "extraction_reviewer"] = reviewer
            df.at[idx, "extraction_date"] = date.today().isoformat()
            df.to_csv(extraction_path, index=False, encoding="utf-8")


def _menu_figures(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    counts = _compute_prisma_counts(state, cfg)
    warnings = prisma_residuals(counts)
    for warning in warnings:
        console.print(f"[yellow]PRISMA check:[/] {warning}")
    if warnings and not questionary.confirm(
            "The PRISMA numbers above do not balance. Generate the figure anyway?",
            default=False).ask():
        console.print("[yellow]Skipped figure generation.[/]")
        prov.log("figures_skipped", reason="unbalanced PRISMA counts", warnings=warnings)
        return

    run_dir = state.run_dir
    outdir = run_dir / "figures"
    extraction_path = run_dir / "extraction.csv"

    cmd = [
        sys.executable, _script_path("figures.py"),
        "--run-dir", str(run_dir),
        "--quality", str(extraction_path),
        "--outdir", str(outdir),
        "--identified", str(counts["identified"]),
        "--duplicates-removed", str(counts["duplicates_removed"]),
        "--screened", str(counts["screened"]),
        "--excluded-ta", str(counts["excluded_ta"]),
        "--assessed-ft", str(counts["assessed_ft"]),
        "--excluded-ft", str(counts["excluded_ft"]),
        "--included", str(counts["included"]),
    ]
    if run_subprocess(cmd, console, "Generating PRISMA + tier figures"):
        prov.log("figures_generated", counts=counts, unbalanced_warnings=warnings)


def _menu_export_refs(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    run_dir = state.run_dir
    extraction_path = run_dir / "extraction.csv"
    included_path = run_dir / "included_final.csv"

    df = pd.DataFrame()
    src_name = ""
    if extraction_path.exists():
        df = _read_csv_safe(extraction_path)
        src_name = extraction_path.name
    if df.empty and included_path.exists():
        df = _read_csv_safe(included_path)
        src_name = included_path.name

    if df.empty:
        console.print("[yellow]No extraction.csv or included_final.csv found -- run "
                       "'Merge included studies' or 'Build extraction sheet' first.[/]")
        return

    total_before = len(df)
    filtered, used_col = pick_progressed(df, "ft_decision")
    if filtered.empty and total_before:
        console.print(f"[yellow]No rows progressed on '{used_col}' -- exporting all "
                       f"{total_before} row(s) unfiltered.[/]")
    else:
        df = filtered

    records = df.to_dict("records")
    bib = srp_export.to_bibtex(records)
    ris = srp_export.to_ris(records)
    (run_dir / "references.bib").write_text(bib, encoding="utf-8")
    (run_dir / "references.ris").write_text(ris, encoding="utf-8")
    console.print(f"[green]Wrote {len(records)} reference(s)[/] from {src_name} to "
                  f"references.bib / references.ris")
    prov.log("export_references", n_records=len(records), source=src_name)


def _menu_export_exclusions(state: RunState, cfg: ReviewConfig, prov: Provenance,
                             console: Console) -> None:
    included_path = state.run_dir / "included_final.csv"
    df = _read_csv_safe(included_path)
    if df.empty or "ft_decision" not in df.columns:
        console.print("[yellow]No full-text decisions recorded yet -- run 'Full-text "
                       "screening' first.[/]")
        return

    excluded = df[df["ft_decision"].astype(str).str.strip().str.lower().eq("exclude")]
    if excluded.empty:
        console.print("[yellow]No full-text exclusions recorded.[/]")
        return

    csv_path = state.run_dir / "excluded_full_text.csv"
    bib_path = state.run_dir / "excluded_full_text.bib"
    excluded.to_csv(csv_path, index=False, encoding="utf-8")
    bib_path.write_text(srp_export.to_bibtex(excluded.to_dict("records")), encoding="utf-8")
    console.print(f"[green]Wrote {len(excluded)} full-text exclusion(s)[/] to "
                  f"{csv_path.name} and {bib_path.name}")

    if "ft_reason" in excluded.columns:
        missing_reason = int((excluded["ft_reason"].isna() |
                               (excluded["ft_reason"].astype(str).str.strip() == "")).sum())
    else:
        missing_reason = len(excluded)
    if missing_reason:
        console.print(f"[yellow]{missing_reason} of them have no reason recorded[/] -- "
                       f"PRISMA item 16b requires a reason for every full-text exclusion.")
    prov.log("export_ft_exclusions", n_excluded=len(excluded), n_missing_reason=missing_reason)


def _menu_kappa(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    console.print(Panel(
        "Inter-rater agreement needs TWO independent screening sheets: two reviewers "
        "screen the same records without seeing each other's calls, then you compute "
        "kappa BEFORE reconciling. Kitchenham SS6.3 and Cochrane SS4.6.1 both require "
        "this; single-reviewer screening is a known validity threat, and a reviewer will "
        "ask for the number.\n\nTo produce a second sheet: copy this phase's "
        "screening.csv, have the second reviewer fill in ta_decision independently, and "
        "point this step at both files.",
        title="Cohen's kappa", border_style="cyan"))

    default_a = state.phase_dir(1) / "screening.csv"
    path_a = questionary.text("Reviewer A's screening CSV:",
                               default=str(default_a) if default_a.exists() else "").ask()
    if path_a is None:
        return
    path_b = questionary.text("Reviewer B's screening CSV:", default="").ask()
    if path_b is None:
        return
    stage = questionary.select("Which stage?", choices=["ta_decision", "ft_decision"]).ask()
    if stage is None:
        return

    df_a = _read_csv_safe(path_a)
    df_b = _read_csv_safe(path_b)
    if df_a.empty or df_b.empty:
        console.print("[red]Could not read one or both sheets (empty or missing).[/]")
        return
    if stage not in df_a.columns or stage not in df_b.columns:
        console.print(f"[red]Both sheets need a '{stage}' column.[/]")
        return

    rows_a = {_id_to_str(v): d for v, d in zip(df_a["id"], df_a[stage])} if "id" in df_a.columns else {}
    rows_b = {_id_to_str(v): d for v, d in zip(df_b["id"], df_b[stage])} if "id" in df_b.columns else {}
    result = compare_reviewers(rows_a, rows_b, stage_col=stage)

    console.print(Panel(result.summary(), title="Agreement", border_style="green"))
    if result.kappa is None:
        console.print(f"[yellow]{result.note}[/]")

    if result.labels:
        table = Table(title="Confusion matrix (rows = A, cols = B)")
        table.add_column("A \\ B")
        for label in result.labels:
            table.add_column(label, justify="right")
        for a in result.labels:
            table.add_row(a, *[str(result.matrix.get(a, {}).get(b, 0)) for b in result.labels])
        console.print(table)

    if result.conflicts:
        table = Table(title=f"Conflicts to reconcile ({len(result.conflicts)})")
        for col in ("id", "title", "A", "B"):
            table.add_column(col)
        for c in result.conflicts[:40]:
            table.add_row(str(c["id"]), _disp(c.get("title", ""))[:60],
                          c["reviewer_a"], c["reviewer_b"])
        console.print(table)

        out = state.run_dir / f"conflicts_{stage}.csv"
        pd.DataFrame(result.conflicts).to_csv(out, index=False, encoding="utf-8")
        console.print(f"[green]Wrote {len(result.conflicts)} conflict(s)[/] to {out.name} "
                      f"-- resolve these by discussion or a third reviewer, and say in "
                      f"your methods how you did it.")

    prov.log("inter_rater_agreement", stage=stage, n_compared=result.n_compared,
              n_agreed=result.n_agreed, kappa=result.kappa,
              percent_agreement=result.percent_agreement, n_conflicts=len(result.conflicts),
              sheet_a=str(path_a), sheet_b=str(path_b), note=result.note)


def _compute_prisma_counts(state: RunState, cfg: ReviewConfig) -> dict:
    """Thin wrapper: reads each phase's own CSVs plus the run-level
    included_final.csv off disk, then delegates the actual funnel-counting to
    srp.prisma.derive_prisma_counts_for_run -- the single implementation
    shared with scripts/figures.py's standalone --run-dir mode."""
    phases = [
        PhaseFrames(
            candidates=_read_csv_safe(state.phase_dir(phase) / "candidates.csv"),
            dedup=_read_csv_safe(state.phase_dir(phase) / "candidates_dedup.csv"),
            screening=_read_csv_safe(state.phase_dir(phase) / "screening.csv"),
        )
        for phase in range(1, cfg.n_phases + 1)
    ]
    included_final = _read_csv_safe(state.run_dir / "included_final.csv")
    return derive_prisma_counts_for_run(phases, included_final)


def _prisma_report_rows(counts: dict) -> dict:
    """Human-readable row labels for PROVENANCE.md's PRISMA table -- distinct
    from _compute_prisma_counts()'s machine-readable keys, which
    methods_report.py/figures.py consume directly."""
    return {
        "records identified (all phases)": counts.get("identified", 0),
        "duplicates removed": counts.get("duplicates_removed", 0),
        "records screened (TA)": counts.get("screened", 0),
        "excluded at TA": counts.get("excluded_ta", 0),
        "assessed at full text": counts.get("assessed_ft", 0),
        "excluded at full text": counts.get("excluded_ft", 0),
        "included (final merged set)": counts.get("included", 0),
    }


def _menu_provenance(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    counts = _compute_prisma_counts(state, cfg)
    out_path = state.run_dir / "PROVENANCE.md"
    prov.render_markdown(out_path, config=cfg.to_dict(),
                          prisma=_prisma_report_rows(counts))
    console.print(f"[green]Wrote[/] {out_path}")
    for warning in prisma_residuals(counts):
        console.print(f"[yellow]PRISMA check:[/] {warning}")


def _menu_diagnose_run(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    """Stage-by-stage funnel across every phase this run has touched: for a run
    that came back empty, this is the trail state.json and provenance.jsonl
    don't show on their own -- which stage first went to zero, and whether any
    stage marked 'done' is missing the file it should have produced (the two
    symptoms a 0-result, empty-folder run actually presents as)."""
    phase_nums = sorted({
        int(key.split(":", 1)[0]) for key in state.state.get("stages", {})
    })
    if not phase_nums:
        console.print("[yellow]No stages have run in this review yet.[/]")
        prov.log("diagnose_run", phases=[], file_mismatches=0)
        return

    file_mismatches = []
    for phase in phase_nums:
        pdir = state.run_dir / f"phase_{phase}"
        table = Table(title=f"Phase {phase}", show_lines=True)
        table.add_column("Stage")
        table.add_column("Status")
        table.add_column("Count")
        table.add_column("Output file")

        zero_flagged = False
        for stage_key, filename, count_key, label in _PHASE_FUNNEL_STAGES:
            status = state.stage_status(phase, stage_key)
            entry = state.state.get("stages", {}).get(f"{phase}:{stage_key}", {})
            count = entry.get("counts", {}).get(count_key)

            if filename is None:
                file_disp = "-"
            else:
                file_path = pdir / filename
                file_disp = filename if file_path.exists() else f"[red]{filename} MISSING[/]"
                if status == "done" and not file_path.exists():
                    file_mismatches.append((phase, label, file_path))

            status_disp = status if status else "[dim]not run[/]"
            if count is None:
                count_disp = "-"
            elif count == 0 and not zero_flagged:
                count_disp = f"[red]{count} <- first zero-drop stage[/]"
                zero_flagged = True
            else:
                count_disp = str(count)
            table.add_row(label, status_disp, count_disp, file_disp)

        console.print(table)

    if file_mismatches:
        console.print(Panel(
            "\n".join(f"Phase {p}: '{label}' is marked done, but {path} "
                      f"does not exist on disk." for p, label, path in file_mismatches),
            title="[red]Stage says done, but its output file is missing[/]",
            border_style="red",
        ))

    prov.log("diagnose_run", phases=phase_nums, file_mismatches=len(file_mismatches))


def _menu_manage_api_keys(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    """Add/update or delete .env-managed API keys, and show the .env path in
    use -- after a pipx install, that path isn't the repo you can see on
    GitHub, it's wherever you happened to run `slr` from (see srp.env.load_dotenv),
    and there was previously no way to discover or manage it short of finding
    the file by hand. Never prints a key's actual value, matching _ask_secret's
    own never-echo convention."""
    env_path = Path.cwd() / ".env"
    console.print(f"[dim]Using .env at:[/] {env_path}")
    file_values = parse_env_text(env_path.read_text(encoding="utf-8")) if env_path.is_file() else {}

    table = Table(title="API keys", show_lines=True)
    table.add_column("Source")
    table.add_column("Env var")
    table.add_column("Status")
    for label, var in _MANAGED_API_KEYS:
        present = bool(file_values.get(var, "").strip())
        table.add_row(label, var, "[green]set[/]" if present else "[dim]not set[/]")
    console.print(table)

    console.print("[dim]Arrow keys choose an action, Enter confirms.[/]")
    action = questionary.select(
        "What would you like to do?",
        choices=["Add or update a key", "Delete a key", "Delete ALL keys", "Back"],
    ).ask()
    if action is None or action == "Back":
        prov.log("manage_api_keys", action="none")
        return

    if action == "Add or update a key":
        console.print("[dim]Arrow keys choose a source, Enter confirms.[/]")
        var = questionary.select(
            "Which key?",
            choices=[questionary.Choice(f"{label} ({v})", value=v) for label, v in _MANAGED_API_KEYS],
        ).ask()
        if var is None:
            return
        value = (questionary.text(f"New value for {var}:", default="").ask() or "").strip()
        if not value:
            console.print("[yellow]No value entered -- nothing changed.[/]")
            return
        set_env_var(env_path, var, value)
        os.environ[var] = value
        console.print(f"[green]Saved[/] {var} to {env_path}")
        prov.log("manage_api_keys", action="set", key=var)

    elif action == "Delete a key":
        present = [(label, var) for label, var in _MANAGED_API_KEYS if file_values.get(var, "").strip()]
        if not present:
            console.print("[yellow]No keys are currently set in .env.[/]")
            return
        console.print("[dim]Arrow keys choose a source, Enter confirms.[/]")
        var = questionary.select(
            "Delete which key?",
            choices=[questionary.Choice(f"{label} ({v})", value=v) for label, v in present],
        ).ask()
        if var is None:
            return
        confirmed = questionary.confirm(f"Delete {var} from {env_path}?", default=False).ask()
        if not confirmed:
            return
        unset_env_var(env_path, var)
        os.environ.pop(var, None)
        console.print(f"[green]Deleted[/] {var} from {env_path}")
        prov.log("manage_api_keys", action="delete", key=var)

    elif action == "Delete ALL keys":
        confirmed = questionary.confirm(
            f"Delete ALL {len(_MANAGED_API_KEYS)} API key(s) from {env_path}? "
            f"This cannot be undone.", default=False).ask()
        if not confirmed:
            return
        for _, var in _MANAGED_API_KEYS:
            unset_env_var(env_path, var)
            os.environ.pop(var, None)
        console.print(f"[green]Deleted all API keys from[/] {env_path}")
        prov.log("manage_api_keys", action="delete_all")


def _edit_search_settings(cfg: ReviewConfig, console: Console) -> list:
    """Lets the user change the mechanical search parameters most likely to
    need correcting after a run came back empty -- mailto, year range,
    per-source cap, and which sources to query. Keyword blocks/topic aren't
    editable here: changing the actual search terms is a protocol decision,
    not an operational fix, and belongs in a fresh phase rather than a
    silent in-place edit to one that already ran. Mutates `cfg` directly and
    returns the field names actually changed."""
    console.print(
        f"[dim]Current: mailto={cfg.mailto or '(blank)'}, years={cfg.year_from}-{cfg.year_to}, "
        f"max_per_source={cfg.max_per_source}, sources={','.join(cfg.sources) or '(none)'}[/]")
    console.print("[dim]Space toggles a setting to change, Enter confirms.[/]")
    fields = questionary.checkbox(
        "Which setting(s) do you want to change?",
        choices=["mailto", "year_from", "year_to", "max_per_source", "sources"],
    ).ask() or []

    changed = []
    if "mailto" in fields:
        value = (questionary.text("New mailto:", default=cfg.mailto).ask() or "").strip()
        if value:
            cfg.mailto = value
            changed.append("mailto")
    if "year_from" in fields:
        value = _ask_int("New year_from:", default=cfg.year_from, minimum=1900)
        if value is not None:
            cfg.year_from = value
            changed.append("year_from")
    if "year_to" in fields:
        value = _ask_int("New year_to:", default=cfg.year_to, minimum=1900)
        if value is not None:
            cfg.year_to = value
            changed.append("year_to")
    if "max_per_source" in fields:
        value = _ask_int("New max_per_source:", default=cfg.max_per_source, minimum=1)
        if value is not None:
            cfg.max_per_source = value
            changed.append("max_per_source")
    if "sources" in fields:
        console.print("[dim]Space toggles a source, Enter confirms.[/]")
        picked = questionary.checkbox(
            "Sources to search:",
            choices=[questionary.Choice(name, checked=name in cfg.sources)
                     for name in _ALL_SOURCE_NAMES],
        ).ask()
        if picked:
            cfg.sources = picked
            changed.append("sources")
    return changed


def _menu_rerun_search(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    """Re-runs a phase's search and continues it through dedup, prescreen,
    AI-assist TA screening, and the human review gate -- the same sequence a
    normal phase run goes through -- either with the current settings or
    after editing them first. This is the fix half of 'Diagnose this run':
    that action can tell you a phase came back empty, this is how you
    actually retry it and screen the results, from the menu, without
    abandoning the run and starting over. Continuing all the way through
    screening (rather than stopping after search) matters because the
    consolidation menu itself has no other way to reach AI-assist screening
    or the review gate -- those otherwise only run inside the phase loop."""
    phase_nums = sorted({
        int(key.split(":", 1)[0]) for key in state.state.get("stages", {})
        if key.split(":", 1)[1] == "search"
    })
    if not phase_nums:
        console.print("[yellow]No phase has run a search yet -- nothing to re-run. "
                       "Start or resume a review to run the first search.[/]")
        prov.log("rerun_search", phase=None, changed_fields=[], confirmed=False)
        return

    console.print("[dim]Arrow keys choose a phase, Enter confirms.[/]")
    phase = questionary.select(
        "Re-run search for which phase?",
        choices=[questionary.Choice(f"Phase {p}", value=p) for p in phase_nums],
    ).ask()
    if phase is None:
        return

    pdir = state.phase_dir(phase)
    screening_df = _read_csv_safe(pdir / "screening.csv")
    has_decisions = (not screening_df.empty and "ta_decision" in screening_df.columns
                      and (screening_df["ta_decision"].astype(str).str.strip() != "").any())

    warning = (f"Re-running search for phase {phase} overwrites candidates.csv, "
               f"candidates_dedup.csv, and screening.csv in {pdir}.")
    if has_decisions:
        warning += (f" Phase {phase}'s screening.csv already has recorded title/abstract "
                     f"decisions -- those will be LOST.")
    console.print(f"[yellow]{warning}[/]")
    confirmed = questionary.confirm(
        f"Continue re-running phase {phase}'s search?", default=False).ask()
    if not confirmed:
        prov.log("rerun_search", phase=phase, changed_fields=[], confirmed=False)
        return

    console.print("[dim]Arrow keys choose an option, Enter confirms.[/]")
    mode = questionary.select(
        "Re-run with the current settings, or change something first?",
        choices=["Re-run with current settings (no changes)", "Edit settings, then re-run"],
    ).ask()
    if mode is None:
        return

    changed_fields = []
    if mode == "Edit settings, then re-run":
        changed_fields = _edit_search_settings(cfg, console)
        if changed_fields:
            state.save_config(cfg.to_dict())
            console.print(f"[green]Updated[/] {', '.join(changed_fields)} for this run.")

    query = cfg.search_query()
    state.state[f"phase_{phase}_query"] = query
    state.save()

    # All five stages are cleared upfront -- search/dedup/prescreen so
    # _run_search_dedup_prescreen's own _should_run_stage() check treats them
    # as pending rather than prompting ANOTHER "already done -- re-run?"
    # confirm on top of the one just given; assist_ta/review_gate because
    # whatever they'd previously recorded described the OLD candidate set.
    for stage in ("search", "dedup", "prescreen", "assist_ta", "review_gate"):
        state.state.get("stages", {}).pop(f"{phase}:{stage}", None)
    state.save()

    _run_search_dedup_prescreen(state, cfg, prov, console, phase, pdir, query)

    # Continue through the same steps a normal phase run goes through -- the
    # consolidation menu itself has no other action that reaches these, so
    # stopping after search would leave a freshly re-populated screening.csv
    # with nothing offering to screen it.
    if _should_run_stage(state, phase, "assist_ta", "AI-assist TA screening"):
        run_ai_assist_loop(state, cfg, prov, phase, pdir, console)
    if _should_run_stage(state, phase, "review_gate", "Human review gate"):
        run_review_gate(state, cfg, prov, phase, pdir, console)

    n_hits = state.state.get("stages", {}).get(f"{phase}:search", {}).get("counts", {}).get("n_hits")
    outcome = "done" if state.stage_status(phase, "search") == "done" else "search_failed"
    prov.log("rerun_search", phase=phase, changed_fields=changed_fields, confirmed=True,
              outcome=outcome, n_hits=n_hits)


# --- core logic ---
def _load_search_strategy_rows(state: RunState, cfg: ReviewConfig) -> list:
    """Read every phase's search_strategy.csv (written by search.py, see
    scripts/search.py's SourceReport) into PhaseSearchRecord objects."""
    phases = []
    for phase in range(1, cfg.n_phases + 1):
        strategy_path = state.phase_dir(phase) / "search_strategy.csv"
        df = _read_csv_safe(strategy_path)
        rows = []
        for _, r in df.iterrows():
            total = r.get("total_available")
            try:
                total = int(total) if pd.notna(total) else None
            except (TypeError, ValueError):
                total = None
            rows.append(SourceStrategyRow(
                source=_disp(r.get("source", "")),
                query_sent=_disp(r.get("query_sent", "")),
                retrieved_at=_disp(r.get("retrieved_at", "")),
                n_retrieved=int(r.get("n_retrieved") or 0),
                total_available=total,
                truncated=bool(r.get("truncated", False)),
                status=_disp(r.get("status", "")) or "ok",
            ))
        label = f"Phase {phase}" if phase == 1 else f"Phase {phase} (query expansion)"
        phases.append(PhaseSearchRecord(phase=phase, label=label, rows=rows))
    return phases


def _menu_methods_report(state: RunState, cfg: ReviewConfig, prov: Provenance,
                          console: Console) -> None:
    """Draft the manuscript's Search-methods paragraph and its supplementary
    tables straight from the pipeline's own recorded search strategy and PRISMA
    counts -- see srp/methods_report.py for why this exists."""
    phases = _load_search_strategy_rows(state, cfg)
    counts = _compute_prisma_counts(state, cfg)

    paragraph = render_search_methods(cfg.to_dict(), phases, counts)
    strategy_table = render_search_strategy_table(phases)

    ft_reasons = counts.get("ft_reasons") or {}
    if ft_reasons:
        reasons_table = "\n".join(["| Reason | Count |", "|---|---|"]
                                   + [f"| {k} | {v} |" for k, v in
                                      sorted(ft_reasons.items(), key=lambda kv: -kv[1])])
    else:
        reasons_table = "_(no full-text exclusions recorded yet)_"

    out_path = state.run_dir / "search_methods.md"
    out_path.write_text(
        "# Search methods (auto-drafted -- read before submitting)\n\n"
        "This paragraph and the tables below are drafted directly from this run's "
        "recorded search strategy and screening counts. Review it: it states what "
        "the pipeline recorded, not necessarily everything your methods section "
        "should say.\n\n"
        "## Methods paragraph\n\n" + paragraph + "\n\n"
        "## Table S1: per-source search strategy\n\n" + strategy_table + "\n\n"
        "## Table S2: full-text exclusion reasons\n\n" + reasons_table + "\n",
        encoding="utf-8",
    )
    console.print(Panel(paragraph, title="Draft methods paragraph", border_style="cyan"))
    console.print(f"[green]Wrote[/] {out_path} (paragraph + Table S1 + Table S2)")
    prov.log("methods_report_drafted", n_phases=len(phases))


def _menu_review_self_appraisal(state: RunState, cfg: ReviewConfig, prov: Provenance,
                                 console: Console) -> None:
    """Write a fillable review-self-appraisal checklist (AMSTAR 2 / ROBIS / DARE /
    MECCIR) -- the review-level counterpart to extract.py's per-study instrument
    columns. A review-level instrument appraises the review itself, once, so it
    has no per-study rows to attach to extraction.csv; this writes its own file."""
    review_level_keys = [k for k, inst in INSTRUMENTS.items() if inst.level == REVIEW_SELF_CHECK]
    key = cfg.review_level_instrument
    inst = INSTRUMENTS.get(key)
    if inst is None or inst.level != REVIEW_SELF_CHECK:
        console.print("[yellow]No review-level self-check instrument is recorded for "
                      "this review (normally set during the setup wizard's "
                      "field-selection step).[/]")
        name_choices = [INSTRUMENTS[k].name for k in review_level_keys]
        picked_name = questionary.select(
            "Pick a review-level instrument to write a checklist for:",
            choices=name_choices + ["Skip"],
        ).ask()
        if picked_name is None or picked_name == "Skip":
            return
        key = next(k for k in review_level_keys if INSTRUMENTS[k].name == picked_name)

    checklist = render_review_self_appraisal(key)
    out_path = state.run_dir / f"review_self_appraisal_{key}.md"
    out_path.write_text(checklist, encoding="utf-8")
    console.print(f"[green]Wrote[/] {out_path} -- fill in Rating/Justification by "
                  f"hand against the finished manuscript.")
    prov.log("review_self_appraisal_drafted", instrument=key)


def _print_closing_panel(state: RunState, console: Console) -> None:
    run_dir = state.run_dir
    lines = [f"Run directory: {run_dir}"]
    for name in ("included_final.csv", "final_dedup.csv", "citation_verification.csv",
                 "extraction.csv", "manual_download_needed.csv", "references.bib",
                 "references.ris", "PROVENANCE.md"):
        if (run_dir / name).exists():
            lines.append(f"  {name}")
    if (run_dir / "figures").exists():
        lines.append("  figures/")
    if (run_dir / "pdfs").exists():
        lines.append("  pdfs/")
    console.print(Panel("\n".join(lines), title="Review outputs", border_style="green"))


# One-line description per consolidation-menu action, keyed by that action's
# exact label -- shown once as a legend before the menu loop starts. Where a
# 0/empty result is a normal, non-broken outcome (e.g. most sets are
# closed-access, so 0 downloads is common), that gets said explicitly, so a
# quiet zero isn't mistaken for a failure.
_CONSOLIDATION_ACTION_HELP = {
    "Merge TA-included studies across phases":
        "Combines every phase's title/abstract-included records into one set for full-text "
        "screening. 0 merged usually means no phase has passed its review gate yet.",
    "Citation snowballing (backward + forward, Wohlin 2014)":
        "Follows references and citations of the current included set to find more candidates "
        "-- PRISMA-style snowballing, not query expansion.",
    "Merge citation-snowball results into included set":
        "Folds screened snowball candidates into the main included set. Run snowballing and "
        "screen its output first.",
    "Download PDFs for the final set":
        "Resolves and downloads open-access PDFs via Unpaywall/direct links. 0 downloaded "
        "usually means the set is mostly closed-access, not a failure -- see "
        "manual_download_needed.csv.",
    "Full-text screening (record eligibility decisions)":
        "Records include/exclude decisions on the full text of every TA-included study.",
    "Verify citations (DOIs)":
        "Cross-checks claimed titles against what each DOI actually resolves to on Crossref, "
        "to catch fabricated or mismatched references.",
    "Build extraction sheet (for human quality coding)":
        "Creates the blank per-study coding template (thematic class, R/A/T/C, quality tier) "
        "for full-text-included studies.",
    "Review/correct an extraction record (venue tier, R/A/T/C, quality tier, ...)":
        "Edit one study's extraction fields by id, after the sheet already exists.",
    "Generate PRISMA + tier figures":
        "Renders the PRISMA flow diagram plus quality-/venue-tier charts from the current "
        "screening and extraction data.",
    "Export references (BibTeX + RIS)":
        "Writes citation files for the final included set, for a reference manager.",
    "Export full-text exclusions with reasons (PRISMA 16b)":
        "Writes the PRISMA-required table of full-text-excluded studies and why.",
    "Inter-rater agreement (Cohen's kappa)":
        "Computes agreement between two reviewers' decisions on the same records.",
    "Draft methods paragraph (search counts, ready to quote)":
        "Auto-drafts a methods paragraph and per-source search-strategy table from this run's "
        "actual counts.",
    "Write review self-appraisal checklist (AMSTAR 2/ROBIS/DARE/MECCIR)":
        "Writes a self-appraisal checklist for the review itself, matched to your configured "
        "field.",
    "Write provenance report":
        "Writes PROVENANCE.md: every stage's counts and the PRISMA numbers derived from them.",
    "Diagnose this run (why did a stage return 0 / where did files go)":
        "Per-phase funnel of stage counts; flags the first stage that returned zero and any "
        "stage marked done whose output file is missing. Start here if a run came back empty.",
    "Re-run a phase's search (with or without changes)":
        "Re-runs search through the review gate for one phase, as-is or after editing "
        "mailto/year range/sources/max-per-source first. Existing screening decisions for "
        "that phase are lost -- always confirms before doing anything.",
    "Manage API keys (.env location, add/update/delete)":
        "Shows the .env path in use and lets you add, update, or delete API keys stored there.",
    "Check for updates":
        "Checks GitHub for a newer release and reports the result -- up to date, outdated, or "
        "unreachable.",
}


def consolidation_menu(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    # Ordered as the method runs: merge the TA-includes, get their full texts, screen
    # them at full text, and only then extract / count / export. Full-text screening
    # sits between download and extraction because that is where PRISMA puts it, and
    # because everything after it is only defensible once it has happened.
    actions = {
        "Merge TA-included studies across phases": lambda: _menu_merge(state, cfg, prov, console),
        "Citation snowballing (backward + forward, Wohlin 2014)":
            lambda: _menu_citation_snowball(state, cfg, prov, console),
        "Merge citation-snowball results into included set":
            lambda: _menu_merge_snowball(state, cfg, prov, console),
        "Download PDFs for the final set": lambda: _menu_download(state, cfg, prov, console),
        "Full-text screening (record eligibility decisions)":
            lambda: _menu_full_text_screening(state, cfg, prov, console),
        "Verify citations (DOIs)": lambda: _menu_verify_citations(state, cfg, prov, console),
        "Build extraction sheet (for human quality coding)":
            lambda: _menu_extract(state, cfg, prov, console),
        "Review/correct an extraction record (venue tier, R/A/T/C, quality tier, ...)":
            lambda: _menu_review_extraction(state, cfg, prov, console),
        "Generate PRISMA + tier figures": lambda: _menu_figures(state, cfg, prov, console),
        "Export references (BibTeX + RIS)": lambda: _menu_export_refs(state, cfg, prov, console),
        "Export full-text exclusions with reasons (PRISMA 16b)":
            lambda: _menu_export_exclusions(state, cfg, prov, console),
        "Inter-rater agreement (Cohen's kappa)": lambda: _menu_kappa(state, cfg, prov, console),
        "Draft methods paragraph (search counts, ready to quote)":
            lambda: _menu_methods_report(state, cfg, prov, console),
        "Write review self-appraisal checklist (AMSTAR 2/ROBIS/DARE/MECCIR)":
            lambda: _menu_review_self_appraisal(state, cfg, prov, console),
        "Write provenance report": lambda: _menu_provenance(state, cfg, prov, console),
        "Diagnose this run (why did a stage return 0 / where did files go)":
            lambda: _menu_diagnose_run(state, cfg, prov, console),
        "Re-run a phase's search (with or without changes)":
            lambda: _menu_rerun_search(state, cfg, prov, console),
        "Manage API keys (.env location, add/update/delete)":
            lambda: _menu_manage_api_keys(state, cfg, prov, console),
        "Check for updates": lambda: _menu_check_for_updates(state, cfg, prov, console),
    }

    console.rule("[bold]Consolidation[/]")
    legend = Table(title="What each action does", show_lines=True)
    legend.add_column("Action")
    legend.add_column("What it does")
    for label in actions:
        legend.add_row(label, _CONSOLIDATION_ACTION_HELP.get(label, "[no description]"))
    console.print(legend)

    while True:
        choice = questionary.select("What would you like to do?", choices=list(actions) + ["Finish"]).ask()
        if choice is None or choice == "Finish":
            break
        actions[choice]()

    _print_closing_panel(state, console)


def _update_status(version: str) -> "tuple[str, str | None]":
    """Shared classification behind both the startup line and the on-demand
    menu action: one of 'dev_build', 'check_failed', 'up_to_date', 'outdated',
    plus the latest release tag when known. A source/dev checkout is never
    flagged outdated -- it can be ahead of the last tag, not behind it -- and
    a failed check is never conflated with 'up to date'."""
    if version.startswith("0.0.0-dev"):
        return "dev_build", None
    latest = latest_release_version()
    if latest is None:
        return "check_failed", None
    if is_newer(latest, version):
        return "outdated", latest
    return "up_to_date", latest


def _maybe_print_update_notice(console: Console) -> None:
    """Non-blocking startup line: always shows the running version, so
    silence is never mistaken for 'the check didn't run'. A dim confirmation
    when up to date, a yellow nag only when a real newer release exists."""
    outcome, latest = _update_status(_VERSION)
    if outcome == "dev_build":
        console.print(f"[dim]v{_VERSION} (source checkout)[/]")
    elif outcome == "check_failed":
        console.print(f"[dim]v{_VERSION}[/]")
    elif outcome == "outdated":
        console.print(
            f"[yellow]A newer version is available: {latest} (you're on {_VERSION}).[/] "
            f"Run 'pipx upgrade --force systematic-review-pipeline' to update, or see "
            f"{_CHANGELOG_URL}"
        )
    else:
        console.print(f"[dim]v{_VERSION} -- up to date[/]")


# --- core logic ---
def _menu_check_for_updates(state: RunState, cfg: ReviewConfig, prov: Provenance,
                             console: Console) -> None:
    """On-demand version of _maybe_print_update_notice that always reports a
    result -- up to date, outdated, or check-failed -- instead of staying
    silent, and logs the outcome to provenance."""
    outcome, latest = _update_status(_VERSION)
    if outcome == "dev_build":
        console.print(
            "[dim]Running from source (not a packaged install) -- version comparison "
            f"isn't meaningful; a checkout can be ahead of the last tagged release. "
            f"See the latest release directly: {_RELEASES_URL}[/]"
        )
    elif outcome == "check_failed":
        console.print(
            "[yellow]Could not check for updates[/] (no network, or GitHub is "
            "unreachable right now)."
        )
    elif outcome == "outdated":
        console.print(
            f"[yellow]A newer version is available: {latest} (you're on {_VERSION}).[/] "
            f"Run 'pipx upgrade --force systematic-review-pipeline' to update."
        )
    else:
        console.print(f"[green]Up to date[/] (v{_VERSION}).")
    prov.log("update_check", current=_VERSION, latest=latest, outcome=outcome)


# ---------------------------------------------------------------------------
# A / E. launch + resume
# ---------------------------------------------------------------------------
def main_interactive(runs_dir: Path, console: Console, preselect_run: str | None = None) -> None:
    console.print(Panel.fit(
        f"[bold]Systematic Review Pipeline[/] [dim]v{_VERSION}[/]\n[dim]guided mode[/]",
        border_style="cyan",
    ))
    _maybe_print_update_notice(console)

    existing_runs = RunState.list_runs(runs_dir)
    run_id = preselect_run if preselect_run in existing_runs else None

    if run_id is None:
        if existing_runs:
            choice = questionary.select(
                "What would you like to do?",
                choices=["Resume an existing review", "Start a new review"],
            ).ask()
        else:
            choice = "Start a new review"

        if choice is None:
            return

        if choice == "Resume an existing review":
            run_id = questionary.select("Choose a run to resume:", choices=existing_runs).ask()
            if run_id is None:
                return

    if run_id is not None:
        state = RunState.load(runs_dir / run_id)
        cfg = ReviewConfig.from_dict(state.config)
        filled = cfg.hydrate_secrets_from_env()
        prov = Provenance(state.run_dir / "provenance.jsonl")
        console.print(f"[green]Resumed run[/] {run_id}")
        if filled:
            console.print(f"[dim]Loaded {len(filled)} API key(s) from .env: "
                          f"{', '.join(cfg.configured_key_names())}[/]")
        missing = [src for src in cfg.sources if src in _KEYED_SOURCE_FIELD
                   and not getattr(cfg, _KEYED_SOURCE_FIELD[src], "")]
        if missing:
            console.print(f"[yellow]No API key found for: {', '.join(missing)}[/] -- "
                          f"those sources will be skipped. Put the key(s) in .env to "
                          f"restore them.")
    else:
        result = new_review_wizard(console, runs_dir)
        if result is None:
            return
        state, cfg, prov = result

    prov.log("tool_version", version=_VERSION)

    run_phase_loop(state, cfg, prov, console)
    consolidation_menu(state, cfg, prov, console)


# ---------------------------------------------------------------------------
# F. CLI entry point
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="slr.py",
        description="Guided interactive terminal for the systematic-review pipeline: "
                     "orchestrates search -> dedup -> screen -> manual-paste AI-assist "
                     "-> human review gate -> query expansion -> consolidation, across as "
                     "many review-gated phases as you choose. Run with no arguments to "
                     "start the interactive wizard.",
    )
    ap.add_argument("--runs-dir", default="runs",
                     help="Base directory for review run workspaces (default: runs)")
    ap.add_argument("--run", default=None,
                     help="Run id to preselect for resuming (still enters the interactive menu)")
    return ap


def main() -> int:
    # Must precede everything else: _ask_secret() and hydrate_secrets_from_env()
    # read os.environ directly, so a .env picked up later would have no effect
    # on keys already prompted for or hydrated by then.
    load_dotenv()

    ap = build_arg_parser()
    args = ap.parse_args()

    if MISSING_UI_DEPS:
        print(
            "This guided interface needs questionary and rich. "
            "Install: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)

    console = Console()
    try:
        main_interactive(runs_dir, console, preselect_run=args.run)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        return 130
    except CorruptCsvError as e:
        console.print(Panel(
            str(e), title="[red]A pipeline CSV is corrupt or unreadable[/]",
            border_style="red"))
        console.print("[yellow]Fix or remove the file named above, then resume this run "
                      "(`python slr.py --run <run-id>`).[/]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
