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
from dataclasses import dataclass, field

# Fallback only. A systematic review screens against criteria fixed in the protocol
# BEFORE the search runs -- "plausibly relevant" is exactly the unstructured
# judgement that pre-registration exists to prevent, and a review screened this way
# cannot honestly claim to have applied its stated eligibility criteria. Callers
# should pass the review's real criteria; every caller now does, and warns loudly
# when it cannot.
DEFAULT_CRITERIA = {
    "ta": "Screen each study by TITLE and ABSTRACT for relevance to the review topic. "
          "INCLUDE if it is plausibly relevant and worth reading in full; EXCLUDE if "
          "clearly off-topic, not a research study, or out of scope; MAYBE if genuinely "
          "uncertain.",
    # No MAYBE here: ft_decision is the terminal stage (see srp/decisions.py's
    # TA_PROCEED_DECISIONS comment) -- every record has already had its full text
    # read, so a definitive call is always possible, and a "maybe" written here
    # would silently vanish from the PRISMA counts without ever being recorded as
    # an excluded-with-reason study (PRISMA item 16b).
    "ft": "Assess each study at FULL-TEXT level for final inclusion. INCLUDE only if it "
          "directly addresses the review topic with usable evidence; EXCLUDE otherwise, "
          "citing the specific reason -- even a genuinely borderline paper, since the "
          "full text is available to judge it.",
}

_STAGE_INSTRUCTION = {
    "ta": "Screen each study by TITLE and ABSTRACT.",
    "ft": "Assess each study at FULL-TEXT level for final inclusion.",
}

_FENCE_LINE_RE = re.compile(r"^\s*```\w*\s*$")
_LINE_SPLIT_RE = re.compile(r"\s*\|\s*|\t+|\s+-\s+|:\s+")
_DECISION_RE = re.compile(r"^(include|exclude|maybe|yes|no)\b[\s,:\-]*(.*)$", re.IGNORECASE)
_INT_RE = re.compile(r"^-?\d+$")
# Record ids are ints within a phase, "p<phase>_<n>" once phases are merged
# (slr.py's _merge_included_across_phases), or "sb_<n>" for citation-snowball
# results merged in later (slr.py's _menu_merge_snowball) -- both namespaces
# can appear in included_final.csv, which a full-text AI-assist batch can be
# built against.
_ID_RE = re.compile(r"^(?:p\d+_|sb_)?\d+$", re.IGNORECASE)
_DECISION_MAP = {
    "include": "include", "exclude": "exclude", "maybe": "maybe",
    "yes": "include", "no": "exclude",
}


_MAYBE_GUIDANCE = {
    "ta": "Apply ONLY the criteria below, exactly as written. Do not substitute your "
          "own judgement about what is interesting or 'plausibly relevant'. If the "
          "abstract clearly contradicts a criterion, EXCLUDE. If a specific criterion "
          "is simply not addressed by the abstract at all (neither confirmed nor "
          "contradicted), that is insufficient information, not a failure to meet it -- "
          "use MAYBE, not EXCLUDE. Use MAYBE whenever the record genuinely does not "
          "contain enough information to decide.",
    # ft_decision is the terminal stage and has no MAYBE (see DEFAULT_CRITERIA["ft"]).
    "ft": "Apply ONLY the criteria below, exactly as written. Do not substitute your "
          "own judgement about what is interesting or 'plausibly relevant'. Give a "
          "definitive INCLUDE or EXCLUDE for every study -- there is no MAYBE at the "
          "full-text stage, since the full text is available to judge every criterion.",
}


def compose_criteria(stage: str, inclusion: str = "", exclusion: str = "") -> str:
    """Build the criteria block from the review's protocol criteria.

    Returns "" when the protocol carries neither, so callers can detect the
    unconfigured case and warn rather than silently screening on vibes.
    """
    inclusion, exclusion = (inclusion or "").strip(), (exclusion or "").strip()
    if not inclusion and not exclusion:
        return ""
    parts = [_STAGE_INSTRUCTION.get(stage, _STAGE_INSTRUCTION["ta"]),
             _MAYBE_GUIDANCE.get(stage, _MAYBE_GUIDANCE["ta"])]
    if inclusion:
        parts.append("INCLUSION CRITERIA (a study must meet ALL of these):\n" + inclusion)
    if exclusion:
        parts.append("EXCLUSION CRITERIA (exclude the study if ANY of these applies):\n" + exclusion)
    return "\n\n".join(parts)


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

    # The id instruction is explicit about WHERE the id is because it used to be
    # ambiguous. Studies were rendered as "1. [61] Title" -- two integers per line --
    # under the instruction "keep the same id shown for each study". A model that
    # answered "1 | INCLUDE" meaning the FIRST study had its decision written onto
    # record id 1, a different and unrelated paper, silently. The enumeration is
    # gone: there is now exactly one number per study line.
    #
    # MAYBE is offered only at "ta" -- ft_decision is the terminal stage (see
    # srp/decisions.py's TA_PROCEED_DECISIONS), so a "maybe" written there would
    # silently vanish from the PRISMA counts with no exclusion reason recorded.
    decision_tokens = "INCLUDE|EXCLUDE|MAYBE" if stage != "ft" else "INCLUDE|EXCLUDE"
    parts.append(
        "Output format -- STRICT. Return EXACTLY one line per study below, and nothing "
        "else (no preamble, no markdown, no numbering, no extra commentary):\n"
        f"<id> | <{decision_tokens}> | <reason naming the specific criterion that drove "
        "the decision, in one short phrase>\n"
        "The <id> is the value in square brackets at the START of each study, e.g. for "
        "'[61] Deep Learning for ...' the id is 61. Copy it exactly. Do not renumber the "
        "studies, and do not add, omit, merge, or reorder them."
    )

    parts.append("Studies:")
    for rec in records:
        parts.append(_render_record(rec))

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


def _try_parse_json(text: str, unparsed: list | None = None) -> list[dict] | None:
    """Like _parse_lines, but for a JSON-array reply. A non-dict entry, a missing
    id/decision key, or a decision word _normalize_decision doesn't recognize
    (e.g. "Included" vs "include") used to vanish with zero trace -- silently
    dropped rather than surfaced, unlike the line-based path's unparsed_lines.
    That produced a factually wrong "N study/studies got no decision back"
    message for a record the model actually did answer."""
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
        dropped: list = []
        for item in data:
            if not isinstance(item, dict):
                dropped.append(json.dumps(item))
                continue
            if "id" not in item or "decision" not in item:
                dropped.append(json.dumps(item))
                continue
            norm = _normalize_decision(str(item["decision"]))
            if norm is None:
                dropped.append(json.dumps(item))
                continue
            decision, _trailing = norm
            out.append({
                "id": _coerce_id_value(item["id"]),
                "decision": decision,
                "reason": str(item.get("reason", "")).strip(),
            })
        if out:
            if unparsed is not None:
                unparsed.extend(dropped)
            return out
    return None


_LEADING_NUMBER_RE = re.compile(r"^\s*\d+\s*[.)]\s+")
_LEADING_BULLET_RE = re.compile(r"^\s*[-•+]\s+")
# Underscore is stripped only at a token boundary (not preceded/followed by a
# word char) -- e.g. "_INCLUDE_" -> "INCLUDE" -- so it only removes genuine
# markdown-italic delimiters, not the underscore inside a namespaced id like
# "p1_3" (the "p<phase>_<n>" format _ID_RE explicitly supports for merged
# phases). A blanket strip here used to turn "p1_3" into "p13", silently
# failing every reply for a namespaced batch (e.g. AI-assisting full-text
# screening against a merged included_final.csv).
_MARKDOWN_NOISE_RE = re.compile(r"[*`]|(?<!\w)_|_(?!\w)")


def _clean_line(line: str) -> str:
    """Strip the decorations chatbots add unprompted despite the strict-format
    instruction: a markdown list number ("1. 12 | INCLUDE | ..."), a bullet
    ("- 12 | INCLUDE | ..."), bold/italic markers ("**12** | INCLUDE | ..."),
    and an outer pipe-table wrapper ("| 12 | INCLUDE | ... |"). Each of these
    used to make the whole line unparseable, dropped without a word -- and a
    dash-bulleted list is one of the most common ways a chatbot renders "one
    line per item" when it isn't being followed to the letter."""
    line = _LEADING_NUMBER_RE.sub("", line)
    line = _LEADING_BULLET_RE.sub("", line)
    line = _MARKDOWN_NOISE_RE.sub("", line).strip()
    if len(line) > 1 and line.startswith("|") and line.endswith("|"):
        line = line[1:-1].strip()
    return line


def _parse_lines(text: str, unparsed: list | None = None) -> list[dict]:
    out = []
    for raw_line in text.splitlines():
        line = _clean_line(raw_line.strip())
        if not line:
            continue
        parts = [p.strip() for p in _LINE_SPLIT_RE.split(line, maxsplit=2)]
        id_tok_clean = parts[0].strip("[]").strip() if parts else ""
        if len(parts) < 2 or not _ID_RE.match(id_tok_clean):
            if unparsed is not None:
                unparsed.append(raw_line.strip())
            continue

        reason = parts[2] if len(parts) > 2 else ""
        norm = _normalize_decision(parts[1])
        if norm is None:
            if unparsed is not None:
                unparsed.append(raw_line.strip())
            continue
        decision, trailing = norm
        if not reason and trailing:
            reason = trailing

        out.append({"id": _coerce_id_value(id_tok_clean), "decision": decision,
                     "reason": reason.strip()})
    return out


@dataclass
class ParseResult:
    """Parsed decisions plus everything that did NOT parse cleanly.

    The old parser returned a bare list and silently discarded anything it could
    not read: a markdown-numbered reply produced "parsed 0 rows" with no
    explanation, contradictory duplicate ids resolved to last-wins without a word,
    and an id the model invented was applied as if legitimate. For the one artifact
    in a systematic review that must be auditable, silence is the wrong default.

    Iterable and len()-able so it behaves like the list it replaced.
    """
    decisions: list[dict] = field(default_factory=list)
    unparsed_lines: list[str] = field(default_factory=list)
    unknown_ids: list = field(default_factory=list)
    conflicts: dict = field(default_factory=dict)
    missing_ids: list = field(default_factory=list)

    def __iter__(self):
        return iter(self.decisions)

    def __len__(self) -> int:
        return len(self.decisions)

    def __getitem__(self, i):
        return self.decisions[i]

    def problems(self) -> list[str]:
        """One-line, human-readable warnings. Empty when the reply was clean."""
        out = []
        if self.unparsed_lines:
            sample = "; ".join(self.unparsed_lines[:3])
            out.append(f"{len(self.unparsed_lines)} reply line(s) could not be parsed "
                        f"and were ignored (e.g. {sample})")
        if self.unknown_ids:
            out.append(f"reply mentioned {len(self.unknown_ids)} id(s) that were not in "
                        f"this batch and were ignored: {self.unknown_ids[:5]}")
        if self.conflicts:
            detail = ", ".join(f"{k}: {'/'.join(v)}" for k, v in list(self.conflicts.items())[:3])
            out.append(f"{len(self.conflicts)} id(s) got contradictory decisions; kept the "
                        f"LAST for each ({detail}) -- review these by hand")
        if self.missing_ids:
            out.append(f"{len(self.missing_ids)} study/studies in the batch got no decision "
                        f"back: {self.missing_ids[:5]}")
        return out


# --- core logic ---
def parse_screening_response(text: str, valid_ids=None) -> ParseResult:
    """Parse a chatbot reply into decisions.

    valid_ids: the ids actually sent in this batch. Anything else the model returns
    is refused rather than written to a record it was never asked about.
    """
    stripped = _strip_fences(text)

    unparsed: list[str] = []
    rows = _try_parse_json(stripped, unparsed)
    if rows is None:
        rows = _parse_lines(stripped, unparsed)

    result = ParseResult(unparsed_lines=unparsed)

    allowed = None
    allowed_ordered: list = []
    if valid_ids is not None:
        allowed_ordered = [_coerce_id_value(v) for v in valid_ids]
        allowed = set(allowed_ordered)

    seen: dict = {}
    order: list = []
    for row in rows:
        rid = row["id"]
        if allowed is not None and rid not in allowed:
            result.unknown_ids.append(rid)
            continue
        if rid in seen:
            if seen[rid]["decision"] != row["decision"]:
                result.conflicts.setdefault(rid, [seen[rid]["decision"]]).append(row["decision"])
        else:
            order.append(rid)
        seen[rid] = row

    result.decisions = [seen[k] for k in order]
    if allowed is not None:
        # Preserve batch order (not set-iteration order) -- problems() truncates
        # this to the first 5 as a triage sample, and a scrambled order shows an
        # arbitrary, non-adjacent subset instead of the batch's actual first few.
        result.missing_ids = [i for i in allowed_ordered if i not in seen]
    return result
