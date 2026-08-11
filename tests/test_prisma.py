"""
These tests pin the numbers that end up in the paper's abstract and flow diagram.
Every one of them was wrong, or unchecked, before.
"""
import pandas as pd
import pytest

from conftest import make_candidates
from figures import (derive_prisma_counts, _normalize_venue_tier,
                      _normalize_quality_tier, _discover_run_dir)
import slr
from srp.config import ReviewConfig
from srp.prisma import PhaseFrames, derive_prisma_counts_for_run
from srp.state import RunState


class _NullConsole:
    def print(self, *a, **k):
        pass


def build_run(tmp_path, n_phases, per_phase):
    """per_phase: list of (n_candidates, n_dupes, decisions list) per phase."""
    cfg = ReviewConfig(topic="t", mailto="a@b.c", n_phases=n_phases)
    st = RunState.create(tmp_path, "run1", cfg.to_dict())
    for phase, (n_cand, n_dupes, decisions) in enumerate(per_phase, start=1):
        pdir = st.phase_dir(phase)
        pdir.mkdir(parents=True, exist_ok=True)
        cands = make_candidates([(i, f"P{phase} Title {i}", f"Author{i} Sur{i}",
                                  f"10.{phase}/{i}", 2021) for i in range(1, n_cand + 1)])
        cands.to_csv(pdir / "candidates.csv", index=False)
        dd = cands.copy()
        dd["duplicate_of"] = None
        for i in range(n_dupes):
            dd.loc[dd.index[i], "duplicate_of"] = 999
        dd.to_csv(pdir / "candidates_dedup.csv", index=False)
        canon = dd[dd["duplicate_of"].isna()][["id", "title", "year", "venue", "doi"]].copy()
        canon["ta_decision"] = decisions
        canon["ta_reason"] = "r"
        canon["ft_decision"] = ""
        canon["ft_reason"] = ""
        canon["round"] = "openalex"
        canon["reviewer"] = "MNH"
        canon.to_csv(pdir / "screening.csv", index=False)
    return st, cfg


class TestDerivePrismaCounts:
    def test_counts_from_a_known_fixture(self):
        candidates = pd.DataFrame({"id": range(1, 11)})
        dedup = pd.DataFrame({"id": range(1, 11),
                               "duplicate_of": [None] * 8 + [1, 2]})
        screening = pd.DataFrame({
            "ta_decision": ["include", "include", "maybe", "exclude", "exclude",
                             "exclude", "include", "exclude"],
            "ft_decision": ["include", "exclude", "", "", "", "", "include", ""],
            "ft_reason": ["", "no data", "", "", "", "", "", ""],
        })
        c = derive_prisma_counts(candidates, dedup, screening)
        assert c["identified"] == 10
        assert c["duplicates_removed"] == 2
        assert c["screened"] == 8
        assert c["excluded_ta"] == 4
        assert c["assessed_ft"] == 4     # include + maybe
        assert c["excluded_ft"] == 1
        assert c["included"] == 2
        assert c["ft_reasons"] == {"no data": 1}

    def test_maybe_counts_as_assessed(self):
        """The classic off-by-definition: figures.py counts include+maybe as
        assessed at full text."""
        c = derive_prisma_counts(pd.DataFrame({"id": [1]}),
                                  pd.DataFrame({"id": [1], "duplicate_of": [None]}),
                                  pd.DataFrame({"ta_decision": ["maybe"]}))
        assert c["assessed_ft"] == 1

    def test_missing_columns_yield_zeros_not_a_crash(self):
        c = derive_prisma_counts(pd.DataFrame({"id": [1, 2]}),
                                  pd.DataFrame({"id": [1, 2]}),
                                  pd.DataFrame())
        assert c["excluded_ta"] == 0 and c["included"] == 0

    def test_empty_corpus(self):
        c = derive_prisma_counts(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        assert c["identified"] == 0 and c["included"] == 0


class TestCrossPhaseCounts:
    def test_counts_sum_across_every_phase(self, tmp_path):
        """The figure used to receive only the LAST phase's CSVs while `included`
        came from all phases, so included could exceed screened."""
        st, cfg = build_run(tmp_path, 3, [
            (50, 10, ["include"] * 10 + ["exclude"] * 30),
            (50, 10, ["include"] * 10 + ["exclude"] * 30),
            (50, 10, ["include"] * 10 + ["exclude"] * 30),
        ])
        slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        c = slr._compute_prisma_counts(st, cfg)
        assert c["identified"] == 150
        assert c["duplicates_removed"] == 30
        assert c["screened"] == 120
        assert c["excluded_ta"] == 90

    def test_figure_and_provenance_report_the_same_numbers(self, tmp_path):
        st, cfg = build_run(tmp_path, 2, [
            (10, 0, ["include"] * 3 + ["exclude"] * 7),
            (10, 0, ["include"] * 3 + ["exclude"] * 7),
        ])
        slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        c = slr._compute_prisma_counts(st, cfg)
        rows = slr._prisma_report_rows(c)
        assert rows["records identified (all phases)"] == c["identified"]
        assert rows["records screened (TA)"] == c["screened"]
        assert rows["included (final merged set)"] == c["included"]

    def test_included_requires_full_text_decisions(self, tmp_path):
        """PRISMA's included box means survived FULL-TEXT assessment. TA-includes
        with no full-text decision are assessed, not included."""
        st, cfg = build_run(tmp_path, 1, [(10, 0, ["include"] * 4 + ["exclude"] * 6)])
        slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        c = slr._compute_prisma_counts(st, cfg)
        assert c["assessed_ft"] == 4
        assert c["included"] == 0
        assert c["undecided_ft"] == 4

    def test_included_counts_ft_includes_once_recorded(self, tmp_path):
        st, cfg = build_run(tmp_path, 1, [(10, 0, ["include"] * 4 + ["exclude"] * 6)])
        merged = slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        merged["ft_decision"] = ["include", "include", "exclude", "exclude"]
        merged["ft_reason"] = ["", "", "no empirical data", "wrong population"]
        merged.to_csv(st.run_dir / "included_final.csv", index=False)
        c = slr._compute_prisma_counts(st, cfg)
        assert c["included"] == 2
        assert c["excluded_ft"] == 2
        assert c["ft_reasons"] == {"no empirical data": 1, "wrong population": 1}


class TestPrismaModule:
    """srp.prisma.derive_prisma_counts_for_run is the single multi-phase
    implementation, extracted out of slr.py so it can also be reached from
    scripts/figures.py's standalone --run-dir mode -- see srp/decisions.py's
    docstring for why a second copy of this kind of logic is a bug waiting to
    happen (that fix already happened once, for AI-decision ingestion)."""

    def test_sums_identified_and_screened_across_phases(self):
        phases = [
            PhaseFrames(
                candidates=pd.DataFrame({"id": range(1, 6)}),
                dedup=pd.DataFrame({"id": range(1, 6), "duplicate_of": [None] * 5}),
                screening=pd.DataFrame({"ta_decision": ["include", "exclude", "", "", ""]}),
            ),
            PhaseFrames(
                candidates=pd.DataFrame({"id": range(1, 4)}),
                dedup=pd.DataFrame({"id": range(1, 4), "duplicate_of": [None, None, 1]}),
                screening=pd.DataFrame({"ta_decision": ["include", "maybe"]}),
            ),
        ]
        c = derive_prisma_counts_for_run(phases, pd.DataFrame())
        assert c["identified"] == 8
        assert c["duplicates_removed"] == 1
        assert c["screened"] == 7
        assert c["excluded_ta"] == 1
        assert c["assessed_ft"] == 3  # 1 include (phase 1) + 1 include + 1 maybe (phase 2)

    def test_excluded_ft_and_included_come_from_included_final_not_any_phase_screening(self):
        """This is the exact bug this module exists to prevent: ft_decision
        lives in the run-level included_final.csv, never in a phase's own
        screening.csv -- a phase screening sheet with an ft_decision-shaped
        column must NOT be read for this."""
        phases = [PhaseFrames(
            candidates=pd.DataFrame({"id": [1, 2]}),
            dedup=pd.DataFrame({"id": [1, 2], "duplicate_of": [None, None]}),
            screening=pd.DataFrame({
                "ta_decision": ["include", "include"],
                "ft_decision": ["exclude", "exclude"],  # must be ignored -- wrong file
            }),
        )]
        included_final = pd.DataFrame({
            "ft_decision": ["include", "exclude"],
            "ft_reason": ["", "wrong population"],
        })
        c = derive_prisma_counts_for_run(phases, included_final)
        assert c["included"] == 1
        assert c["excluded_ft"] == 1
        assert c["ft_reasons"] == {"wrong population": 1}

    def test_empty_included_final_yields_zero_not_a_crash(self):
        phases = [PhaseFrames(
            candidates=pd.DataFrame({"id": [1]}),
            dedup=pd.DataFrame({"id": [1], "duplicate_of": [None]}),
            screening=pd.DataFrame({"ta_decision": ["include"]}),
        )]
        c = derive_prisma_counts_for_run(phases, pd.DataFrame())
        assert c["excluded_ft"] == 0 and c["included"] == 0

    def test_no_phases_yields_all_zeros(self):
        c = derive_prisma_counts_for_run([], pd.DataFrame())
        assert c["identified"] == 0 and c["screened"] == 0


class TestFiguresRunDirDiscovery:
    """_discover_run_dir is scripts/figures.py's standalone --run-dir
    counterpart to slr.py's state.phase_dir() loop: it must find the same
    phases and the same included_final.csv from a plain run directory on
    disk, with no RunState/ReviewConfig object required, since figures.py is
    meant to stay usable without the guided TUI."""

    def _make_run_dir(self, tmp_path, n_phases=2):
        run_dir = tmp_path / "run1"
        for phase in range(1, n_phases + 1):
            pdir = run_dir / f"phase_{phase}"
            pdir.mkdir(parents=True)
            pd.DataFrame({"id": [1, 2]}).to_csv(pdir / "candidates.csv", index=False)
            pd.DataFrame({"id": [1, 2], "duplicate_of": [None, None]}).to_csv(
                pdir / "candidates_dedup.csv", index=False)
            pd.DataFrame({"ta_decision": ["include", "exclude"]}).to_csv(
                pdir / "screening.csv", index=False)
        pd.DataFrame({"ft_decision": ["include", "exclude"],
                      "ft_reason": ["", "off-topic"]}).to_csv(
            run_dir / "included_final.csv", index=False)
        return run_dir

    def test_finds_every_phase_dir_in_numeric_order(self, tmp_path):
        run_dir = self._make_run_dir(tmp_path, n_phases=3)
        phases, _ = _discover_run_dir(run_dir)
        assert len(phases) == 3
        assert all(len(p.candidates) == 2 for p in phases)

    def test_reads_included_final_for_ft_decision(self, tmp_path):
        run_dir = self._make_run_dir(tmp_path, n_phases=1)
        _, included_final = _discover_run_dir(run_dir)
        assert list(included_final["ft_decision"]) == ["include", "exclude"]

    def test_missing_run_dir_yields_no_phases_not_a_crash(self, tmp_path):
        phases, included_final = _discover_run_dir(tmp_path / "does-not-exist")
        assert phases == []
        assert included_final.empty

    def test_ten_or_more_phases_sort_numerically_not_lexicographically(self, tmp_path):
        run_dir = tmp_path / "run1"
        for phase in (1, 2, 10):
            pdir = run_dir / f"phase_{phase}"
            pdir.mkdir(parents=True)
            pd.DataFrame({"id": [phase]}).to_csv(pdir / "candidates.csv", index=False)
            pd.DataFrame({"id": [phase], "duplicate_of": [None]}).to_csv(
                pdir / "candidates_dedup.csv", index=False)
            pd.DataFrame({"ta_decision": ["include"]}).to_csv(pdir / "screening.csv", index=False)
        phases, _ = _discover_run_dir(run_dir)
        assert [p.candidates["id"].iloc[0] for p in phases] == [1, 2, 10]


class TestRunDirCrossCheck:
    """The whole point of --run-dir: figures.py's standalone discovery must
    produce EXACTLY the counts slr._compute_prisma_counts (the guided TUI's
    already-correct path) produces for the identical on-disk run -- this is
    the tripwire that would have caught the original bug (figures.py run
    standalone on a real multi-phase run silently zeroing excluded_ft/included)."""

    def test_run_dir_agrees_with_tui_computed_counts(self, tmp_path):
        st, cfg = build_run(tmp_path, 2, [
            (10, 2, ["include"] * 4 + ["exclude"] * 4),
            (8, 1, ["include"] * 3 + ["maybe"] + ["exclude"] * 3),
        ])
        merged = slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        merged["ft_decision"] = ["include", "exclude"] * (len(merged) // 2) \
            + (["include"] if len(merged) % 2 else [])
        merged["ft_reason"] = ["", "off-topic"] * (len(merged) // 2) \
            + ([""] if len(merged) % 2 else [])
        merged.to_csv(st.run_dir / "included_final.csv", index=False)

        tui_counts = slr._compute_prisma_counts(st, cfg)

        phases, included_final = _discover_run_dir(st.run_dir)
        standalone_counts = derive_prisma_counts_for_run(phases, included_final)

        for key in ("identified", "duplicates_removed", "screened", "excluded_ta",
                    "assessed_ft", "excluded_ft", "included"):
            assert standalone_counts[key] == tui_counts[key], key


class TestTierNormalizers:
    @pytest.mark.parametrize("raw, expected", [
        ("T3 (thesis, 2021)", "T3"),   # the year's digit used to win
        ("Tier 3 - NDSS 2021", "T3"),
        ("T2, 2019", "T2"),
        ("T2 (arXiv 2021)", "T2"),
        ("T1", "T1"), ("2", "T2"),
        ("rubbish", None), ("", None), ("nan", None),
    ])
    def test_venue_tier_is_anchored_not_scanned(self, raw, expected):
        """extract.py documents venue_tier as free text, so annotations are
        expected. Scanning the whole string for the first of 1/2/3 meant any year
        or page number hijacked the tier, over-reporting Tier 1 in the chart."""
        assert _normalize_venue_tier(raw) == expected

    @pytest.mark.parametrize("raw, letter, full", [
        ("A", "A", "A"), ("A-", "A", "A-"), ("B+", "B", "B+"),
        ("B", "B", "B"), ("B-", "B", "B-"), ("C+", "C", "C+"), ("C", "C", "C"),
        ("C+ borderline", "C", "C+"),
    ])
    def test_quality_tier_keeps_the_modifier(self, raw, letter, full):
        """The README publishes a 7-point scale (A, A-, B+, B, B-, C+, C) with a
        mechanical formula. Taking s[0] destroyed the distinction the formula
        exists to make."""
        assert _normalize_quality_tier(raw) == letter
        assert _normalize_quality_tier(raw, keep_modifier=True) == full

    @pytest.mark.parametrize("raw", ["Tier A", "Awesome", "rubbish", "", "nan"])
    def test_unknown_quality_scale_is_rejected(self, raw):
        assert _normalize_quality_tier(raw) is None


class TestPrismaResiduals:
    def test_balanced_counts_produce_no_warnings(self):
        assert slr.prisma_residuals({
            "screened": 100, "excluded_ta": 70, "assessed_ft": 30,
            "undecided_ta": 0, "included": 25,
        }) == []

    def test_undecided_records_are_flagged(self):
        w = slr.prisma_residuals({"screened": 100, "excluded_ta": 70, "assessed_ft": 20,
                                   "undecided_ta": 10, "included": 0})
        assert any("no title/abstract decision" in x for x in w)

    def test_included_exceeding_assessed_is_flagged(self):
        """A study cannot be included without being assessed. This is the
        self-contradicting diagram a reviewer catches instantly."""
        w = slr.prisma_residuals({"screened": 40, "excluded_ta": 10, "assessed_ft": 30,
                                   "undecided_ta": 0, "included": 90})
        assert any("exceeds assessed_ft" in x for x in w)

    def test_unexplained_residual_is_flagged(self):
        w = slr.prisma_residuals({"screened": 100, "excluded_ta": 10, "assessed_ft": 10,
                                   "undecided_ta": 0, "included": 5})
        assert any("unexplained difference" in x for x in w)


class TestCrossPhaseMerge:
    def test_ids_are_globally_unique(self, tmp_path):
        """search.py numbers records 1..N per phase, so phase 1 and phase 2 both
        contain an id=1 for different papers."""
        st, cfg = build_run(tmp_path, 2, [(3, 0, ["include"] * 3), (3, 0, ["include"] * 3)])
        merged = slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        assert len(set(merged["id"])) == len(merged) == 6

    def test_authors_stay_attached_to_the_right_study(self, tmp_path):
        """dict(zip(id, authors)) resolved last-wins on colliding ids, silently
        attributing one paper's authors to another in references.bib."""
        st, cfg = build_run(tmp_path, 2, [(2, 0, ["include"] * 2), (2, 0, ["include"] * 2)])
        merged = slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        for _, row in merged.iterrows():
            n = row["title"].split()[-1]
            assert row["authors"] == f"Author{n} Sur{n}"

    def test_phase_provenance_is_retained(self, tmp_path):
        st, cfg = build_run(tmp_path, 2, [(2, 0, ["include"] * 2), (2, 0, ["include"] * 2)])
        merged = slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        assert set(merged["phase"]) == {1, 2}
        assert all(str(i).startswith("p") for i in merged["id"])

    def test_same_study_in_two_phases_appears_once(self, tmp_path):
        """This is the 'n = X included studies' in the abstract."""
        st, cfg = build_run(tmp_path, 2, [(2, 0, ["include"] * 2), (2, 0, ["include"] * 2)])
        # Make phase 2's first record the same work as phase 1's first.
        for phase in (1, 2):
            p = st.phase_dir(phase) / "screening.csv"
            df = pd.read_csv(p)
            df.loc[0, "doi"] = "10.1/shared"
            df.to_csv(p, index=False)
        merged = slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        assert len(merged) == 3
        assert len(merged[merged["doi"] == "10.1/shared"]) == 1

    def test_merge_rebuilds_when_screening_changes(self, tmp_path):
        """included_final.csv was a permanent cache: re-screening left download /
        extract / export all working from the previous merge."""
        import time
        st, cfg = build_run(tmp_path, 1, [(4, 0, ["include"] * 2 + ["exclude"] * 2)])
        first = slr._ensure_merged(st, cfg, _NullConsole(), force=True)
        assert len(first) == 2
        time.sleep(0.01)
        p = st.phase_dir(1) / "screening.csv"
        df = pd.read_csv(p)
        df["ta_decision"] = ["include"] * 4
        df.to_csv(p, index=False)
        assert len(slr._ensure_merged(st, cfg, _NullConsole())) == 4

    def test_empty_run_returns_empty_frame_with_columns(self, tmp_path):
        st, cfg = build_run(tmp_path, 1, [(0, 0, [])])
        merged = slr._merge_included_across_phases(st, cfg)
        assert merged.empty and "id" in merged.columns
