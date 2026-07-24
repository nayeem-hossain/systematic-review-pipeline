"""
Cohen's kappa is the number a peer reviewer asks for first. The degenerate case
matters most: when both raters use one identical label, agreement is total but
entirely explained by chance, and reporting kappa = 1.0 there would be a
misreported statistic in a published paper.
"""
import pytest

from srp.agreement import cohens_kappa, compare_reviewers, interpret_kappa


class TestCohensKappa:
    def test_worked_example(self):
        """Po = 35/50 = 0.70; Pe = (25/50)(30/50) + (25/50)(20/50) = 0.50;
        kappa = (0.70 - 0.50) / (1 - 0.50) = 0.40"""
        pairs = ([("include", "include")] * 20 + [("include", "exclude")] * 5
                 + [("exclude", "include")] * 10 + [("exclude", "exclude")] * 15)
        r = cohens_kappa(pairs)
        assert r.observed_agreement == pytest.approx(0.70)
        assert r.expected_agreement == pytest.approx(0.50)
        assert r.kappa == pytest.approx(0.40)
        assert r.n_compared == 50
        assert r.n_agreed == 35

    def test_perfect_agreement_with_both_labels_used(self):
        r = cohens_kappa([("include", "include")] * 10 + [("exclude", "exclude")] * 10)
        assert r.kappa == pytest.approx(1.0)

    def test_single_label_gives_undefined_not_one(self):
        """Both raters said include to everything. Pe == 1, so kappa is 0/0."""
        r = cohens_kappa([("include", "include")] * 20)
        assert r.kappa is None
        assert "undefined" in r.note
        assert r.observed_agreement == pytest.approx(1.0)

    def test_total_disagreement_is_negative(self):
        r = cohens_kappa([("include", "exclude")] * 10 + [("exclude", "include")] * 10)
        assert r.kappa < 0

    def test_chance_level_agreement_is_about_zero(self):
        pairs = ([("include", "include")] * 25 + [("include", "exclude")] * 25
                 + [("exclude", "include")] * 25 + [("exclude", "exclude")] * 25)
        assert cohens_kappa(pairs).kappa == pytest.approx(0.0)

    def test_empty_input(self):
        r = cohens_kappa([])
        assert r.kappa is None and r.n_compared == 0

    def test_blank_labels_are_dropped(self):
        r = cohens_kappa([("include", "include"), ("", "include"), ("exclude", "")])
        assert r.n_compared == 1

    def test_case_and_whitespace_insensitive(self):
        r = cohens_kappa([("INCLUDE", " include "), ("Exclude", "exclude")])
        assert r.n_agreed == 2

    def test_three_labels(self):
        pairs = [("include", "include"), ("exclude", "exclude"),
                 ("maybe", "maybe"), ("maybe", "include")]
        r = cohens_kappa(pairs)
        assert set(r.labels) == {"include", "exclude", "maybe"}
        assert r.kappa is not None


class TestInterpretKappa:
    @pytest.mark.parametrize("k, expected", [
        (0.95, "almost perfect"), (0.70, "substantial"), (0.50, "moderate"),
        (0.30, "fair"), (0.10, "slight"), (-0.20, "poor (worse than chance)"),
    ])
    def test_landis_koch_bands(self, k, expected):
        assert interpret_kappa(k) == expected


class TestCompareReviewers:
    def test_only_records_both_decided_are_compared(self):
        """Records one reviewer never touched are missing data, not disagreement.
        Folding them in would silently deflate the statistic."""
        a = {"1": {"ta_decision": "include", "title": "One"},
             "2": {"ta_decision": "exclude", "title": "Two"},
             "3": {"ta_decision": "", "title": "Undecided by A"}}
        b = {"1": {"ta_decision": "include", "title": "One"},
             "2": {"ta_decision": "include", "title": "Two"},
             "3": {"ta_decision": "exclude", "title": "Undecided by A"}}
        r = compare_reviewers(a, b)
        assert r.n_compared == 2

    def test_conflicts_are_listed_for_reconciliation(self):
        a = {"1": {"ta_decision": "include", "title": "Paper One"},
             "2": {"ta_decision": "exclude", "title": "Paper Two"}}
        b = {"1": {"ta_decision": "include", "title": "Paper One"},
             "2": {"ta_decision": "include", "title": "Paper Two"}}
        r = compare_reviewers(a, b)
        assert len(r.conflicts) == 1
        c = r.conflicts[0]
        assert c["id"] == "2" and c["reviewer_a"] == "exclude" and c["reviewer_b"] == "include"
        assert c["title"] == "Paper Two"

    def test_no_overlap_is_not_computable(self):
        r = compare_reviewers({"1": {"ta_decision": "include"}},
                               {"2": {"ta_decision": "include"}})
        assert r.kappa is None
        assert "both reviewers" in r.note

    def test_ft_stage_column(self):
        a = {"1": {"ft_decision": "include"}, "2": {"ft_decision": "exclude"}}
        b = {"1": {"ft_decision": "include"}, "2": {"ft_decision": "exclude"}}
        r = compare_reviewers(a, b, stage_col="ft_decision")
        assert r.n_compared == 2 and r.kappa == pytest.approx(1.0)

    def test_summary_is_reportable(self):
        a = {"1": {"ta_decision": "include"}, "2": {"ta_decision": "exclude"}}
        b = {"1": {"ta_decision": "include"}, "2": {"ta_decision": "exclude"}}
        assert "kappa" in compare_reviewers(a, b).summary()
