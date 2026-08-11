"""
assist.py -- CLI wrapper around srp.llm_assist: `build` compiles a paste-ready
screening prompt from the undecided rows of a CSV; `parse` takes a saved
chatbot reply and writes the parsed include/exclude/maybe decisions back into
screening.csv. No API calls, no API keys -- the prompt and the reply both
cross the browser by hand (paste into ChatGPT / Claude / Gemini / any web
chatbot, save the reply to a file, then parse it back in).

Usage:
    python assist.py build --in output/candidates_dedup.csv --stage ta --out prompt_ta.txt
    python assist.py parse --response reply.txt --into output/screening.csv --stage ta
"""
# ===========================================================================
# WHAT YOU CHANGE (safe): the command-line flags -- run `python assist.py --help`.
#   For a real review: your input CSV / stage / batch size / paths.
#   You do NOT need to edit any code below for normal use.
# WHAT YOU DON'T CHANGE (unless you are extending the tool):
#   the parts marked "# --- core logic ---" (prompt wording, response parsing).
# ===========================================================================
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# make the repo root importable so `srp` resolves when run as `python scripts/assist.py`
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from srp.llm_assist import build_screening_prompt, parse_screening_response, compose_criteria
from srp.decisions import apply_decisions


def _split_criteria_text(text: str) -> tuple[str, str]:
    """Split a criteria file into (inclusion, exclusion) on INCLUDE:/EXCLUDE: headers.

    Free text with no headers is treated entirely as inclusion criteria, which is
    the conservative reading: unlabelled criteria are applied, not silently halved.
    """
    lines = text.splitlines()
    inc: list[str] = []
    exc: list[str] = []
    bucket = inc
    for line in lines:
        head = line.strip().lower().rstrip(":").strip()
        if head in ("include", "inclusion", "inclusion criteria"):
            bucket = inc
            continue
        if head in ("exclude", "exclusion", "exclusion criteria"):
            bucket = exc
            continue
        bucket.append(line)
    return "\n".join(inc).strip(), "\n".join(exc).strip()


def _resolve_criteria(args) -> str:
    """Criteria precedence: --criteria > --criteria-file > the run's config.json."""
    if getattr(args, "criteria", ""):
        return compose_criteria(args.stage, args.criteria, "")
    path = getattr(args, "criteria_file", None)
    if path:
        try:
            inc, exc = _split_criteria_text(Path(path).read_text(encoding="utf-8"))
            return compose_criteria(args.stage, inc, exc)
        except OSError as e:
            print(f"error: could not read --criteria-file {path}: {e}", file=sys.stderr)
            raise SystemExit(2)
    if getattr(args, "run", None):
        try:
            from srp.state import RunState
            cfg = RunState.load(args.run).config
            return compose_criteria(args.stage,
                                     cfg.get("inclusion_criteria", ""),
                                     cfg.get("exclusion_criteria", ""))
        except (ImportError, OSError, KeyError):
            pass
    return ""

_DECISION_COL = {"ta": "ta_decision", "ft": "ft_decision"}
_REASON_COL = {"ta": "ta_reason", "ft": "ft_reason"}


def _id_to_str(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _native(v):
    """Unwrap a numpy/pandas scalar (e.g. int64) into a plain Python type so
    it survives json.dumps in RunState.record_decision."""
    item = getattr(v, "item", None)
    return item() if callable(item) else v


def _count_undecided(csv_path: Path, decision_col: str) -> int:
    """Rows in csv_path with no value in decision_col, excluding duplicates --
    the same filter build applies before slicing a batch, re-applied here so
    parse can report the same 'how much is left' number after writing."""
    df = pd.read_csv(csv_path, encoding="utf-8")
    if df.empty:
        return 0
    if "duplicate_of" in df.columns:
        df = df[df["duplicate_of"].isna()]
    if decision_col not in df.columns:
        return len(df)
    blank = df[decision_col].isna() | (df[decision_col].astype(str).str.strip() == "")
    return int(blank.sum())


def _filter_already_decided(df: pd.DataFrame, run_dir: str, stage: str) -> pd.DataFrame:
    """Drop rows whose record_key is already decided in RunState (cross-phase cache)."""
    try:
        from srp.state import RunState, record_key
    except ImportError:
        return df

    state = RunState.load(run_dir)
    decided = state.decided_keys(stage)
    if not decided:
        return df

    keep = [
        record_key(row.get("doi", ""), row.get("title", "")) not in decided
        for _, row in df.iterrows()
    ]
    return df[keep]


def cmd_build(args: argparse.Namespace) -> int:
    df = pd.read_csv(args.inp, encoding="utf-8")
    if "duplicate_of" in df.columns:
        df = df[df["duplicate_of"].isna()]

    decision_col = _DECISION_COL[args.stage]
    if decision_col in df.columns:
        undecided = df[decision_col].isna() | (df[decision_col].astype(str).str.strip() == "")
        df = df[undecided]
    # else: no decision column yet (e.g. raw candidates for a first TA pass) -> all undecided

    if args.run:
        df = _filter_already_decided(df, args.run, args.stage)

    if df.empty:
        print(f"Nothing left to screen for stage {args.stage}.", file=sys.stderr)
        return 0

    total_undecided = len(df)
    batch = df.iloc[args.start: args.start + args.batch_size]
    records = [
        {
            "id": row.get("id"),
            "title": row.get("title", ""),
            "abstract": row.get("abstract", ""),
            "year": row.get("year", ""),
            "venue": row.get("venue", ""),
        }
        for _, row in batch.iterrows()
    ]

    missing_abstracts = sum(1 for r in records if not str(r.get("abstract") or "").strip())
    if missing_abstracts and args.stage == "ta":
        print(f"WARNING: {missing_abstracts}/{len(records)} record(s) in this batch have no "
               f"abstract, but the prompt asks the model to screen by title AND abstract. "
               f"Those will be screened on title alone. Make sure --in points at a sheet "
               f"built by a current screen.py (it carries an 'abstract' column).",
               file=sys.stderr)

    criteria = _resolve_criteria(args)
    if not criteria:
        if not args.allow_generic_fallback:
            print(
                "error: no --criteria / --criteria-file / --run given, so this batch would "
                "be screened on a generic 'is this plausibly relevant' judgement instead of "
                "your protocol's eligibility criteria -- not defensible screening for a "
                "systematic review. Pass --criteria-file with your protocol's criteria, or "
                "pass --allow-generic-fallback if you genuinely intend to screen this way.",
                file=sys.stderr,
            )
            return 2
        print("WARNING: --allow-generic-fallback set -- this batch will be screened on a "
              "generic 'is this plausibly relevant' judgement, not your protocol's "
              "eligibility criteria. A review screened that way cannot claim to have "
              "applied them.", file=sys.stderr)

    batch_label = f"stage={args.stage} rows={args.start}-{args.start + len(records) - 1}"
    prompt = build_screening_prompt(
        records, stage=args.stage, topic=args.topic, criteria=criteria,
        batch_label=batch_label,
    )

    out_path = args.out or f"prompt_{args.stage}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    # Persist exactly which ids were sent in THIS batch, so `parse` can scope its
    # id-refusal check to the batch actually sent rather than the whole sheet --
    # otherwise a hallucinated/echoed id belonging to a different, not-yet-decided
    # row elsewhere in the sheet would pass validation and get written as if real.
    ids_path = f"{out_path}.ids.json"
    with open(ids_path, "w", encoding="utf-8") as f:
        json.dump([_native(r["id"]) for r in records], f)

    total_batches = -(-total_undecided // args.batch_size) if args.batch_size > 0 else 1
    batch_num = args.start // args.batch_size + 1 if args.batch_size > 0 else 1
    remaining_after = max(total_undecided - args.start - len(records), 0)
    print(
        f"wrote {len(records)} record(s) [rows {args.start + 1}-{args.start + len(records)} of "
        f"{total_undecided} undecided] to {out_path} for stage '{args.stage}' "
        f"(batch {batch_num} of {total_batches}; {remaining_after} record(s) will remain "
        f"undecided after this batch).\n"
        f"Paste this file's contents into your chatbot, then save the reply and run:\n"
        f"  python assist.py parse --response <reply.txt> --into <screening.csv> "
        f"--stage {args.stage}",
        file=sys.stderr,
    )
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    into_path = Path(args.into)
    if not into_path.exists():
        print(
            f"error: {into_path} does not exist -- run screen.py first to create it.",
            file=sys.stderr,
        )
        return 1

    response_path = Path(args.response)
    text = response_path.read_text(encoding="utf-8")

    # Pin the reply to the ids actually SENT in this batch, not every id in the
    # whole sheet -- otherwise a hallucinated/echoed id belonging to a different,
    # not-yet-decided row elsewhere in the sheet passes validation unnoticed and
    # gets written as if the model had genuinely been asked about it.
    valid_ids = None
    if args.prompt:
        ids_path = Path(f"{args.prompt}.ids.json")
        if ids_path.exists():
            valid_ids = json.loads(ids_path.read_text(encoding="utf-8"))
        else:
            print(f"warning: --prompt given but {ids_path} not found (was it built "
                  f"with this version of assist.py?) -- falling back to whole-sheet "
                  f"id validation.", file=sys.stderr)
    if valid_ids is None:
        print("warning: no --prompt given, so ids are validated against the WHOLE "
              "sheet, not just the batch actually sent -- a hallucinated or echoed "
              "id belonging to a different, undecided row elsewhere in the sheet "
              "would not be caught. Pass --prompt <the prompt file build wrote> to "
              "scope this properly.", file=sys.stderr)
        sheet = pd.read_csv(into_path, encoding="utf-8")
        valid_ids = list(sheet["id"]) if "id" in sheet.columns else None
    parsed = parse_screening_response(text, valid_ids=valid_ids)
    for problem in parsed.problems():
        print(f"reply check: {problem}", file=sys.stderr)

    run_state = None
    if args.run:
        try:
            from srp.state import RunState
            run_state = RunState.load(args.run)
        except (ImportError, OSError) as e:
            print(f"note: could not load run state at {args.run} ({e}) -- decisions will "
                   f"be written to the CSV but not to the append-only log", file=sys.stderr)

    phase = run_state.state.get("current_phase", 1) if run_state is not None else 1
    applied = apply_decisions(into_path, parsed, run_state, phase,
                               stage=args.stage, overwrite=args.overwrite)

    print(f"parsed {len(parsed)} row(s) from {response_path}")
    print(f"matched {len(applied.matched)} row(s) into {into_path}")
    for problem in applied.problems():
        print(f"apply check: {problem}", file=sys.stderr)
    print("counts by decision: "
          + (", ".join(f"{k}={v}" for k, v in sorted(applied.counts.items())) or "(none)"))

    remaining = _count_undecided(into_path, _DECISION_COL[args.stage])
    if remaining:
        print(f"{remaining} record(s) still undecided for stage '{args.stage}' in {into_path}.")
    else:
        print(f"All records now decided for stage '{args.stage}' in {into_path}.")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Manual-paste AI-assist layer for the SLR pipeline: compile a "
                     "paste-ready screening prompt, or parse a pasted chatbot reply "
                     "back into screening.csv decisions. No API calls, no API keys.")
    sub = ap.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser(
        "build", help="Compile a paste-ready screening prompt from undecided rows.")
    build_p.add_argument("--in", dest="inp", required=True,
                          help="Input CSV (candidates_dedup.csv or screening.csv).")
    build_p.add_argument("--stage", choices=["ta", "ft"], required=True,
                          help="Screening stage: ta (title/abstract) or ft (full text).")
    build_p.add_argument("--out", default=None,
                          help="Output prompt file (default prompt_<stage>.txt).")
    build_p.add_argument("--batch-size", type=int, default=20,
                          help="Records per batch (default 20).")
    build_p.add_argument("--start", type=int, default=0,
                          help="Row offset into the undecided set (default 0).")
    build_p.add_argument("--topic", default="",
                          help="Review topic line included in the prompt.")
    build_p.add_argument("--criteria-file", default=None,
                          help="File holding your protocol's eligibility criteria, applied "
                               "verbatim in the prompt. Use 'INCLUDE:' and 'EXCLUDE:' "
                               "section headers to separate them, or pass free text. "
                               "STRONGLY recommended: without it the prompt asks only for "
                               "generic relevance, which is not a screening criterion.")
    build_p.add_argument("--criteria", default="",
                          help="Eligibility criteria as an inline string (alternative to "
                               "--criteria-file).")
    build_p.add_argument("--run", default=None,
                          help="Run directory for the cross-phase decided-keys cache "
                               "(optional; requires srp.state). If given, criteria are "
                               "read from its config.json unless overridden above.")
    build_p.add_argument("--allow-generic-fallback", action="store_true",
                          help="Required if none of --criteria / --criteria-file / --run "
                               "resolve to real eligibility criteria: without this flag, "
                               "build refuses to write a prompt that would screen on a "
                               "generic 'plausibly relevant' judgement instead of your "
                               "protocol's criteria.")
    build_p.set_defaults(func=cmd_build)

    parse_p = sub.add_parser(
        "parse", help="Parse a saved chatbot reply into screening.csv decisions.")
    parse_p.add_argument("--response", required=True,
                          help="File containing the pasted-back chatbot reply.")
    parse_p.add_argument("--prompt", default=None,
                          help="The prompt file `build` wrote for this exact batch "
                               "(e.g. prompt_ta.txt). Strongly recommended: without it, "
                               "ids are validated against the whole sheet rather than just "
                               "this batch, so a hallucinated/echoed id belonging to a "
                               "different undecided row would not be caught.")
    parse_p.add_argument("--into", required=True,
                          help="screening.csv to write decisions into.")
    parse_p.add_argument("--stage", choices=["ta", "ft"], required=True,
                          help="Screening stage: ta (title/abstract) or ft (full text).")
    parse_p.add_argument("--run", default=None,
                          help="Run directory to also log decisions into RunState "
                               "(optional; requires srp.state).")
    parse_p.add_argument("--overwrite", action="store_true",
                          help="Replace decisions that are already recorded. Default is to "
                               "leave them alone: re-parsing a stale reply used to silently "
                               "overwrite a human's decision while leaving the human's "
                               "initials on the row.")
    parse_p.set_defaults(func=cmd_parse)

    return ap


def main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
