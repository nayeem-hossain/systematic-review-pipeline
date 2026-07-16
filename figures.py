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
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

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
    assessed_ft = int(ta.isin(["include", "maybe"]).sum())
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


def _normalize_quality_tier(v) -> Optional[str]:
    s = str(v).strip().upper()
    if not s or s == "NAN":
        return None
    letter = s[0]
    return letter if letter in ("A", "B", "C") else None


def _normalize_venue_tier(v) -> Optional[str]:
    s = str(v).strip().upper()
    for digit in ("1", "2", "3"):
        if digit in s:
            return f"T{digit}"
    return None


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

    n_q, q_counts = draw_quality_tiers(quality, outdir)
    print(f"quality-tier counts (n={n_q}): {q_counts} "
          f"-> {outdir / 'quality_tiers.png'} / .pdf")

    n_v, v_counts = draw_venue_tiers(quality, outdir)
    print(f"venue-tier counts (n={n_v}): {v_counts} "
          f"-> {outdir / 'venue_tiers.png'} / .pdf")


if __name__ == "__main__":
    main()
