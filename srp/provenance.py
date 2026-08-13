"""
provenance.py -- Provenance: an always-on run-manifest logger for the SLR
pipeline. Every phase appends one JSON line per event to a run's
provenance.jsonl (search queries, dedup decisions, AI-assisted screening
responses, extraction steps, ...); render_markdown() turns that log plus the
run's ReviewConfig and PRISMA counts into a human-readable audit trail
(PRISMA flow + AI-assistance disclosure) suitable as a reproducibility
appendix.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Provenance:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # --- core logic ---
    def log(self, event: str, **fields) -> dict:
        rec = {"ts": _now(), "event": event, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def events(self) -> list[dict]:
        out = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    # --- core logic ---
    def ai_assist_summary(self) -> dict:
        """What AI assistance ACTUALLY happened, read back from the event log.

        The disclosure below is generated from these numbers. It used to be emitted
        from the mere presence of a config string, and it asserted that "every AI
        suggestion was reviewed and confirmed by a human before inclusion" -- a
        claim the tool never checked and which was false for every exclusion, since
        the review gate only ever displayed includes. That statement then went into
        a file the README describes as safe to submit as a reproducibility
        appendix. A tool must not sign a claim about human oversight on the
        researcher's behalf.
        """
        s = {
            "n_ai_decided": 0, "n_include": 0, "n_exclude": 0, "n_maybe": 0,
            "n_reviewed_by_human": 0, "n_overridden": 0,
            "n_flipped_to_exclude": 0, "n_flipped_to_include": 0,
            "n_ai_excludes_total": 0, "n_ai_excludes_shown": 0,
            "n_unparsed_lines": 0, "n_unknown_ids": 0, "n_conflicts": 0,
            "criteria_sources": set(), "stages": set(),
            "first_ts": None, "last_ts": None, "gate_ran": False,
        }
        for rec in self.events():
            event = rec.get("event")
            if event == "assist_response_parsed":
                s["n_ai_decided"] += int(rec.get("n_decided", 0) or 0)
                s["n_include"] += int(rec.get("n_include", 0) or 0)
                s["n_exclude"] += int(rec.get("n_exclude", 0) or 0)
                s["n_maybe"] += int(rec.get("n_maybe", 0) or 0)
                s["n_unparsed_lines"] += int(rec.get("n_unparsed_lines", 0) or 0)
                s["n_unknown_ids"] += int(rec.get("n_unknown_ids", 0) or 0)
                s["n_conflicts"] += int(rec.get("n_conflicts", 0) or 0)
                if rec.get("criteria_source"):
                    s["criteria_sources"].add(rec["criteria_source"])
                s["stages"].add(rec.get("stage", "title/abstract"))
                ts = rec.get("ts")
                if ts:
                    s["first_ts"] = min(s["first_ts"] or ts, ts)
                    s["last_ts"] = max(s["last_ts"] or ts, ts)
            elif event == "phase_review":
                s["gate_ran"] = True
                for key in ("n_reviewed_by_human", "n_overridden",
                             "n_flipped_to_exclude", "n_flipped_to_include",
                             "n_ai_excludes_total", "n_ai_excludes_shown"):
                    s[key] += int(rec.get(key, 0) or 0)
        return s

    def _reporting_bias_lines(self, config: dict) -> list:
        """PRISMA item 14 (grey literature) and item 21 (reporting-bias
        assessment) both need an answer, not silence. Item 21's usual methods
        (funnel plot, Egger's test) only make sense with pooled effect sizes,
        which this tool does not compute unless the review explicitly
        meta-analyzes -- see the wizard's own caution against naive pooling."""
        lines = []
        if config.get("grey_literature_included"):
            lines.append("- **Grey literature:** included in the search.")
        else:
            justification = config.get("grey_literature_justification") or "not recorded"
            lines.append(f"- **Grey literature:** excluded. {justification}")
        if config.get("reporting_bias_assessment"):
            lines.append(f"- **Reporting bias assessment:** "
                          f"{config['reporting_bias_assessment']}")
        return lines

    def _appraisal_lines(self, config: dict) -> list:
        """PRISMA item 11 (risk-of-bias assessment) and items 15/22 (certainty of
        evidence) each need an answer, not silence -- see srp/appraisal.py. This
        renders whichever instrument(s) the review actually selected, with their
        citations, or the field's own justification when none applies."""
        from srp.appraisal import INSTRUMENTS  # local import: avoid a hard srp.appraisal
                                                 # dependency for callers that only need
                                                 # the rest of this module

        field = config.get("research_field")
        primary = config.get("primary_study_instruments") or []
        certainty = config.get("certainty_framework")
        review_level = config.get("review_level_instrument")
        justification = config.get("appraisal_justification")

        if not field and not primary and not certainty:
            return ["**Critical appraisal:** not recorded for this review."]

        out = ["**Critical appraisal (PRISMA item 11):**"]
        if primary:
            for key in primary:
                inst = INSTRUMENTS.get(key)
                if inst:
                    out.append(f"- {inst.name} -- {inst.citation} ({inst.url})")
        else:
            out.append("- No primary-study appraisal instrument was recorded.")

        if certainty:
            inst = INSTRUMENTS.get(certainty)
            if inst:
                out.append(f"\n**Certainty of evidence (PRISMA items 15, 22):** {inst.name} "
                            f"-- {inst.citation} ({inst.url})")
        elif justification:
            out.append(f"\n**Certainty of evidence (PRISMA items 15, 22):** {justification}")

        if review_level:
            inst = INSTRUMENTS.get(review_level)
            if inst:
                out.append(f"\n**Review self-appraisal:** {inst.name} -- {inst.citation} "
                            f"({inst.url})")

        return out

    def _ai_disclosure_lines(self, config: dict) -> list:
        tool = config.get("assist_tool_name")
        if not tool:
            return ["**AI-assistance disclosure:** No AI assistance was used in screening."]

        s = self.ai_assist_summary()
        if not s["n_ai_decided"]:
            return [f"**AI-assistance disclosure:** *{tool}* was configured for "
                     f"AI-assisted screening, but the provenance log records no AI "
                     f"screening decisions, so no AI output entered this review."]

        out = [
            "**AI-assistance disclosure:**",
            "",
            f"- **Tool:** {tool}, used in a manual copy-paste workflow (no automated API calls).",
            f"- **Stage(s) assisted:** {', '.join(sorted(s['stages'])) or 'title/abstract'} screening.",
        ]
        if s["first_ts"]:
            window = (s["first_ts"] if s["first_ts"] == s["last_ts"]
                      else f"{s['first_ts']} to {s['last_ts']}")
            out.append(f"- **Access dates:** {window}.")
            out.append("- **Note:** the exact model version behind a web chatbot changes "
                        "without notice and is not recorded by this tool. Record it "
                        "manually if your venue requires it.")
        out.append(f"- **Records decided with AI assistance:** {s['n_ai_decided']} "
                    f"({s['n_include']} include, {s['n_exclude']} exclude, {s['n_maybe']} maybe).")

        if s["gate_ran"]:
            out.append(f"- **Human review:** a reviewer inspected {s['n_reviewed_by_human']} "
                        f"of these {s['n_ai_decided']} AI decisions at the review gate "
                        f"({s['n_ai_excludes_shown']} of {s['n_ai_excludes_total']} AI "
                        f"exclusions were shown for checking), and overrode "
                        f"{s['n_overridden']} ({s['n_flipped_to_exclude']} include->exclude, "
                        f"{s['n_flipped_to_include']} exclude->include).")
            if s["n_reviewed_by_human"] < s["n_ai_decided"]:
                out.append(f"- **Coverage caveat:** {s['n_ai_decided'] - s['n_reviewed_by_human']} "
                            f"AI decision(s) were NOT individually inspected by a human. "
                            f"This review does not claim complete human verification of "
                            f"AI-assisted screening.")
        else:
            out.append("- **Human review:** NO review gate was completed for this run. "
                        "AI screening decisions entered the review without recorded "
                        "human confirmation.")

        if "generic-fallback" in s["criteria_sources"]:
            out.append("- **Criteria caveat:** some or all AI screening was run WITHOUT the "
                        "protocol's eligibility criteria, against a generic relevance "
                        "instruction instead.")
        elif "protocol" in s["criteria_sources"]:
            out.append("- **Criteria:** the protocol's eligibility criteria, reproduced "
                        "verbatim in every screening prompt.")

        noise = s["n_unparsed_lines"] + s["n_unknown_ids"] + s["n_conflicts"]
        if noise:
            out.append(f"- **Reply anomalies:** {s['n_unparsed_lines']} unparseable line(s), "
                        f"{s['n_unknown_ids']} out-of-batch id(s), and {s['n_conflicts']} "
                        f"contradictory decision(s) were detected and refused.")
        return out

    def _search_strategy_lines(self) -> list:
        """Render the per-source search strategy as a quotable table.

        This is PRISMA item 7 ("full search strategies for all databases...
        including any filters and limits") and item 6 (date last searched). Both
        are REQUIRED items and neither can be reconstructed after the fact, because
        each source gets a different transformation of your query.

        The data was already being logged, but the generic event renderer flattened
        it into a comma-joined string truncated at 80 characters -- present in the
        file and useless in it. This renders it as a table a user can paste into a
        methods section or an appendix as-is.
        """
        runs = [r for r in self.events() if r.get("event") == "search_run"]
        if not runs:
            return []

        lines = ["## Search strategy (PRISMA 2020 items 6 and 7)", ""]
        any_truncation = False
        for run in runs:
            phase = run.get("phase", "")
            lines.append(f"**Phase {phase}** -- query as entered: `{run.get('query', '')}`")
            lines.append("")
            per_source = run.get("per_source") or []
            if not per_source:
                lines.append(f"_Per-source detail was not recorded for this phase "
                              f"(n_hits={run.get('n_hits', '?')})._")
                lines.append("")
                continue
            lines.append("| Source | Query actually sent | Date searched (UTC) | Retrieved | Available | Status |")
            lines.append("|---|---|---|---|---|---|")
            for s in per_source:
                total = s.get("total_available")
                retrieved = s.get("n_retrieved", 0)
                available = "unknown" if total in (None, "") else f"{int(float(total)):,}"
                if total not in (None, "") and int(float(total)) > (retrieved or 0):
                    available += " **(truncated)**"
                    any_truncation = True
                # Escape pipes so a query containing one cannot break the table.
                query_sent = str(s.get("query_sent", "") or "").replace("|", "\\|")
                lines.append(
                    f"| {s.get('source', '')} | `{query_sent}` | "
                    f"{s.get('retrieved_at', '')} | {retrieved} | {available} | "
                    f"{s.get('status', '')} |")
            lines.append("")

        if any_truncation:
            lines.append(
                "> **Truncation notice.** Sources marked *(truncated)* returned only the "
                "first `max_per_source` records of a larger result set, ranked by each "
                "API's own undocumented relevance score. For those sources the retrieved "
                "count is a sample, not the number of records the database holds for this "
                "query, and it should not be reported as \"records identified\" without "
                "saying so. Re-run with a higher `--max-per-source` for a complete search.")
            lines.append("")
        lines.append(
            "> **Note on `Available`.** This is the total each API reports for the query "
            "*as that API interpreted it*. Some sources score relevance rather than "
            "applying your boolean strictly (Crossref's `query.bibliographic` is the "
            "clearest example), so a very large number here means the source matched "
            "loosely -- not that your review missed that many eligible studies.")
        lines.append("")
        return lines

    # --- core logic: render ---
    def render_markdown(self, out_path, config: dict | None = None, prisma: dict | None = None) -> None:
        lines = ["# Review provenance", "", f"Generated: {_now()}", ""]

        lines.append("## Configuration")
        lines.append("")
        if config:
            sources = config.get("sources") or []
            if config.get("topic"):
                lines.append(f"- **Topic:** {config['topic']}")
            # The wizard builds keyword_blocks (OR-within, AND-across) and leaves
            # the legacy flat `keywords` empty, so reading only `keywords` here
            # silently dropped the "Keywords:" line for every guided-wizard
            # review -- ReviewConfig.display_keywords() is the one place that
            # already knows the right precedence between the two forms.
            from srp.config import ReviewConfig
            keyword_display = ReviewConfig.from_dict(config).display_keywords()
            if keyword_display:
                lines.append(f"- **Keywords:** {keyword_display}")
            if config.get("year_from") or config.get("year_to"):
                lines.append(f"- **Year range:** {config.get('year_from', '')}-{config.get('year_to', '')}")
            if sources:
                lines.append(f"- **Sources:** {', '.join(str(s) for s in sources)}")
            if config.get("n_phases"):
                lines.append(f"- **Phases:** {config['n_phases']}")
            if config.get("mailto"):
                lines.append(f"- **Mailto:** {config['mailto']}")
            version_events = [e for e in self.events() if e.get("event") == "tool_version"]
            if version_events:
                versions_seen = []
                for e in version_events:
                    v = e.get("version")
                    if v and v not in versions_seen:
                        versions_seen.append(v)
                latest = version_events[-1].get("version")
                line = f"- **Tool version:** {latest}"
                earlier = [v for v in versions_seen if v != latest]
                if earlier:
                    line += (f" (this review was also worked on under {', '.join(earlier)} -- "
                              f"see the search & processing log for exactly when)")
                lines.append(line)
            if config.get("registration_id"):
                lines.append(f"- **Protocol registration:** {config['registration_id']}")
            else:
                # PRISMA 2020 item 24a: state the registration, or state that there
                # was none. Silence is not one of the options.
                lines.append("- **Protocol registration:** none (this review was not "
                              "registered in a public protocol registry)")
            for label, key in (("Inclusion criteria", "inclusion_criteria"),
                                ("Exclusion criteria", "exclusion_criteria"),
                                ("Language restriction", "language_restriction"),
                                ("Funding", "funding_statement"),
                                ("Competing interests", "competing_interests")):
                if config.get(key):
                    lines.append(f"- **{label}:** {config[key]}")
            lines.extend(self._reporting_bias_lines(config))
            lines.append("")
            lines.extend(self._appraisal_lines(config))
            lines.append("")
            lines.extend(self._ai_disclosure_lines(config))
        else:
            lines.append("_No configuration recorded._")
        lines.append("")

        lines.extend(self._search_strategy_lines())

        if prisma is not None:
            lines.append("## PRISMA counts")
            lines.append("")
            lines.append("| Stage | Count |")
            lines.append("|---|---|")
            for k, v in prisma.items():
                lines.append(f"| {k} | {v} |")
            lines.append("")

        lines.append("## Search & processing log")
        lines.append("")
        lines.append("| Timestamp | Phase | Event | Details |")
        lines.append("|---|---|---|---|")
        for rec in self.events():
            ts = rec.get("ts", "")
            event = rec.get("event", "")
            phase = rec.get("phase", "")
            details = _format_details(rec)
            lines.append(f"| {ts} | {phase} | {event} | {details} |")
        lines.append("")

        lines.append(
            "_This file was generated from provenance.jsonl and is safe to include as a "
            "reproducibility appendix._"
        )

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def _format_details(rec: dict) -> str:
    parts = []
    for k, v in rec.items():
        if k in ("ts", "event", "phase"):
            continue
        if isinstance(v, list):
            v = ",".join(str(x) for x in v)
        else:
            v = str(v)
        if len(v) > 80:
            v = v[:80] + "..."
        parts.append(f"{k}={v}")
    details = "; ".join(parts)
    return details.replace("|", "/")
