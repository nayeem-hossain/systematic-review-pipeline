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
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# make the repo root importable so `srp` resolves when run as `python scripts/assist.py`
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from srp.llm_assist import build_screening_prompt, parse_screening_response

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

    batch_label = f"stage={args.stage} rows={args.start}-{args.start + len(records) - 1}"
    prompt = build_screening_prompt(
        records, stage=args.stage, topic=args.topic, batch_label=batch_label,
    )

    out_path = args.out or f"prompt_{args.stage}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    print(
        f"wrote {len(records)} record(s) to {out_path} for stage '{args.stage}'.\n"
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
    parsed = parse_screening_response(text)

    df = pd.read_csv(into_path, encoding="utf-8")
    decision_col = _DECISION_COL[args.stage]
    reason_col = _REASON_COL[args.stage]
    # An all-blank column is read as float64 (all-NaN); force object dtype so
    # writing string decisions/reasons/reviewer values below doesn't raise.
    for col in (decision_col, reason_col, "reviewer"):
        if col in df.columns:
            df[col] = df[col].astype(object)

    id_index: dict[str, int] = {}
    for idx, v in df["id"].items():
        id_index[_id_to_str(v)] = idx  # last-wins on duplicate ids in the CSV

    run_state = None
    record_key = None
    if args.run:
        try:
            from srp.state import RunState, record_key as _record_key
            run_state = RunState.load(args.run)
            record_key = _record_key
        except ImportError:
            run_state = None

    matched_ids = []
    unmatched_ids = []

    for rec in parsed:
        key = str(rec["id"])
        if key not in id_index:
            unmatched_ids.append(rec["id"])
            continue

        idx = id_index[key]
        df.at[idx, decision_col] = rec["decision"]
        df.at[idx, reason_col] = rec["reason"]
        if "reviewer" in df.columns:
            cur_reviewer = df.at[idx, "reviewer"]
            if pd.isna(cur_reviewer) or str(cur_reviewer).strip() == "":
                df.at[idx, "reviewer"] = "ai-assisted"
        matched_ids.append(rec["id"])

        if run_state is not None:
            row = df.loc[idx]
            run_state.record_decision(
                record_key=record_key(row.get("doi", ""), row.get("title", "")),
                id=_native(row.get("id")),
                decision=rec["decision"],
                stage=args.stage,
                phase=run_state.state.get("current_phase", 1),
                reason=rec["reason"],
                source="manual-paste",
                title=row.get("title", ""),
                doi=row.get("doi", ""),
            )

    df.to_csv(into_path, index=False, encoding="utf-8")

    counts = Counter(rec["decision"] for rec in parsed)
    print(f"parsed {len(parsed)} row(s) from {response_path}")
    print(f"matched {len(matched_ids)} row(s) into {into_path}")
    if unmatched_ids:
        print(f"unmatched ids ({len(unmatched_ids)}): "
              f"{', '.join(str(x) for x in unmatched_ids)}")
    print("counts by decision: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
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
    build_p.add_argument("--run", default=None,
                          help="Run directory for the cross-phase decided-keys cache "
                               "(optional; requires srp.state).")
    build_p.set_defaults(func=cmd_build)

    parse_p = sub.add_parser(
        "parse", help="Parse a saved chatbot reply into screening.csv decisions.")
    parse_p.add_argument("--response", required=True,
                          help="File containing the pasted-back chatbot reply.")
    parse_p.add_argument("--into", required=True,
                          help="screening.csv to write decisions into.")
    parse_p.add_argument("--stage", choices=["ta", "ft"], required=True,
                          help="Screening stage: ta (title/abstract) or ft (full text).")
    parse_p.add_argument("--run", default=None,
                          help="Run directory to also log decisions into RunState "
                               "(optional; requires srp.state).")
    parse_p.set_defaults(func=cmd_parse)

    return ap


def main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
