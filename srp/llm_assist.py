"""
llm_assist.py -- manual-paste AI-assist layer: compiles a paste-ready screening
prompt (records embedded) for a researcher to paste into any free web chatbot
(ChatGPT / Claude / Gemini / ...), and parses the pasted-back reply into
per-record screening decisions. No API calls, no API keys -- the prompt and
the reply both cross the browser by hand.
"""
from __future__ import annotations

import json
import re
import textwrap

DEFAULT_CRITERIA = {
    "ta": "Screen each study by TITLE and ABSTRACT for relevance to the review topic. "
          "INCLUDE if it is plausibly relevant and worth reading in full; EXCLUDE if "
          "clearly off-topic, not a research study, or out of scope; MAYBE if genuinely "
          "uncertain.",
    "ft": "Assess each study at FULL-TEXT level for final inclusion. INCLUDE only if it "
          "directly addresses the review topic with usable evidence; EXCLUDE otherwise; "
          "MAYBE if borderline.",
}

_FENCE_LINE_RE = re.compile(r"^\s*```\w*\s*$")
_LINE_SPLIT_RE = re.compile(r"\s*\|\s*|\t+|\s+-\s+|:\s+")
_DECISION_RE = re.compile(r"^(include|exclude|maybe|yes|no)\b[\s,:\-]*(.*)$", re.IGNORECASE)
_INT_RE = re.compile(r"^-?\d+$")
_DECISION_MAP = {
    "include": "include", "exclude": "exclude", "maybe": "maybe",
    "yes": "include", "no": "exclude",
}


def _is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:  # NaN
        return True
    s = str(v).strip()
    return s == "" or s.lower() == "nan"


def _fmt(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _render_record(rec: dict) -> str:
    rid = _fmt(rec.get("id", ""))
    title = rec.get("title")
    title = _fmt(title) if not _is_blank(title) else "(no title)"
    year = rec.get("year")
    venue = rec.get("venue")
    abstract = rec.get("abstract")

    lines = [f"[{rid}] {title}"]

    meta = []
    if not _is_blank(year):
        meta.append(f"Year: {_fmt(year)}")
    if not _is_blank(venue):
        meta.append(f"Venue: {_fmt(venue)}")
    if meta:
        lines.append("  ".join(meta))

    if not _is_blank(abstract):
        abstract_s = textwrap.shorten(_fmt(abstract), width=1200, placeholder=" ...")
        lines.append(f"Abstract: {abstract_s}")
    else:
        lines.append("Abstract: (not available)")

    return "\n".join(lines)


# --- core logic ---
def build_screening_prompt(records: list[dict], *, stage: str = "ta", topic: str = "",
                            criteria: str = "", batch_label: str = "") -> str:
    crit = criteria.strip() if criteria else DEFAULT_CRITERIA.get(stage, "")

    parts: list[str] = []
    if batch_label:
        parts.append(f"# Batch: {batch_label}")

    parts.append("You are assisting a systematic literature review as a screening assistant.")
    if topic:
        parts.append(f"Review topic: {topic}")
    if crit:
        parts.append(crit)

    parts.append(
        "Output format -- STRICT. Return EXACTLY one line per study below, and nothing "
        "else (no preamble, no markdown, no extra commentary):\n"
        "<id> | <INCLUDE|EXCLUDE|MAYBE> | <one short reason>\n"
        "Keep the same id shown for each study. Do not add, omit, merge, or reorder studies."
    )

    parts.append("Studies:")
    for i, rec in enumerate(records, start=1):
        parts.append(f"{i}. {_render_record(rec)}")

    return "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    lines = text.splitlines()
    kept = [ln for ln in lines if not _FENCE_LINE_RE.match(ln)]
    return "\n".join(kept)


def _coerce_id_value(value):
    if isinstance(value, int):
        return value
    s = str(value).strip().strip("[]").strip()
    if _INT_RE.match(s):
        return int(s)
    return s


def _normalize_decision(token: str):
    m = _DECISION_RE.match(token.strip())
    if not m:
        return None
    word = m.group(1).lower()
    trailing = m.group(2).strip(" ,:-")
    return _DECISION_MAP[word], trailing


def _try_parse_json(text: str) -> list[dict] | None:
    text = text.strip()
    if not text:
        return None
    candidates = [text]
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(data, list):
            continue
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if "id" not in item or "decision" not in item:
                continue
            norm = _normalize_decision(str(item["decision"]))
            if norm is None:
                continue
            decision, _trailing = norm
            out.append({
                "id": _coerce_id_value(item["id"]),
                "decision": decision,
                "reason": str(item.get("reason", "")).strip(),
            })
        if out:
            return out
    return None


def _parse_lines(text: str) -> list[dict]:
    out = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in _LINE_SPLIT_RE.split(line, maxsplit=2)]
        if len(parts) < 2:
            continue
        id_tok, decision_tok = parts[0], parts[1]
        reason = parts[2] if len(parts) > 2 else ""

        id_tok_clean = id_tok.strip("[]").strip()
        if not _INT_RE.match(id_tok_clean):
            continue

        norm = _normalize_decision(decision_tok)
        if norm is None:
            continue
        decision, trailing = norm
        if not reason and trailing:
            reason = trailing

        out.append({"id": int(id_tok_clean), "decision": decision, "reason": reason.strip()})
    return out


# --- core logic ---
def parse_screening_response(text: str) -> list[dict]:
    stripped = _strip_fences(text)

    rows = _try_parse_json(stripped)
    if rows is None:
        rows = _parse_lines(stripped)

    deduped: dict = {}
    order: list = []
    for row in rows:
        key = row["id"]
        if key not in deduped:
            order.append(key)
        deduped[key] = row
    return [deduped[k] for k in order]
