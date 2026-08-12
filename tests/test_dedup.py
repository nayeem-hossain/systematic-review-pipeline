"""
Dedup is the highest-stakes pure function in this pipeline: every record it flags
as a duplicate disappears before a human sees it (screen.py keeps only canonical
rows) and is reported as "duplicates removed" in the PRISMA diagram. A false merge
silently deletes a study from a published review and nothing downstream can
recover it. These tests pin both directions of that trade.
"""
import pandas as pd
import pytest

from conftest import make_candidates
from dedup import (dedup, normalize_doi, normalize_title, title_similarity,
                    titles_match, records_match, author_surnames, MIN_LENGTH_RATIO)


def canonical_ids(df):
    return set(df.loc[df["duplicate_of"].isna(), "id"])


def merged_ids(df):
    return set(df.loc[df["duplicate_of"].notna(), "id"])


class TestNormalizeDoi:
    @pytest.mark.parametrize("raw, expected", [
        ("10.1/a", "10.1/a"),
        ("https://doi.org/10.1/a", "10.1/a"),
        ("http://doi.org/10.1/a", "10.1/a"),
        ("https://dx.doi.org/10.1/a", "10.1/a"),
        ("http://dx.doi.org/10.1/a", "10.1/a"),
        ("https://www.doi.org/10.1/a", "10.1/a"),
        ("doi.org/10.1/a", "10.1/a"),
        ("doi:10.1/a", "10.1/a"),
        ("DOI: 10.1/a", "10.1/a"),
        ("10.1/A", "10.1/a"),
        ("  10.1/a  ", "10.1/a"),
        ("", ""),
        (None, ""),
        (float("nan"), ""),
    ])
    def test_forms(self, raw, expected):
        assert normalize_doi(raw) == expected

    def test_same_paper_from_two_sources_shares_one_key(self):
        """Crossref returns a bare DOI, OpenAlex a URL. If these normalize
        differently the same paper is counted twice."""
        assert normalize_doi("10.1002/ett.4150") == normalize_doi(
            "https://doi.org/10.1002/ett.4150")


class TestTitleSimilarity:
    def test_subset_titles_are_not_identical(self):
        """The original bug. token_set_ratio returned 100 whenever one title's
        token set was a subset of the other's, so no threshold could separate
        'X' from 'X: A Survey'."""
        short = normalize_title("Anomaly Detection in IoT Networks")
        long = normalize_title(
            "Anomaly Detection in IoT Networks Using Federated Learning: A Comprehensive Evaluation")
        assert title_similarity(short, long) < 92

    def test_length_guard_rejects_extension_titles(self):
        a = normalize_title("Deep Learning for Intrusion Detection")
        b = normalize_title("Deep Learning for Intrusion Detection: A Comprehensive Survey "
                             "of Methods, Datasets and Open Challenges")
        assert titles_match(a, b, 92) is None

    def test_word_order_and_punctuation_do_not_matter(self):
        a = normalize_title("Deep Learning for Network Intrusion Detection")
        b = normalize_title("deep  learning for network intrusion detection.")
        assert titles_match(a, b, 92) is not None

    def test_empty_titles_never_match(self):
        assert titles_match("", "", 92) is None
        assert titles_match("a real title here", "", 92) is None


class TestAuthorSurnames:
    @pytest.mark.parametrize("raw, expected", [
        ("Jane Smith", {"smith"}),
        ("Smith, Jane", {"smith"}),
        ("M.K. Divya", {"divya"}),
        ("A Author; B Writer", {"author", "writer"}),
        ("", set()),
        (None, set()),
        (float("nan"), set()),
    ])
    def test_extraction(self, raw, expected):
        assert author_surnames(raw) == expected


class TestRecordsMatch:
    def test_same_title_different_authors_is_not_a_duplicate(self):
        """Regression: the shipped example corpus has two DISTINCT book chapters
        both titled 'Machine Learning for Intrusion Detection'. Merging on title
        alone deleted one of them."""
        t = normalize_title("Machine Learning for Intrusion Detection")
        assert records_match(t, t, "M.K. Divya", "Akshay Mudgal", 92) is None

    def test_same_title_same_author_is_a_duplicate(self):
        t = normalize_title("Machine Learning for Intrusion Detection")
        assert records_match(t, t, "M.K. Divya", "Divya, M.K.", 92) is not None

    def test_missing_authors_refuses_the_merge(self):
        """Conservative by design: a missed duplicate costs one manual catch, a
        false merge deletes a study permanently."""
        t = normalize_title("Some Title")
        assert records_match(t, t, "", "", 92) is None


class TestDedupDoiPass:
    def test_exact_doi_duplicates_collapse_to_one_canonical(self):
        df = dedup(make_candidates([
            (1, "Paper A", "Jane Smith", "10.1/a", 2021),
            (2, "Paper A (repost)", "Jane Smith", "https://doi.org/10.1/A", 2021),
            (3, "Paper B", "Bob Lee", "10.1/b", 2021),
        ]))
        assert canonical_ids(df) == {1, 3}
        assert df.loc[df["id"] == 2, "duplicate_of"].iloc[0] == 1
        assert df.loc[df["id"] == 2, "dedup_method"].iloc[0] == "doi_exact"

    def test_three_records_one_doi_leaves_exactly_one(self):
        """The count this produces is the PRISMA 'duplicates removed' number."""
        df = dedup(make_candidates([
            (1, "P", "Jane Smith", "10.1/x", 2021),
            (2, "P", "Jane Smith", "10.1/x", 2021),
            (3, "P", "Jane Smith", "10.1/x", 2021),
        ]))
        assert len(canonical_ids(df)) == 1
        assert len(merged_ids(df)) == 2


class TestDedupFuzzyPass:
    def test_distinct_papers_with_subset_titles_survive(self):
        df = dedup(make_candidates([
            (1, "Anomaly Detection in IoT Networks", "Jane Smith", "", 2021),
            (2, "Anomaly Detection in IoT Networks Using Federated Learning: "
                "A Comprehensive Evaluation", "Jane Smith", "", 2021),
        ]))
        assert canonical_ids(df) == {1, 2}, "distinct papers were merged"

    def test_preprint_and_published_merge_across_year_and_doi(self):
        """The most common duplicate in a real review, and the one the original
        year-bucketed, DOI-gated implementation could never catch."""
        df = dedup(make_candidates([
            (1, "Federated Learning for Intrusion Detection", "A Author; B Writer",
             "10.1109/ACCESS.2021.123", 2021),
            (2, "Federated Learning for Intrusion Detection", "A Author; B Writer",
             "10.48550/arXiv.2005.11111", 2020),
        ]))
        assert df.loc[df["id"] == 2, "duplicate_of"].iloc[0] == 1
        assert "crossdoi" in df.loc[df["id"] == 2, "dedup_method"].iloc[0]

    def test_canonical_keeps_the_citable_doi(self):
        """references.bib is built from the canonical record; if the survivor has
        no DOI the citation is unusable."""
        df = dedup(make_candidates([
            (1, "Federated Learning for IDS", "A Author", "", 2020),
            (2, "Federated Learning for IDS", "A Author", "10.1109/x", 2021),
        ]))
        survivor = df[df["duplicate_of"].isna()].iloc[0]
        assert survivor["doi"] == "10.1109/x"

    def test_missing_year_does_not_block_a_match(self):
        """A missing year is a metadata gap, not evidence of a different paper.
        Year bucketing put NaN in its own bucket and refused the match."""
        df = dedup(make_candidates([
            (1, "Same Exact Title Here", "Jane Smith", "", 2020),
            (2, "Same Exact Title Here", "Jane Smith", "", None),
        ]))
        assert df.loc[df["id"] == 2, "duplicate_of"].iloc[0] == 1

    def test_unrelated_papers_never_merge(self):
        df = dedup(make_candidates([
            (1, "Deep Learning for Network Intrusion Detection", "Jane Smith", "", 2021),
            (2, "A Study of Honeybee Foraging Behaviour", "Bob Lee", "", 2021),
        ]))
        assert canonical_ids(df) == {1, 2}

    def test_threshold_is_honoured(self):
        rows = make_candidates([
            (1, "Deep Learning for Intrusion Detection", "Jane Smith", "", 2021),
            (2, "Deep Learning for Intrusion Prevention", "Jane Smith", "", 2021),
        ])
        assert len(canonical_ids(dedup(rows, title_threshold=100))) == 2
        assert len(canonical_ids(dedup(rows, title_threshold=50))) == 1


class TestCanonicalPrefersAbstract:
    """Canonical-record selection used to be pure first-seen order, with no
    awareness of which duplicate actually carries an abstract. That happened to
    mostly work only because keyless sources (often abstract-bearing) are queried
    before keyed ones like Scopus/IEEE (frequently abstract-less) -- incidental,
    untested, and does nothing for a paper found only by two abstract-less
    sources. Canonical selection now explicitly prefers: has DOI, then has
    abstract, then first-seen -- DOI-citability still wins over abstract
    richness, matching test_canonical_keeps_the_citable_doi's existing
    guarantee."""

    def _blank_abstract(self, df, id_):
        df.loc[df["id"] == id_, "abstract"] = ""
        return df

    def test_doi_exact_duplicate_prefers_the_row_with_an_abstract(self):
        df = make_candidates([
            (1, "Paper A", "Jane Smith", "10.1/a", 2021),
            (2, "Paper A (repost)", "Jane Smith", "10.1/a", 2021),
        ])
        df = self._blank_abstract(df, 1)  # first-seen has NO abstract
        result = dedup(df)
        survivor = result[result["duplicate_of"].isna()].iloc[0]
        assert survivor["id"] == 2
        assert survivor["abstract"] == "abstract text"

    def test_fuzzy_title_duplicate_prefers_the_row_with_an_abstract(self):
        df = make_candidates([
            (1, "Federated Learning for IDS", "A Author", "", 2020),
            (2, "Federated Learning for IDS", "A Author", "", 2020),
        ])
        df = self._blank_abstract(df, 1)
        result = dedup(df)
        survivor = result[result["duplicate_of"].isna()].iloc[0]
        assert survivor["id"] == 2

    def test_doi_presence_still_outranks_abstract_presence(self):
        """A DOI-bearing record must stay canonical even if it has no abstract
        and its DOI-less duplicate does -- citability is the primary axis,
        abstract richness only breaks ties within it."""
        df = make_candidates([
            (1, "Federated Learning for IDS", "A Author", "", 2020),
            (2, "Federated Learning for IDS", "A Author", "10.1109/x", 2021),
        ])
        df = self._blank_abstract(df, 2)  # the DOI-bearing row has no abstract
        result = dedup(df)
        survivor = result[result["duplicate_of"].isna()].iloc[0]
        assert survivor["id"] == 2
        assert survivor["doi"] == "10.1109/x"

    def test_equal_abstract_presence_falls_back_to_first_seen(self):
        df = make_candidates([
            (1, "Paper A", "Jane Smith", "10.1/a", 2021),
            (2, "Paper A (repost)", "Jane Smith", "10.1/a", 2021),
        ])
        result = dedup(df)  # both have "abstract text" via make_candidates
        survivor = result[result["duplicate_of"].isna()].iloc[0]
        assert survivor["id"] == 1

    def test_missing_abstract_column_does_not_crash(self):
        df = pd.DataFrame([
            {"id": 1, "title": "T", "authors": "Jane Smith", "doi": "10.1/a", "year": 2021},
            {"id": 2, "title": "T", "authors": "Jane Smith", "doi": "10.1/a", "year": 2021},
        ])
        result = dedup(df)
        assert len(result[result["duplicate_of"].isna()]) == 1


class TestDedupEdgeCases:
    def test_empty_frame(self):
        df = dedup(pd.DataFrame(columns=["id", "title", "authors", "doi", "year"]))
        assert len(df) == 0
        assert "duplicate_of" in df.columns

    def test_single_row(self):
        df = dedup(make_candidates([(1, "Only Paper", "Jane Smith", "10.1/a", 2021)]))
        assert canonical_ids(df) == {1}

    def test_all_titles_blank(self):
        df = dedup(make_candidates([
            (1, "", "Jane Smith", "", 2021),
            (2, "", "Jane Smith", "", 2021),
        ]))
        assert canonical_ids(df) == {1, 2}

    def test_no_authors_column_does_not_crash(self):
        df = pd.DataFrame([{"id": 1, "title": "T", "doi": "10.1/a", "year": 2021},
                            {"id": 2, "title": "T", "doi": "10.1/b", "year": 2021}])
        assert len(dedup(df)) == 2
