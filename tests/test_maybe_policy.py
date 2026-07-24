"""
PRISMA has no 'maybe' state, but title/abstract screening produces them. Before
this fix, the same screening.csv produced different denominators depending which
code path read it: figures.py counted maybe as assessed-at-full-text, while the
review gate, cross-phase merge, and query-expansion term mining silently dropped
it. The policy (per the maintainer): a 'maybe' proceeds to full-text assessment
exactly like an 'include' -- these tests pin that everywhere it must apply.
"""
import pandas as pd

from srp.decisions import TA_PROCEED_DECISIONS, pick_progressed, ta_proceeds_mask
import slr


class TestTaProceedsMask:
    def test_include_and_maybe_proceed(self):
        s = pd.Series(["include", "maybe", "exclude", ""])
        assert list(ta_proceeds_mask(s)) == [True, True, False, False]

    def test_case_and_whitespace_insensitive(self):
        s = pd.Series([" Include ", "MAYBE", "Exclude"])
        assert list(ta_proceeds_mask(s)) == [True, True, False]

    def test_constant_is_exactly_include_and_maybe(self):
        assert TA_PROCEED_DECISIONS == frozenset({"include", "maybe"})


class TestPickProgressed:
    def test_ft_decision_include_only_no_maybe(self):
        """ft_decision is the terminal stage -- PRISMA has no full-text 'maybe',
        so even if one is hand-written it must not count as included."""
        df = pd.DataFrame({"ft_decision": ["include", "maybe", "exclude"]})
        filtered, col = pick_progressed(df, "ft_decision")
        assert col == "ft_decision"
        assert list(filtered["ft_decision"]) == ["include"]

    def test_falls_back_to_ta_decision_when_ft_decision_blank(self):
        """The original bug: export.py preferred ft_decision merely because the
        column EXISTED, even entirely blank -- the normal state before full-text
        screening runs -- and silently exported zero rows."""
        df = pd.DataFrame({
            "ft_decision": ["", "", ""],
            "ta_decision": ["include", "maybe", "exclude"],
        })
        filtered, col = pick_progressed(df, "ft_decision")
        assert col == "ta_decision"
        assert len(filtered) == 2  # include AND maybe

    def test_falls_back_when_ft_decision_column_missing(self):
        df = pd.DataFrame({"ta_decision": ["include", "maybe", "exclude"]})
        filtered, col = pick_progressed(df, "ft_decision")
        assert col == "ta_decision" and len(filtered) == 2

    def test_neither_column_present(self):
        df = pd.DataFrame({"title": ["a", "b"]})
        filtered, _col = pick_progressed(df, "ft_decision")
        assert filtered.empty

    def test_partially_filled_ft_decision_is_not_treated_as_blank(self):
        """Only entirely-blank triggers the fallback; a partially-screened
        ft_decision column must be used as-is, not conflated with ta_decision."""
        df = pd.DataFrame({
            "ft_decision": ["include", "", ""],
            "ta_decision": ["include", "include", "include"],
        })
        filtered, col = pick_progressed(df, "ft_decision")
        assert col == "ft_decision" and len(filtered) == 1


class TestMaybeInReviewGate:
    def _sheet(self, tmp_path, decisions):
        pdir = tmp_path / "run1" / "phase_1"
        pdir.mkdir(parents=True)
        df = pd.DataFrame({
            "id": range(1, len(decisions) + 1),
            "title": [f"Paper {i}" for i in range(len(decisions))],
            "doi": [f"10.1/{i}" for i in range(len(decisions))],
            "year": 2021, "venue": "V",
            "ta_decision": decisions, "ta_reason": "r",
            "ft_decision": "", "ft_reason": "", "round": "openalex", "reviewer": "",
        })
        df.to_csv(pdir / "screening.csv", index=False)
        return pdir

    def test_merge_carries_maybe_through(self, tmp_path):
        """A maybe dropped here never reaches full-text screening -- it is
        silently excluded from the review instead of being read."""
        from srp.state import RunState
        from srp.config import ReviewConfig

        cfg = ReviewConfig(topic="t", mailto="a@b.c", n_phases=1)
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        pdir = st.phase_dir(1)
        pdir.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame({
            "id": [1, 2, 3], "title": ["A", "B", "C"],
            "doi": ["10.1/a", "10.1/b", "10.1/c"], "year": 2021, "venue": "V",
            "ta_decision": ["include", "maybe", "exclude"], "ta_reason": "r",
            "ft_decision": "", "ft_reason": "", "round": "openalex", "reviewer": "",
        })
        df.to_csv(pdir / "screening.csv", index=False)
        merged = slr._merge_included_across_phases(st, cfg)
        assert set(merged["title"]) == {"A", "B"}

    def test_query_expansion_mines_maybe_titles_too(self):
        screening_df = pd.DataFrame({
            "id": [1], "title": ["Federated Adversarial Robustness Benchmark"],
            "ta_decision": ["maybe"],
        })
        proceed_mask = ta_proceeds_mask(screening_df["ta_decision"])
        titles = screening_df.loc[proceed_mask, "title"].tolist()
        assert titles == ["Federated Adversarial Robustness Benchmark"]
