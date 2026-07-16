"""
slr.py -- guided interactive terminal (TUI) for the systematic-review pipeline.

Walks a researcher through a review-gated snowball systematic literature
review: search -> dedup -> screening skeleton -> manual-paste AI-assist
screening (paste into any free web chatbot, no API keys) -> human review
gate -> keyword snowball -> next phase, for as many phases as chosen, then a
consolidation menu (merge, download PDFs, verify citations, build the
extraction sheet, generate PRISMA/tier figures, export references, write the
provenance report). This is the top-level entry point:

    python slr.py

Every review lives under its own runs/<run_id>/ workspace (see srp.state);
per-phase/stage checkpoints make the wizard safe to stop and resume at any
point.
"""
# ===========================================================================
# WHAT YOU CHANGE (safe): nothing here for normal use -- run `python slr.py`
#   and answer the prompts. Advanced flags: `python slr.py --help`.
# WHAT YOU DON'T CHANGE (unless you are extending the tool):
#   the parts marked "# --- core logic ---" (phase orchestration, snowball).
# ===========================================================================
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from dataclasses import replace
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

from srp.config import ReviewConfig
from srp.state import RunState, record_key
from srp.provenance import Provenance
from srp import llm_assist
from srp import export as srp_export

_SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = _SCRIPT_DIR / "scripts"
_BATCH_SIZE = 20

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
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8")
    except (pd.errors.EmptyDataError, pd.errors.ParserError, OSError, UnicodeDecodeError):
        return pd.DataFrame()


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


def _should_run_stage(state: RunState, phase: int, stage: str, label: str) -> bool:
    if state.is_stage_done(phase, stage):
        rerun = questionary.confirm(
            f"Phase {phase}: '{label}' already completed -- re-run?", default=False,
        ).ask()
        return bool(rerun)
    return True


def run_subprocess(cmd: list, console: Console, description: str, success_codes=(0,)) -> bool:
    """Run a v1 CLI script, showing a spinner, and on failure offer
    retry / skip / abort instead of crashing the whole wizard."""
    while True:
        console.print(f"[dim]$ {' '.join(cmd)}[/]")
        result = None
        try:
            with console.status(f"[cyan]{description}...[/]", spinner="dots"):
                result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_SCRIPT_DIR))
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
       "and labels the AI screening prompt.   e.g.  Machine-learning intrusion detection")
    topic = questionary.text("Review topic:").ask()
    if topic is None or not topic.strip():
        console.print("[yellow]Cancelled.[/]")
        return None

    _h("Comma-separated search terms that define your query. Multi-word phrases are "
       "auto-quoted and joined with AND, and are reused in every phase.\n"
       "      e.g.  intrusion detection, machine learning, deep learning")
    keywords_raw = questionary.text("Keywords (comma-separated):").ask()
    if keywords_raw is None:
        return None
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

    _h("Publication-year range for the search (inclusive on both ends).")
    year_from = _ask_int("Year from:", default=2020)
    if year_from is None:
        return None
    year_to = _ask_int("Year to:", default=2026)
    if year_to is None:
        return None

    _h("A real, deliverable email. OpenAlex and Crossref use it for faster polite-pool "
       "access, and Unpaywall requires it to look up open-access PDFs. Do not use an "
       "@example.com placeholder.")
    mailto = questionary.text(
        "Contact email (used for API polite-pool access):",
        validate=lambda s: True if "@" in s else "Must contain @",
    ).ask()
    if mailto is None:
        return None

    _h("Optional. Raises your Semantic Scholar rate limit above the free shared pool. "
       "Leave blank to use the shared pool. Get a key at semanticscholar.org/product/api")
    s2_api_key = questionary.text("Semantic Scholar API key (optional):", default="").ask() or ""
    _h("Optional. Enables CORE as an extra open-access PDF source when downloading. "
       "Leave blank to skip CORE. Get a key at core.ac.uk/services/api")
    core_api_key = questionary.text("CORE API key (optional):", default="").ask() or ""

    _h("Which literature databases to query. Space toggles a source, Enter confirms. "
       "Keeping all four is the usual choice.")
    sources = questionary.checkbox(
        "Sources to search:",
        choices=[
            questionary.Choice("openalex", checked=True),
            questionary.Choice("semanticscholar", checked=True),
            questionary.Choice("crossref", checked=True),
            questionary.Choice("arxiv", checked=True),
        ],
    ).ask()
    if not sources:
        sources = ["openalex", "semanticscholar", "crossref", "arxiv"]

    _h("Cap on records fetched from each source, per phase. Keep it small (40-60) while "
       "testing; raise it for a full run.")
    max_per_source = _ask_int("Max results per source:", default=60, minimum=1)
    if max_per_source is None:
        return None
    _h("How many review-gated snowball rounds to run. 1 = a single search. With more than 1, "
       "after you review each phase's included studies the tool suggests new keywords from "
       "them and searches again.")
    n_phases = _ask_int("Number of snowball phases:", default=1, minimum=1)
    if n_phases is None:
        return None
    _h("Fuzzy title-match cutoff for near-duplicate detection (0-100). 92 is a safe default; "
       "lower merges more aggressively but risks false matches.")
    title_threshold = _ask_int("Fuzzy title-dedup threshold (0-100):", default=92, minimum=0)
    if title_threshold is None:
        return None

    _h("Free text -- ANY web chatbot works; the paste-and-parse workflow does not depend on "
       "the tool. This is only recorded in the provenance file as your AI-assistance "
       "disclosure. Examples: ChatGPT, Claude, Gemini, Microsoft Copilot, DeepSeek, Qwen, "
       "Mistral (Le Chat), Perplexity, HuggingChat. Leave blank to screen fully by hand.")
    assist_tool_name = questionary.text(
        "Which web chatbot will you paste screening prompts into? "
        "(free text -- any tool; e.g. ChatGPT / Claude / Gemini / Copilot / DeepSeek / Qwen)",
        default="",
    ).ask() or ""
    _h("Your name or initials, stamped onto decisions and the audit trail. Optional.")
    reviewer = questionary.text("Reviewer name/initials:", default="").ask() or ""

    slug = _slugify(topic) or "review"
    run_id = _unique_run_id(runs_dir, slug)

    cfg = ReviewConfig(
        topic=topic.strip(), keywords=keywords, year_from=year_from, year_to=year_to,
        mailto=mailto.strip(), s2_api_key=s2_api_key.strip(), core_api_key=core_api_key.strip(),
        sources=sources, max_per_source=max_per_source, n_phases=n_phases,
        title_threshold=title_threshold, assist_tool_name=assist_tool_name.strip(),
        reviewer=reviewer.strip(),
    )

    summary_lines = [
        f"Run id: {run_id}",
        f"Topic: {cfg.topic}",
        f"Keywords: {', '.join(cfg.keywords) or '(none)'}",
        f"Years: {cfg.year_from}-{cfg.year_to}",
        f"Sources: {', '.join(cfg.sources)}",
        f"Max per source: {cfg.max_per_source}",
        f"Phases: {cfg.n_phases}",
        f"Title-dedup threshold: {cfg.title_threshold}",
        f"AI-assist tool: {cfg.assist_tool_name or '(none)'}",
        f"Reviewer: {cfg.reviewer or '(none)'}",
        f"Search query: {cfg.search_query()}",
    ]
    console.print(Panel("\n".join(summary_lines), title="Review summary", border_style="cyan"))

    if not questionary.confirm("Create this review?", default=True).ask():
        console.print("[yellow]Cancelled.[/]")
        return None

    state = RunState.create(runs_dir, run_id, cfg.to_dict())
    prov = Provenance(state.run_dir / "provenance.jsonl")
    prov.log("review_created", run_id=run_id, topic=cfg.topic, keywords=cfg.keywords)
    console.print(f"[green]Created run[/] {run_id} at {state.run_dir}")

    return state, cfg, prov


# ---------------------------------------------------------------------------
# C. phase loop
# ---------------------------------------------------------------------------
# --- core logic ---
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

        # 1. SEARCH
        if _should_run_stage(state, phase, "search", "Search"):
            candidates_path = pdir / "candidates.csv"
            cmd = [
                sys.executable, _script_path("search.py"),
                "--query", query,
                "--year-from", str(cfg.year_from),
                "--year-to", str(cfg.year_to),
                "--mailto", cfg.mailto,
                "--max-per-source", str(cfg.max_per_source),
                "--sources", ",".join(cfg.sources),
                "--out", str(candidates_path),
            ]
            if cfg.s2_api_key:
                cmd += ["--s2-api-key", cfg.s2_api_key]
            if run_subprocess(cmd, console, f"Phase {phase}: searching sources"):
                n_hits = len(_read_csv_safe(candidates_path))
                state.mark_stage(phase, "search", counts={"n_hits": n_hits})
                prov.log("search_run", phase=phase, query=query, sources=cfg.sources, n_hits=n_hits)

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

        # 4. AI-ASSIST TA (manual paste loop)
        if _should_run_stage(state, phase, "assist_ta", "AI-assist TA screening"):
            run_ai_assist_loop(state, cfg, prov, phase, pdir, console)

        # 5. HUMAN REVIEW GATE
        if _should_run_stage(state, phase, "review_gate", "Human review gate"):
            run_review_gate(state, cfg, prov, phase, pdir, console)

        # 6. SNOWBALL (only if there is a next phase)
        if phase < n_phases:
            if _should_run_stage(state, phase, "snowball", "Snowball expansion"):
                run_snowball(state, cfg, prov, phase, pdir, console)

        # Advance the resume pointer past this phase, unless snowball already did.
        if state.state.get("current_phase", 1) <= phase:
            state.state["current_phase"] = phase + 1
            state.save()
        phase += 1

    console.print(Panel(f"All {n_phases} phase(s) complete.", title="Phase loop finished",
                         border_style="green"))


def _apply_ta_decisions(screening_path: Path, parsed: list, state: RunState, phase: int):
    if not screening_path.exists():
        return [], [rec["id"] for rec in parsed], Counter()

    df = _read_csv_safe(screening_path)
    if df.empty:
        return [], [rec["id"] for rec in parsed], Counter()

    for col in ("ta_decision", "ta_reason", "reviewer"):
        if col in df.columns:
            df[col] = df[col].astype(object)

    id_index = {}
    for idx, v in df["id"].items():
        id_index[_id_to_str(v)] = idx  # last-wins on duplicate ids

    matched, unmatched = [], []
    counts: Counter = Counter()
    for rec in parsed:
        key = str(rec["id"])
        if key not in id_index:
            unmatched.append(rec["id"])
            continue
        idx = id_index[key]
        df.at[idx, "ta_decision"] = rec["decision"]
        df.at[idx, "ta_reason"] = rec["reason"]
        if "reviewer" in df.columns:
            cur = df.at[idx, "reviewer"]
            if pd.isna(cur) or str(cur).strip() == "":
                df.at[idx, "reviewer"] = "ai-assisted"
        matched.append(rec["id"])
        counts[rec["decision"]] += 1

        row = df.loc[idx]
        state.record_decision(
            record_key=record_key(row.get("doi", ""), row.get("title", "")),
            id=_native(row.get("id")),
            decision=rec["decision"],
            stage="ta",
            phase=phase,
            reason=rec["reason"],
            source="manual-paste",
            title=row.get("title", ""),
            doi=row.get("doi", ""),
        )

    df.to_csv(screening_path, index=False, encoding="utf-8")
    return matched, unmatched, counts


# --- core logic ---
def run_ai_assist_loop(state: RunState, cfg: ReviewConfig, prov: Provenance, phase: int,
                        pdir: Path, console: Console) -> None:
    dedup_path = pdir / "candidates_dedup.csv"
    screening_path = pdir / "screening.csv"
    skipped_ids: set = set()
    totals: Counter = Counter()
    tool_name = cfg.assist_tool_name or "your web chatbot"

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

        batch = undecided_rows[:_BATCH_SIZE]
        start = len(dedup_df) - len(undecided_rows)
        records = [
            {"id": row.get("id"), "title": row.get("title", ""), "abstract": row.get("abstract", ""),
             "year": row.get("year", ""), "venue": row.get("venue", "")}
            for row in batch
        ]
        batch_label = f"phase={phase} stage=ta rows={start}-{start + len(records) - 1}"
        prompt = llm_assist.build_screening_prompt(
            records, stage="ta", topic=cfg.topic, batch_label=batch_label,
        )
        prompt_path = pdir / f"prompt_ta_{start}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        console.print(Panel(
            f"Paste the contents of [bold]{prompt_path}[/] into [bold]{tool_name}[/],\n"
            f"then save its reply to a text file.",
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

        default_reply = pdir / f"reply_ta_{start}.txt"
        console.print(
            "[dim]Save the chatbot's reply to a text file, then give its path below "
            "(Enter accepts the suggested path). The reply should be one line per study, "
            "e.g.  12 | INCLUDE | on-topic DL IDS.[/]")
        reply_path = None
        while reply_path is None:
            reply_str = questionary.text(
                "Path to the chatbot reply file:", default=str(default_reply),
            ).ask()
            if reply_str is None:
                break
            candidate = Path(reply_str)
            if candidate.exists():
                reply_path = candidate
            else:
                retry = questionary.confirm(
                    f"{candidate} does not exist yet. Try again?", default=True,
                ).ask()
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

        parsed = llm_assist.parse_screening_response(text)
        matched, unmatched, counts = _apply_ta_decisions(screening_path, parsed, state, phase)
        totals.update(counts)

        table = Table(title="Batch result")
        table.add_column("decision")
        table.add_column("count", justify="right")
        for decision, n in sorted(counts.items()):
            table.add_row(decision, str(n))
        console.print(table)
        if unmatched:
            console.print(f"[yellow]{len(unmatched)} id(s) in the reply did not match any row: "
                           f"{', '.join(str(x) for x in unmatched)}[/]")

        prov.log(
            "assist_response_parsed", phase=phase, n_decided=len(matched),
            n_include=counts.get("include", 0), n_exclude=counts.get("exclude", 0),
            tool=cfg.assist_tool_name,
        )

        if not questionary.confirm("Screen another batch?", default=True).ask():
            break

    state.mark_stage(phase, "assist_ta", counts=dict(totals))


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

    included_mask = screening_df["ta_decision"].astype(str).str.strip().str.lower().eq("include")
    included_df = screening_df[included_mask]

    if included_df.empty:
        console.print(f"[yellow]No TA-included studies in phase {phase} yet.[/]")
    else:
        table = Table(title=f"Phase {phase}: TA-included studies ({len(included_df)})")
        table.add_column("id")
        table.add_column("title")
        table.add_column("year")
        table.add_column("venue")
        table.add_column("reason")
        for _, row in included_df.iterrows():
            table.add_row(
                _disp(row.get("id", "")),
                _disp(row.get("title", ""))[:70],
                _disp(row.get("year", "")),
                _disp(row.get("venue", ""))[:25],
                _disp(row.get("ta_reason", ""))[:40],
            )
        console.print(table)

    console.print(
        "[dim]This is your decision gate: the studies above are what AI-assisted screening "
        "proposed to include. Type the ids you disagree with to flip them to EXCLUDE, or "
        "press Enter to accept them all.   e.g.  7, 12, 30[/]")
    overrides_str = questionary.text(
        "Comma-separated ids to flip to EXCLUDE (leave blank to accept all as included):",
        default="",
    ).ask() or ""
    ids_to_flip = {s.strip() for s in overrides_str.split(",") if s.strip()}

    n_overridden = 0
    if ids_to_flip:
        for idx, row in screening_df.iterrows():
            if _id_to_str(row.get("id", "")) in ids_to_flip and included_mask.get(idx, False):
                screening_df.at[idx, "ta_decision"] = "exclude"
                prior_reason = _disp(screening_df.at[idx, "ta_reason"])
                screening_df.at[idx, "ta_reason"] = (prior_reason + " [reviewer override]").strip()
                state.record_decision(
                    record_key=record_key(row.get("doi", ""), row.get("title", "")),
                    id=_native(row.get("id")),
                    decision="exclude",
                    stage="ta",
                    phase=phase,
                    reason="reviewer override",
                    source="human-review",
                    title=row.get("title", ""),
                    doi=row.get("doi", ""),
                )
                n_overridden += 1
        screening_df.to_csv(screening_path, index=False, encoding="utf-8")

    n_included_final = len(included_df) - n_overridden
    console.print(f"[green]{n_included_final}[/] studies confirmed included for phase {phase} "
                  f"({n_overridden} overridden to exclude).")

    state.mark_stage(phase, "review_gate", counts={"n_included": n_included_final, "n_overridden": n_overridden})
    prov.log("phase_review", phase=phase, n_included=n_included_final, n_overridden=n_overridden)


# --- core logic ---
def run_snowball(state: RunState, cfg: ReviewConfig, prov: Provenance, phase: int,
                  pdir: Path, console: Console) -> None:
    screening_df = _read_csv_safe(pdir / "screening.csv")
    titles = []
    if not screening_df.empty and "ta_decision" in screening_df.columns:
        included_mask = screening_df["ta_decision"].astype(str).str.strip().str.lower().eq("include")
        titles = screening_df.loc[included_mask, "title"].dropna().astype(str).tolist()

    terms = _extract_expansion_terms(titles, cfg.keywords)
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

    new_keywords = list(cfg.keywords) + picked
    next_query = replace(cfg, keywords=new_keywords).search_query()
    state.state[f"phase_{phase + 1}_query"] = next_query
    console.print(f"[green]Next phase query:[/] {next_query}")

    state.mark_stage(phase, "snowball", counts={"n_added": len(picked)})
    prov.log("snowball_expand", from_phase=phase, added_keywords=picked)

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
        included = screening_df[
            screening_df["ta_decision"].astype(str).str.strip().str.lower().eq("include")
        ].copy()
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
        included["phase"] = phase
        frames.append(included)

    if not frames:
        return pd.DataFrame(columns=[
            "id", "title", "year", "venue", "doi", "ta_decision", "ta_reason",
            "ft_decision", "ft_reason", "round", "reviewer", "url", "authors", "phase",
        ])

    merged = pd.concat(frames, ignore_index=True)
    merged["record_key"] = merged.apply(
        lambda r: record_key(r.get("doi", ""), r.get("title", "")), axis=1,
    )
    return merged.drop_duplicates(subset="record_key", keep="first").reset_index(drop=True)


def _ensure_merged(state: RunState, cfg: ReviewConfig, console: Console, force: bool = False) -> pd.DataFrame:
    run_dir = state.run_dir
    included_path = run_dir / "included_final.csv"
    final_dedup_path = run_dir / "final_dedup.csv"

    if not force and included_path.exists():
        return _read_csv_safe(included_path)

    merged = _merge_included_across_phases(state, cfg)
    merged.to_csv(included_path, index=False, encoding="utf-8")

    dedup_cols = ["id", "doi", "url", "title", "authors", "year", "venue"]
    present = [c for c in dedup_cols if c in merged.columns]
    merged[present].to_csv(final_dedup_path, index=False, encoding="utf-8")

    console.print(f"[green]Merged[/] {len(merged)} included studies across {cfg.n_phases} phase(s) "
                  f"-> {included_path.name}")
    return merged


def _menu_merge(state: RunState, cfg: ReviewConfig, console: Console) -> None:
    merged = _ensure_merged(state, cfg, console, force=True)
    console.print(f"[bold]{len(merged)}[/] unique included studies written to "
                  f"{state.run_dir / 'included_final.csv'}")


def _menu_download(state: RunState, cfg: ReviewConfig, console: Console) -> None:
    _ensure_merged(state, cfg, console)
    run_dir = state.run_dir
    final_dedup_path = run_dir / "final_dedup.csv"
    outdir = run_dir / "pdfs"
    report_path = run_dir / "manual_download_needed.csv"

    cmd = [
        sys.executable, _script_path("download.py"),
        "--in", str(final_dedup_path),
        "--mailto", cfg.mailto,
        "--outdir", str(outdir),
        "--report", str(report_path),
    ]
    if cfg.core_api_key:
        cmd += ["--core-api-key", cfg.core_api_key]

    if run_subprocess(cmd, console, "Downloading PDFs for the final set"):
        manual = _read_csv_safe(report_path)
        total = _read_csv_safe(final_dedup_path)
        n_manual = len(manual)
        n_downloaded = max(len(total) - n_manual, 0)
        console.print(f"[bold]{n_downloaded}[/] downloaded, [bold]{n_manual}[/] need manual "
                      f"retrieval -> {report_path}")


def _menu_verify_citations(state: RunState, cfg: ReviewConfig, console: Console) -> None:
    _ensure_merged(state, cfg, console)
    run_dir = state.run_dir
    cmd = [
        sys.executable, _script_path("verify_citations.py"),
        "--csv", str(run_dir / "included_final.csv"),
        "--out", str(run_dir / "citation_verification.csv"),
    ]
    # returncode 1 just means some DOIs failed verification -- still a completed run.
    run_subprocess(cmd, console, "Verifying citation DOIs", success_codes=(0, 1))


def _menu_extract(state: RunState, cfg: ReviewConfig, console: Console) -> None:
    _ensure_merged(state, cfg, console)
    run_dir = state.run_dir
    cmd = [
        sys.executable, _script_path("extract.py"),
        "--in", str(run_dir / "included_final.csv"),
        "--out", str(run_dir / "extraction.csv"),
        "--candidates", str(run_dir / "final_dedup.csv"),
    ]
    run_subprocess(cmd, console, "Building extraction sheet")


def _menu_figures(state: RunState, cfg: ReviewConfig, console: Console) -> None:
    last_phase = cfg.n_phases
    pdir = state.phase_dir(last_phase)
    run_dir = state.run_dir
    outdir = run_dir / "figures"
    extraction_path = run_dir / "extraction.csv"

    cmd = [
        sys.executable, _script_path("figures.py"),
        "--screening", str(pdir / "screening.csv"),
        "--dedup", str(pdir / "candidates_dedup.csv"),
        "--candidates", str(pdir / "candidates.csv"),
        "--quality", str(extraction_path),
        "--outdir", str(outdir),
    ]
    included_path = run_dir / "included_final.csv"
    if included_path.exists():
        merged = _read_csv_safe(included_path)
        if not merged.empty:
            # figures.py derives its own counts from the last phase's CSVs (per
            # convention); override "included" with the true cross-phase total.
            cmd += ["--included", str(len(merged))]

    run_subprocess(cmd, console, "Generating PRISMA + tier figures")


def _menu_export_refs(state: RunState, cfg: ReviewConfig, console: Console) -> None:
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

    for col in ("ft_decision", "ta_decision"):
        if col in df.columns and df[col].astype(str).str.strip().ne("").any():
            mask = df[col].astype(str).str.strip().str.lower().eq("include")
            if mask.any():
                df = df[mask]
            break

    records = df.to_dict("records")
    bib = srp_export.to_bibtex(records)
    ris = srp_export.to_ris(records)
    (run_dir / "references.bib").write_text(bib, encoding="utf-8")
    (run_dir / "references.ris").write_text(ris, encoding="utf-8")
    console.print(f"[green]Wrote {len(records)} reference(s)[/] from {src_name} to "
                  f"references.bib / references.ris")


def _compute_prisma_counts(state: RunState, cfg: ReviewConfig) -> dict:
    identified = 0
    dup_removed = 0
    screened = 0
    excluded_ta = 0
    for phase in range(1, cfg.n_phases + 1):
        pdir = state.phase_dir(phase)
        cand = _read_csv_safe(pdir / "candidates.csv")
        dedup = _read_csv_safe(pdir / "candidates_dedup.csv")
        screening = _read_csv_safe(pdir / "screening.csv")
        identified += len(cand)
        if "duplicate_of" in dedup.columns:
            dup_removed += int(dedup["duplicate_of"].notna().sum())
        elif not cand.empty and not dedup.empty:
            dup_removed += max(len(cand) - len(dedup), 0)
        screened += len(screening)
        if "ta_decision" in screening.columns:
            excluded_ta += int(screening["ta_decision"].astype(str).str.strip().str.lower().eq("exclude").sum())

    included_final = _read_csv_safe(state.run_dir / "included_final.csv")
    return {
        "records identified (all phases)": identified,
        "duplicates removed": dup_removed,
        "records screened (TA)": screened,
        "excluded at TA": excluded_ta,
        "included (final merged set)": len(included_final),
    }


def _menu_provenance(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    prisma = _compute_prisma_counts(state, cfg)
    out_path = state.run_dir / "PROVENANCE.md"
    prov.render_markdown(out_path, config=cfg.to_dict(), prisma=prisma)
    console.print(f"[green]Wrote[/] {out_path}")


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


def consolidation_menu(state: RunState, cfg: ReviewConfig, prov: Provenance, console: Console) -> None:
    actions = {
        "Merge included studies across phases": lambda: _menu_merge(state, cfg, console),
        "Download PDFs for the final set": lambda: _menu_download(state, cfg, console),
        "Verify citations (DOIs)": lambda: _menu_verify_citations(state, cfg, console),
        "Build extraction sheet (for human quality coding)": lambda: _menu_extract(state, cfg, console),
        "Generate PRISMA + tier figures": lambda: _menu_figures(state, cfg, console),
        "Export references (BibTeX + RIS)": lambda: _menu_export_refs(state, cfg, console),
        "Write provenance report": lambda: _menu_provenance(state, cfg, prov, console),
    }

    console.rule("[bold]Consolidation[/]")
    while True:
        choice = questionary.select("What would you like to do?", choices=list(actions) + ["Finish"]).ask()
        if choice is None or choice == "Finish":
            break
        actions[choice]()

    _print_closing_panel(state, console)


# ---------------------------------------------------------------------------
# A / E. launch + resume
# ---------------------------------------------------------------------------
def main_interactive(runs_dir: Path, console: Console, preselect_run: str | None = None) -> None:
    console.print(Panel.fit(
        "[bold]Systematic Review Pipeline[/]\n[dim]guided mode[/]", border_style="cyan",
    ))

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
        prov = Provenance(state.run_dir / "provenance.jsonl")
        console.print(f"[green]Resumed run[/] {run_id}")
    else:
        result = new_review_wizard(console, runs_dir)
        if result is None:
            return
        state, cfg, prov = result

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
                     "-> human review gate -> snowball -> consolidation, across as many "
                     "review-gated phases as you choose. Run with no arguments to start "
                     "the interactive wizard.",
    )
    ap.add_argument("--runs-dir", default="runs",
                     help="Base directory for review run workspaces (default: runs)")
    ap.add_argument("--run", default=None,
                     help="Run id to preselect for resuming (still enters the interactive menu)")
    return ap


def main() -> int:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
