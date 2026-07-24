"""
The pilot/double-extraction workflow and the appraisal-column augmentation.
Cochrane 5.4 and Kitchenham 6.4 both expect the extraction form to be piloted
and, ideally, double-extracted -- these tests pin that the sample is
reproducible and that a NaN round-trip through CSV doesn't get miscounted as
"both extractors agreed on nothing."
"""
import pandas as pd

from conftest import make_candidates
from extract import (build_extraction_columns, build_sheet, resolve_authors,
                      ADMIN_COLUMNS, BASE_EXTRACTION_COLUMNS)
from srp.agreement import categorical_agreement


def included_frame():
    df = make_candidates([(i, f"Paper {i}", "A Author", f"10.1/{i}", 2021)
                          for i in range(1, 11)])
    return df


class TestBuildExtractionColumns:
    def test_default_has_no_appraisal_instrument(self):
        cols = build_extraction_columns()
        assert cols == BASE_EXTRACTION_COLUMNS + ADMIN_COLUMNS

    def test_admin_columns_always_last(self):
        cols = build_extraction_columns(["rob2"])
        assert cols[-3:] == ADMIN_COLUMNS

    def test_instrument_columns_appended_between_base_and_admin(self):
        cols = build_extraction_columns(["dyba_dingsoyr"])
        assert len(cols) == len(BASE_EXTRACTION_COLUMNS) + 11 + len(ADMIN_COLUMNS)

    def test_multiple_instruments_all_included(self):
        cols = build_extraction_columns(["rob2", "robins_i"])
        assert sum(c.startswith("rob2__") for c in cols) == 5
        assert sum(c.startswith("robins_i__") for c in cols) == 7

    def test_duplicate_instrument_key_does_not_duplicate_columns(self):
        """A typo'd --instruments rob2,rob2 used to emit the same column header
        twice. pandas writes that to CSV without complaint, but reading it back
        disambiguates with a '.1' suffix, silently splitting one instrument's
        data across two differently-named columns."""
        cols = build_extraction_columns(["rob2", "rob2"])
        assert len(cols) == len(set(cols))
        assert sum(c.startswith("rob2__") for c in cols) == 5

    def test_duplicate_among_multiple_distinct_instruments(self):
        cols = build_extraction_columns(["rob2", "robins_i", "rob2"])
        assert len(cols) == len(set(cols))


class TestBuildSheet:
    def test_produces_exactly_the_requested_columns(self):
        included = included_frame()
        authors = pd.Series(["A"] * len(included), index=included.index)
        cols = build_extraction_columns(["dyba_dingsoyr"])
        sheet = build_sheet(included, authors, cols)
        assert list(sheet.columns) == cols

    def test_appraisal_columns_start_blank(self):
        included = included_frame()
        authors = pd.Series(["A"] * len(included), index=included.index)
        cols = build_extraction_columns(["rob2"])
        sheet = build_sheet(included, authors, cols)
        rob2_cols = [c for c in cols if c.startswith("rob2__")]
        assert (sheet[rob2_cols] == "").all().all()


class TestPilotAndCompare:
    def test_categorical_agreement_ignores_blank_vs_blank(self):
        """The original bug: a column both extractors left blank round-trips
        through CSV as float NaN, and str(nan) == "nan" -- a non-empty string --
        so it registered as "both said nan", a spurious perfect agreement on
        columns neither reviewer had actually filled in."""
        df_a = pd.DataFrame({"id": [1, 2], "R": ["Low", float("nan")], "title": ["A", "B"]})
        df_b = pd.DataFrame({"id": [1, 2], "R": ["Low", float("nan")], "title": ["A", "B"]})
        results = categorical_agreement(df_a, df_b, ["R"])
        assert results["R"].n_compared == 1  # only id=1 -- id=2 is blank/blank, not a real pair

    def test_categorical_agreement_finds_real_disagreement(self):
        df_a = pd.DataFrame({"id": [1, 2, 3], "quality_tier": ["A", "B", "A"], "title": ["x"] * 3})
        df_b = pd.DataFrame({"id": [1, 2, 3], "quality_tier": ["A", "C", "A"], "title": ["x"] * 3})
        results = categorical_agreement(df_a, df_b, ["quality_tier"])
        r = results["quality_tier"]
        assert r.n_compared == 3
        assert len(r.conflicts) == 1
        assert r.conflicts[0]["id"] == 2

    def test_only_shared_columns_are_compared(self):
        df_a = pd.DataFrame({"id": [1], "R": ["Low"], "title": ["x"]})
        df_b = pd.DataFrame({"id": [1], "title": ["x"]})  # no R column at all
        results = categorical_agreement(df_a, df_b, ["R", "quality_tier"])
        assert results == {}


class TestResolveAuthors:
    def test_prefers_authors_already_present(self):
        included = included_frame()
        authors = resolve_authors(included, candidates_path="")
        assert list(authors) == list(included["authors"])

    def test_falls_back_to_candidates_join_when_authors_column_blank(self, tmp_path):
        included = included_frame().drop(columns=["authors"])
        cand_path = tmp_path / "candidates.csv"
        included_frame().to_csv(cand_path, index=False)
        authors = resolve_authors(included, candidates_path=str(cand_path))
        assert authors.iloc[0] == "A Author"

    def test_duplicate_ids_in_candidates_refuses_the_join(self, tmp_path, capsys):
        included = included_frame().drop(columns=["authors"])
        dupes = pd.concat([included_frame(), included_frame()], ignore_index=True)
        cand_path = tmp_path / "candidates.csv"
        dupes.to_csv(cand_path, index=False)
        authors = resolve_authors(included, candidates_path=str(cand_path))
        assert (authors == "").all()
        assert "duplicate id" in capsys.readouterr().err
