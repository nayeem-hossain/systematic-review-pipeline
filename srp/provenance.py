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

    # --- core logic: render ---
    def render_markdown(self, out_path, config: dict | None = None, prisma: dict | None = None) -> None:
        lines = ["# Review provenance", "", f"Generated: {_now()}", ""]

        lines.append("## Configuration")
        lines.append("")
        if config:
            keywords = config.get("keywords") or []
            sources = config.get("sources") or []
            if config.get("topic"):
                lines.append(f"- **Topic:** {config['topic']}")
            if keywords:
                lines.append(f"- **Keywords:** {', '.join(str(k) for k in keywords)}")
            if config.get("year_from") or config.get("year_to"):
                lines.append(f"- **Year range:** {config.get('year_from', '')}-{config.get('year_to', '')}")
            if sources:
                lines.append(f"- **Sources:** {', '.join(str(s) for s in sources)}")
            if config.get("n_phases"):
                lines.append(f"- **Phases:** {config['n_phases']}")
            if config.get("mailto"):
                lines.append(f"- **Mailto:** {config['mailto']}")
            lines.append("")
            assist_tool_name = config.get("assist_tool_name")
            if assist_tool_name:
                lines.append(
                    f"**AI-assistance disclosure:** Title/abstract screening was assisted by "
                    f"*{assist_tool_name}* used in a manual copy-paste workflow (no automated "
                    f"API calls); every AI suggestion was reviewed and confirmed by a human "
                    f"before inclusion."
                )
            else:
                lines.append("No AI assistance was used in screening.")
        else:
            lines.append("_No configuration recorded._")
        lines.append("")

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
