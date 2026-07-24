"""
figures.py -- generate the reporting-stage figures from the pipeline's own
CSVs: a PRISMA 2020 flow diagram, and quality-tier / venue-tier distribution
bar charts. Each figure is drawn directly with matplotlib (no extra
diagramming dependency) and written as both a 300 dpi PNG and a vector PDF
into --outdir.

Usage:
    python figures.py \
        --screening output/screening.csv \
        --dedup output/candidates_dedup.csv \
        --candidates output/candidates.csv \
        --quality output/extraction.csv \
        --outdir figures

PRISMA counts are derived automatically from the pipeline's own CSVs (see
"Count derivation" below). Pass --identified / --duplicates-removed /
--screened / --excluded-ta / --assessed-ft / --excluded-ft / --included to
override any single box with a final, manually-decided number -- e.g. once a
review is complete and the published diagram should show the exact numbers
reported in the manuscript, not a snapshot of an in-progress screening.csv.
Every count actually used -- derived or overridden -- is printed to stdout so
the numbers on the diagram are always traceable to a source.

Quality-tier and venue-tier charts read --quality (default
output/extraction.csv)'s `quality_tier` / `venue_tier` columns. Either chart
renders a "no data yet" placeholder instead of crashing if its column is
missing or entirely blank -- expected on a first run, before a human has
filled in the extraction template.

Layout note: the flow diagram lays the boxes out as a single vertical series
(top to bottom). Each box's height is sized to its own text, and each row is
spaced to clear the taller of its main box and its side ("excluded") box, so
nothing overlaps no matter how long the exclusion-reason list runs. The figure
is saved with a tight bounding box, so it always crops to the content.

Count derivation:
  records identified         = rows in --candidates (output/candidates.csv)
  duplicates removed         = rows in --dedup with a non-empty `duplicate_of`
                                (falls back to len(candidates) - len(dedup) if
                                `duplicate_of` isn't present, e.g. a hand-built
                                dedup file that drops rows instead of flagging them)
  records screened            = rows in --screening (screen.py already excludes
                                duplicates, so this is the canonical/unique count)
  excluded at title/abstract  = rows where ta_decision == "exclude"
  assessed at full text       = rows where ta_decision in {"include", "maybe"}
  excluded at full text       = rows where ft_decision == "exclude"
                                 (reasons summarized from ft_reason, if present)
  studies included             = rows where ft_decision == "include"
"""
# ===========================================================================
# WHAT YOU CHANGE (safe): the command-line flags -- run `python <this>.py --help`.
#   For a real review: your search terms / thresholds / paths / --mailto, etc.
#   You do NOT need to edit any code below for normal use.
# WHAT YOU DON'T CHANGE (unless an API changes or you are extending the tool):
#   the parts marked "# --- API internals ---" / "# --- core logic ---"
#   (endpoint URLs, pagination, parsing, rate-limit handling, plot geometry).
# ===========================================================================
import argparse
import re
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

# make the repo root importable so `srp` resolves when run as `python scripts/figures.py`
import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from srp.decisions import TA_PROCEED_DECISIONS  # noqa: E402

DPI = 300

# Chart chrome + categorical/ordinal steps, from this project's dataviz palette
# (light-mode slots; see the dataviz skill's references/palette.md for the
# full parameter set this was drawn from).
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BOX_FILL = "#eef4fb"          # light tint of the blue categorical slot
BOX_EDGE = "#2a78d6"          # categorical slot 1 (blue)
SIDE_FILL = "#f9f9f7"         # neutral -- excluded-count boxes, not alarm-colored
SIDE_EDGE = "#898781"
FINAL_FILL = "#cde2fb"        # a shade darker -- highlights the terminal "Included" box
ORDINAL_BLUE = ["#184f95", "#3987e5", "#86b6ef"]  # dark -> light, best -> weakest tier


def _read_csv_or_empty(path: Optional[str], **kwargs) -> pd.DataFrame:
    if not path:
        return pd.DataFrame()
    p = Path(path)
    if not p.exists():
        print(f"note: {p} not found -- treating as empty for count derivation")
        return pd.DataFrame()
    return pd.read_csv(p, encoding="utf-8", **kwargs)


def _norm(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.lower()


# --- core logic: PRISMA-count derivation ---
def derive_prisma_counts(candidates: pd.DataFrame, dedup: pd.DataFrame,
                          screening: pd.DataFrame) -> dict:
    identified = len(candidates)

    if "duplicate_of" in dedup.columns:
        duplicates_removed = int(dedup["duplicate_of"].notna().sum())
    else:
        duplicates_removed = max(0, len(candidates) - len(dedup))

    screened = len(screening)

    ta = _norm(screening.get("ta_decision", pd.Series(dtype=object)))
    ft = _norm(screening.get("ft_decision", pd.Series(dtype=object)))

    excluded_ta = int(ta.eq("exclude").sum())
    assessed_ft = int(ta.isin(TA_PROCEED_DECISIONS).sum())
    excluded_ft = int(ft.eq("exclude").sum())
    included = int(ft.eq("include").sum())

    ft_reasons = {}
    if "ft_reason" in screening.columns:
        excl_mask = ft.eq("exclude")
        reasons = screening.loc[excl_mask, "ft_reason"].dropna().astype(str).str.strip()
        reasons = reasons[reasons != ""]
        if not reasons.empty:
            ft_reasons = reasons.value_counts().to_dict()

    return {
        "identified": identified,
        "duplicates_removed": duplicates_removed,
        "screened": screened,
        "excluded_ta": excluded_ta,
        "assessed_ft": assessed_ft,
        "excluded_ft": excluded_ft,
        "included": included,
        "ft_reasons": ft_reasons,
    }


def _reason_block_lines(ft_reasons: dict, max_reasons: int = 3,
                         truncate_at: int = 46, wrap_width: int = 30) -> list:
    """Turn {reason: count} into short, wrapped, hyphen-bulleted lines that fit
    inside the side box. Raw reason strings can run to 150+ characters, so each
    is truncated and wrapped -- matplotlib does not clip text to a patch, and an
    unwrapped line would silently overrun the figure."""
    import textwrap
    lines = []
    items = list(ft_reasons.items())
    for reason, n in items[:max_reasons]:
        short = reason if len(reason) <= truncate_at else reason[:truncate_at - 1].rstrip() + "..."
        wrapped = textwrap.wrap(f"{short} (n={n})", width=wrap_width) or [""]
        lines.append(f"- {wrapped[0]}")
        lines.extend(f"  {cont}" for cont in wrapped[1:])
    if len(items) > max_reasons:
        lines.append(f"- (+{len(items) - max_reasons} more; see ft_reason)")
    return lines


def _n_lines(text: str) -> int:
    return text.count("\n") + 1


# --- core logic: plotting geometry ---
def draw_prisma_flow(counts: dict, outdir: Path):
    # Vertical (serial) layout. Each box auto-sizes to its own text; each row is
    # spaced to clear the taller of {main box, side box}, so a long exclusion
    # list never overlaps the box above or below it.
    LINE_H = 0.34        # data-units of height per line of text
    PAD_V = 0.55         # total vertical padding inside a box
    MIN_H = 1.15         # minimum box height
    GAP = 0.90           # vertical gap between consecutive rows (edge to edge)
    MAIN_W, SIDE_W = 5.9, 4.7
    MAIN_X, SIDE_X = 3.6, 9.7
    TITLE_FS, MAIN_FS, SIDE_FS, FINAL_FS, STAGE_FS = 14, 9.5, 8.5, 10.5, 9

    canonical = counts["identified"] - counts["duplicates_removed"]
    reason_lines = _reason_block_lines(counts.get("ft_reasons") or {})
    ft_excluded_text = "\n".join(
        [f"Full-text articles excluded", f"(n = {counts['excluded_ft']})"] + reason_lines
    )

    # rows, top -> bottom
    rows = [
        dict(main=f"Records identified through\ndatabase / API searching\n(n = {counts['identified']})",
             side=None, stage="IDENTIFICATION", final=False),
        dict(main=f"Records after duplicates removed\n(n = {canonical})",
             side=f"Duplicates removed\n(n = {counts['duplicates_removed']})",
             stage=None, final=False),
        dict(main=f"Records screened\n(title / abstract)\n(n = {counts['screened']})",
             side=f"Records excluded at\ntitle / abstract\n(n = {counts['excluded_ta']})",
             stage="SCREENING", final=False),
        dict(main=f"Full-text articles assessed\nfor eligibility\n(n = {counts['assessed_ft']})",
             side=ft_excluded_text, stage="ELIGIBILITY", final=False),
        dict(main=f"Studies included in synthesis\n(n = {counts['included']})",
             side=None, stage="INCLUDED", final=True),
    ]

    for r in rows:
        r["main_h"] = max(MIN_H, _n_lines(r["main"]) * LINE_H + PAD_V)
        r["side_h"] = 0.0 if not r["side"] else max(MIN_H, _n_lines(r["side"]) * LINE_H + PAD_V)
        r["row_h"] = max(r["main_h"], r["side_h"])

    total_h = sum(r["row_h"] for r in rows) + GAP * (len(rows) - 1)

    fig_h = total_h * 0.62 + 1.2
    fig, ax = plt.subplots(figsize=(9.5, fig_h), dpi=DPI)
    ax.set_xlim(0, 13)
    ax.set_ylim(-0.4, total_h + 0.4)
    ax.axis("off")

    # assign row-center y-coordinates, stacking downward
    y = total_h
    for r in rows:
        r["cy"] = y - r["row_h"] / 2.0
        y -= (r["row_h"] + GAP)

    def box(cx, cy, w, h, text, fill, edge, fs):
        ax.add_patch(FancyBboxPatch(
            (cx - w / 2, cy - h / 2), w, h,
            boxstyle="round,pad=0.03,rounding_size=0.07",
            facecolor=fill, edgecolor=edge, linewidth=1.3, zorder=2))
        ax.text(cx, cy, text, ha="center", va="center", fontsize=fs,
                color=INK, zorder=3, linespacing=1.35)

    def harrow(cy):
        ax.annotate("", xy=(SIDE_X - SIDE_W / 2, cy), xytext=(MAIN_X + MAIN_W / 2, cy),
                    arrowprops=dict(arrowstyle="-|>", color=SIDE_EDGE, lw=1.2,
                                    shrinkA=0, shrinkB=0), zorder=1)

    # boxes + side boxes + stage labels
    for r in rows:
        box(MAIN_X, r["cy"], MAIN_W, r["main_h"], r["main"],
            FINAL_FILL if r["final"] else BOX_FILL, BOX_EDGE,
            FINAL_FS if r["final"] else MAIN_FS)
        if r["side"]:
            box(SIDE_X, r["cy"], SIDE_W, r["side_h"], r["side"], SIDE_FILL, SIDE_EDGE, SIDE_FS)
            harrow(r["cy"])
        if r["stage"]:
            ax.text(0.25, r["cy"], r["stage"], ha="left", va="center",
                    fontsize=STAGE_FS, color=MUTED, fontweight="bold", rotation=90)

    # vertical arrows down the main column, from box bottom to next box top
    for i in range(len(rows) - 1):
        y0 = rows[i]["cy"] - rows[i]["main_h"] / 2
        y1 = rows[i + 1]["cy"] + rows[i + 1]["main_h"] / 2
        ax.annotate("", xy=(MAIN_X, y1), xytext=(MAIN_X, y0),
                    arrowprops=dict(arrowstyle="-|>", color=INK_SECONDARY, lw=1.3,
                                    shrinkA=0, shrinkB=0), zorder=1)

    ax.set_title("PRISMA 2020 flow diagram", fontsize=TITLE_FS, color=INK, pad=12)
    fig.savefig(outdir / "prisma_flow.png", dpi=DPI, bbox_inches="tight")
    fig.savefig(outdir / "prisma_flow.pdf", bbox_inches="tight")
    plt.close(fig)


# --- core logic: plotting geometry (official PRISMA 2020 template) ---
def draw_prisma_2020(counts: dict, outdir: Path):
    """The full official PRISMA 2020 three-column template (previous studies |
    new studies via databases & registers | new studies via other methods).

    Boxes sit on a fixed 5-lane x 6-row grid with generous inter-box gaps, and
    every label is pre-wrapped, so no text overruns its box or a neighbour. The
    numbers the pipeline actually knows (identified / duplicates / screened /
    excluded-at-TA / assessed / included) are filled in; the boxes it cannot
    know -- registers, automation tools, reports-not-retrieved, the
    previous-version column, and the other-methods column -- are left as
    "(n =  )" for the reviewer to complete, exactly like the fillable template.
    A snowball-only review can fill the other-methods column by hand."""
    from matplotlib.patches import FancyArrowPatch

    LH, PAD, MIN_H = 1.85, 2.4, 4.5
    BOXFS, HDRFS, FOOTFS = 7.6, 10.0, 6.8
    PUR_FILL, PUR_EDGE = "#e7e1f3", "#5b4a8a"
    GRY_FILL, GRY_EDGE = "#e8e8e6", "#7d7d78"

    def hh(text: str) -> float:
        return max(MIN_H, _n_lines(text) * LH + PAD)

    n_id = counts.get("identified", "")
    n_dup = counts.get("duplicates_removed", "")
    n_scr = counts.get("screened", "")
    n_exta = counts.get("excluded_ta", "")
    n_ass = counts.get("assessed_ft", "")
    n_inc = counts.get("included", "")

    reasons = counts.get("ft_reasons") or {}
    if reasons:
        rlines = []
        for reason, n in list(reasons.items())[:3]:
            short = reason if len(str(reason)) <= 24 else str(reason)[:23].rstrip() + "..."
            rlines.append(f"{short} (n={n})")
        ds4 = "Reports excluded:\n" + "\n".join(rlines)
    else:
        ds4 = ("Reports excluded:\nReason 1 (n =  )\nReason 2 (n =  )\n"
               "Reason 3 (n =  ) etc")

    p_box = ("Studies included in\nprevious version of\nreview (n =  )\n\n"
             "Reports of studies\nincluded in previous\nversion of review\n(n =  )")
    dm1 = f"Records identified from:\nDatabases (n = {n_id})\nRegisters (n =  )"
    ds1 = ("Records removed before\nscreening:\nDuplicate records\n"
           f"removed (n = {n_dup})\nRecords marked ineligible\nby automation tools (n =  )\n"
           "Records removed for other\nreasons (n =  )")
    dm2 = f"Records screened\n(n = {n_scr})"
    ds2 = f"Records excluded\n(n = {n_exta})"
    dm3 = f"Reports sought for\nretrieval (n = {n_ass})"
    ds3 = "Reports not retrieved\n(n =  )"
    dm4 = f"Reports assessed for\neligibility (n = {n_ass})"
    dm5 = (f"New studies included in\nreview (n = {n_inc})\n"
           "Reports of new included\nstudies (n =  )")
    dm6 = (f"Total studies included in\nreview (n = {n_inc})\n"
           "Reports of total included\nstudies (n =  )")
    om1 = ("Records identified from:\nWebsites (n =  )\nOrganisations (n =  )\n"
           "Citation searching (n =  ) etc")
    om3 = "Reports sought for\nretrieval (n =  )"
    os3 = "Reports not retrieved\n(n =  )"
    om4 = "Reports assessed for\neligibility (n =  )"
    os4 = ("Reports excluded:\nReason 1 (n =  )\nReason 2 (n =  )\n"
           "Reason 3 (n =  ) etc")

    # 5 lanes (x-centre, width): previous | databases-main | databases-side |
    # other-main | other-side. Row heights -- and therefore the gaps between
    # rows -- are computed from the actual box text, so a long exclusion list
    # just pushes the rows below it further down instead of overlapping them.
    xP, wP = 9.0, 15.0
    xDm, wDm = 30.0, 16.0
    xDs, wDs = 50.0, 17.0
    xOm, wOm = 72.0, 15.0
    xOs, wOs = 91.0, 15.0
    GAP = 4.5             # minimum clearance kept between any row and the next
    PUR = (PUR_FILL, PUR_EDGE)
    GRY = (GRY_FILL, GRY_EDGE)

    # each row: (name, cx, w, text, (fill, edge)). Boxes in a row share a top
    # edge; the row's height is the tallest box in it.
    rows = [
        [("p", xP, wP, p_box, GRY), ("dm1", xDm, wDm, dm1, PUR),
         ("ds1", xDs, wDs, ds1, PUR), ("om1", xOm, wOm, om1, GRY)],
        [("dm2", xDm, wDm, dm2, PUR), ("ds2", xDs, wDs, ds2, PUR)],
        [("dm3", xDm, wDm, dm3, PUR), ("ds3", xDs, wDs, ds3, PUR),
         ("om3", xOm, wOm, om3, GRY), ("os3", xOs, wOs, os3, GRY)],
        [("dm4", xDm, wDm, dm4, PUR), ("ds4", xDs, wDs, ds4, PUR),
         ("om4", xOm, wOm, om4, GRY), ("os4", xOs, wOs, os4, GRY)],
        [("dm5", xDm, wDm, dm5, PUR)],
        [("dm6", xDm, wDm, dm6, GRY)],
    ]

    row_h = [max(hh(text) for _, _, _, text, _ in row) for row in rows]
    total_h = sum(row_h) + GAP * (len(rows) - 1)

    # stack rows downward from the top; every box hangs from its row's top edge
    pos = {}   # name -> (cx, cy, w, h)
    y = total_h
    for row, rh in zip(rows, row_h):
        for name, cx, w, text, _ in row:
            h = hh(text)
            pos[name] = (cx, y - h / 2, w, h)
        y -= (rh + GAP)

    header_cy = total_h + 4.4
    title_y = header_cy + 4.4
    bottom = pos["dm6"][1] - pos["dm6"][3] / 2

    fig, ax = plt.subplots(figsize=(15, max(9.5, total_h * 0.13 + 2.6)), dpi=DPI)
    ax.set_xlim(0, 100)
    ax.set_ylim(bottom - 7, title_y + 2)
    ax.axis("off")

    def draw_box(cx, cy, w, h, text, fill, edge, fs=BOXFS):
        ax.add_patch(FancyBboxPatch(
            (cx - w / 2, cy - h / 2), w, h,
            boxstyle="round,pad=0.02,rounding_size=0.5",
            facecolor=fill, edgecolor=edge, linewidth=1.2, zorder=2))
        ax.text(cx, cy, text, ha="center", va="center", fontsize=fs,
                color=INK, zorder=3, linespacing=1.3)

    def header(cx, w, text, fill):
        ax.add_patch(FancyBboxPatch(
            (cx - w / 2, header_cy - 2.4), w, 4.8,
            boxstyle="round,pad=0.02,rounding_size=0.5",
            facecolor=fill, edgecolor=fill, linewidth=0, zorder=2))
        ax.text(cx, header_cy, text, ha="center", va="center", fontsize=HDRFS,
                color="white", fontweight="bold", zorder=3, linespacing=1.15)

    def vconn(a, b):   # vertical arrow: bottom of a -> top of b (same lane)
        cxa, cya, _, ha_ = pos[a]
        _, cyb, _, hb = pos[b]
        ax.annotate("", xy=(cxa, cyb + hb / 2), xytext=(cxa, cya - ha_ / 2),
                    arrowprops=dict(arrowstyle="-|>", color=INK_SECONDARY, lw=1.2,
                                    shrinkA=0, shrinkB=0), zorder=1)

    def hconn(a, b):   # horizontal arrow: right of main a -> left of side b
        cxa, cya, wa, _ = pos[a]
        cxb, _, wb, _ = pos[b]
        ax.annotate("", xy=(cxb - wb / 2, cya), xytext=(cxa + wa / 2, cya),
                    arrowprops=dict(arrowstyle="-|>", color=INK_SECONDARY, lw=1.2,
                                    shrinkA=0, shrinkB=0), zorder=1)

    def elbow(a, tx, ty, angle_b):   # right-angle arrow from bottom of a to (tx,ty)
        cxa, cya, _, ha_ = pos[a]
        ax.add_patch(FancyArrowPatch(
            (cxa, cya - ha_ / 2), (tx, ty),
            connectionstyle=f"angle,angleA=-90,angleB={angle_b},rad=0",
            arrowstyle="-|>", mutation_scale=12, color=INK_SECONDARY,
            lw=1.2, zorder=1))

    # column headers
    header(xP, wP, "Previous studies", GRY_EDGE)
    header(40.25, 36.5, "Identification of new studies via\ndatabases and registers", PUR_EDGE)
    header(81.5, 34.0, "Identification of new studies via\nother methods", GRY_EDGE)

    # boxes
    for row in rows:
        for name, cx, w, text, (fill, edge) in row:
            _, cy, _, h = pos[name]
            draw_box(cx, cy, w, h, text, fill, edge)

    # databases lane: vertical flow
    for a, b in [("dm1", "dm2"), ("dm2", "dm3"), ("dm3", "dm4"),
                 ("dm4", "dm5"), ("dm5", "dm6")]:
        vconn(a, b)
    # other-methods lane: vertical flow (no screening row)
    vconn("om1", "om3")
    vconn("om3", "om4")
    # main -> side (removed / excluded / not-retrieved)
    for a, b in [("dm1", "ds1"), ("dm2", "ds2"), ("dm3", "ds3"),
                 ("dm4", "ds4"), ("om3", "os3"), ("om4", "os4")]:
        hconn(a, b)
    # elbow: other-methods "assessed" feeds into "new studies included"
    dm5c = pos["dm5"]
    elbow("om4", dm5c[0] + dm5c[2] / 2, dm5c[1], 0)
    # elbow: previous studies feed into "total studies included"
    dm6c = pos["dm6"]
    elbow("p", dm6c[0] - dm6c[2] / 2, dm6c[1], 180)

    foot = (
        "*Consider, if feasible to do so, reporting the number of records identified from each\n"
        " database or register searched (rather than the total number across all databases/registers).\n"
        "†If automation tools were used, indicate how many records were excluded by a human\n"
        " and how many were excluded by automation tools."
    )
    # anchor the footnotes just below the "new studies included" box's bottom
    # edge, so the incoming other-methods arrow (which runs at that box's centre)
    # never crosses the text.
    ax.text(41.0, pos["dm5"][1] - pos["dm5"][3] / 2 - 1.5, foot, ha="left",
            va="top", fontsize=FOOTFS, color=INK_SECONDARY, linespacing=1.4)
    ax.text(50, title_y, "PRISMA 2020 flow diagram", ha="center", va="center",
            fontsize=13, color=INK, fontweight="bold")

    fig.savefig(outdir / "prisma_2020.png", dpi=DPI, bbox_inches="tight")
    fig.savefig(outdir / "prisma_2020.pdf", bbox_inches="tight")
    plt.close(fig)


def _wrap(text: str, width: int = 30) -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text, width=width))


def _tier_bar_chart(counts: dict, order: list, labels: list, colors: list,
                     title: str, ylabel: str, no_data_msg: str, outpath_base: Path):
    fig, ax = plt.subplots(figsize=(5, 4), dpi=DPI)
    values = [counts.get(k, 0) for k in order]

    if sum(values) == 0:
        ax.text(0.5, 0.5, _wrap(no_data_msg, 34), ha="center", va="center",
                 transform=ax.transAxes, fontsize=10.5, color=MUTED)
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
    else:
        bars = ax.bar(labels, values, color=colors, edgecolor=INK, linewidth=0.6, width=0.6, zorder=2)
        for rect, v in zip(bars, values):
            ax.text(rect.get_x() + rect.get_width() / 2, rect.get_height(),
                     str(v), ha="center", va="bottom", fontsize=10, color=INK)
        ax.set_ylabel(ylabel, color=INK_SECONDARY)
        ax.set_ylim(0, max(values) * 1.2 + 1)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color(AXIS)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(colors=INK_SECONDARY)
        ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)

    ax.set_title(title, color=INK, fontsize=12, pad=12)
    fig.tight_layout()
    fig.savefig(outpath_base.with_suffix(".png"), dpi=DPI, bbox_inches="tight")
    fig.savefig(outpath_base.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)


# A quality tier is a letter, optionally with a +/- modifier: A, A-, B+, B, C...
# The negative lookahead (rather than \b) is deliberate: \b after an optional
# "-" can never match at end-of-string, so "A-" would silently lose its modifier.
# This form also rejects "Awesome" while accepting "A" and "C+ borderline".
_QUALITY_TIER_RE = re.compile(r"^([ABC])\s*([+-])?(?![A-Z0-9])")
# A venue tier is T1/T2/T3, "Tier 2", or a bare 1/2/3 -- anchored at the START.
_VENUE_TIER_RE = re.compile(r"^T?(?:IER)?\s*([123])\b")


def _normalize_quality_tier(v, keep_modifier: bool = False) -> Optional[str]:
    """Normalize a quality tier.

    The README's Stage-5 rubric publishes a 7-point scale (A, A-, B+, B, B-, C+,
    C) and gives a mechanical formula for it, but this took s[0] and collapsed
    A- -> A and B+ -> B. That silently destroys the exact distinction the formula
    exists to make, in a published figure. Charts still bucket by letter (three
    bars stay readable), but the modifier is now parsed rather than assumed away,
    and unrecognized values are rejected loudly instead of being mapped to
    whatever letter happened to come first.
    """
    s = str(v).strip().upper()
    if not s or s == "NAN":
        return None
    m = _QUALITY_TIER_RE.match(s)
    if not m:
        return None
    return m.group(1) + (m.group(2) or "") if keep_modifier else m.group(1)


def _normalize_venue_tier(v) -> Optional[str]:
    """Normalize a venue tier.

    Anchored at the start. This used to scan the WHOLE string for the first of
    '1','2','3' and return on it, so 'T3 (thesis, 2021)' classified as T1 -- any
    year, page number or note containing a digit hijacked the tier. extract.py
    documents venue_tier as free text, so annotated values are expected, and the
    result was a venue-tier chart that over-reported Tier 1.
    """
    s = str(v).strip().upper()
    if not s or s == "NAN":
        return None
    m = _VENUE_TIER_RE.match(s)
    return f"T{m.group(1)}" if m else None


def draw_quality_tiers(quality: pd.DataFrame, outdir: Path):
    n = 0
    counts = {"A": 0, "B": 0, "C": 0}
    if "quality_tier" in quality.columns:
        tiers = quality["quality_tier"].map(_normalize_quality_tier).dropna()
        n = len(tiers)
        counts = tiers.value_counts().reindex(["A", "B", "C"], fill_value=0).to_dict()

    title = f"Quality-tier distribution (n = {n})" if n else "Quality-tier distribution"
    _tier_bar_chart(
        counts, order=["A", "B", "C"], labels=["A", "B", "C"], colors=ORDINAL_BLUE,
        title=title, ylabel="Studies",
        no_data_msg="No quality_tier data yet -- run extract.py, then fill in "
                     "the R/A/T/C rubric and quality_tier column.",
        outpath_base=outdir / "quality_tiers",
    )
    return n, counts


def draw_venue_tiers(quality: pd.DataFrame, outdir: Path):
    n = 0
    counts = {"T1": 0, "T2": 0, "T3": 0}
    if "venue_tier" in quality.columns:
        tiers = quality["venue_tier"].map(_normalize_venue_tier).dropna()
        n = len(tiers)
        counts = tiers.value_counts().reindex(["T1", "T2", "T3"], fill_value=0).to_dict()

    title = f"Venue-tier distribution (n = {n})" if n else "Venue-tier distribution"
    _tier_bar_chart(
        counts, order=["T1", "T2", "T3"], labels=["Tier 1", "Tier 2", "Tier 3"],
        colors=ORDINAL_BLUE, title=title, ylabel="Studies",
        no_data_msg="No venue_tier data yet -- run extract.py, then fill in "
                     "the venue_tier column.",
        outpath_base=outdir / "venue_tiers",
    )
    return n, counts


def main():
    ap = argparse.ArgumentParser(
        description="Generate the PRISMA 2020 flow diagram and quality-/venue-"
                     "tier distribution charts from the pipeline's own CSVs.")
    ap.add_argument("--screening", default="output/screening.csv")
    ap.add_argument("--dedup", default="output/candidates_dedup.csv")
    ap.add_argument("--candidates", default="output/candidates.csv")
    ap.add_argument("--quality", default="output/extraction.csv",
                     help="Extraction CSV with quality_tier / venue_tier columns")
    ap.add_argument("--outdir", default="figures")

    ap.add_argument("--identified", type=int, default=None,
                     help="Override: records identified")
    ap.add_argument("--duplicates-removed", type=int, default=None,
                     help="Override: duplicates removed")
    ap.add_argument("--screened", type=int, default=None,
                     help="Override: records screened (title/abstract)")
    ap.add_argument("--excluded-ta", type=int, default=None,
                     help="Override: excluded at title/abstract")
    ap.add_argument("--assessed-ft", type=int, default=None,
                     help="Override: full-text articles assessed for eligibility")
    ap.add_argument("--excluded-ft", type=int, default=None,
                     help="Override: full-text articles excluded")
    ap.add_argument("--included", type=int, default=None,
                     help="Override: studies included in synthesis")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    candidates = _read_csv_or_empty(args.candidates)
    dedup = _read_csv_or_empty(args.dedup)
    screening = _read_csv_or_empty(args.screening)
    quality = _read_csv_or_empty(args.quality)

    counts = derive_prisma_counts(candidates, dedup, screening)

    overrides = {
        "identified": args.identified,
        "duplicates_removed": args.duplicates_removed,
        "screened": args.screened,
        "excluded_ta": args.excluded_ta,
        "assessed_ft": args.assessed_ft,
        "excluded_ft": args.excluded_ft,
        "included": args.included,
    }
    for key, value in overrides.items():
        if value is not None:
            counts[key] = value

    print("PRISMA counts used for the flow diagram:")
    for key in ("identified", "duplicates_removed", "screened", "excluded_ta",
                "assessed_ft", "excluded_ft", "included"):
        source = "override" if overrides.get(key) is not None else "derived"
        print(f"  {key:<20} {counts[key]:>6}   ({source})")

    draw_prisma_flow(counts, outdir)
    print(f"wrote {outdir / 'prisma_flow.png'} and {outdir / 'prisma_flow.pdf'}")

    draw_prisma_2020(counts, outdir)
    print(f"wrote {outdir / 'prisma_2020.png'} and {outdir / 'prisma_2020.pdf'}")

    n_q, q_counts = draw_quality_tiers(quality, outdir)
    print(f"quality-tier counts (n={n_q}): {q_counts} "
          f"-> {outdir / 'quality_tiers.png'} / .pdf")

    n_v, v_counts = draw_venue_tiers(quality, outdir)
    print(f"venue-tier counts (n={n_v}): {v_counts} "
          f"-> {outdir / 'venue_tiers.png'} / .pdf")


if __name__ == "__main__":
    main()
