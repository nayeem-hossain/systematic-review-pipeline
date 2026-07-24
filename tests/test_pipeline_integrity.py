"""
Cross-cutting integrity tests: the properties whose silent breakage would put a
wrong number, a wrong author, or a leaked credential into a published artifact.
"""
import json
import os

import pandas as pd
import pytest

from conftest import make_candidates
from screen import build_sheet, merge_decisions, decided_ids, SCREENING_COLUMNS
from srp.config import ReviewConfig, SECRET_FIELDS
from srp.decisions import apply_decisions, AI_REVIEWER, id_to_str
from srp.env import parse_env_text, load_dotenv
from srp.normalize import normalize_doi, normalize_title, record_key
from srp.state import RunState
import dedup


class TestKeywordBlocksAndDisplay:
    """search_query() and display_keywords() are the ONE place that knows the
    precedence between keyword_blocks (OR-within, AND-across -- what the
    wizard now builds) and the legacy flat `keywords` field. Every other module
    (methods_report.py, provenance.py) reuses these rather than re-deriving the
    query, specifically because a second implementation previously read only
    the flat field and silently produced "(no keywords recorded)" for every
    guided-wizard review.
    """

    def test_search_query_prefers_blocks_over_flat_keywords(self):
        cfg = ReviewConfig(topic="t", keywords=["stale", "ignored"],
                            keyword_blocks=[["intrusion detection", "IDS"]])
        assert cfg.search_query() == '("intrusion detection" OR IDS)'

    def test_search_query_falls_back_to_flat_keywords(self):
        cfg = ReviewConfig(topic="t", keywords=["intrusion detection", "machine learning"])
        assert cfg.search_query() == '"intrusion detection" AND "machine learning"'

    def test_all_empty_blocks_produce_empty_query_not_a_crash(self):
        cfg = ReviewConfig(topic="t", keyword_blocks=[[], []])
        assert cfg.search_query() == ""

    def test_one_empty_block_among_real_ones_is_skipped(self):
        cfg = ReviewConfig(topic="t", keyword_blocks=[[], ["b"]])
        assert cfg.search_query() == "b"

    def test_display_keywords_matches_search_query_structure(self):
        cfg = ReviewConfig(topic="t", keyword_blocks=[["a", "b"], ["c"]])
        assert cfg.display_keywords() == "(a OR b) AND c"

    def test_display_keywords_falls_back_to_flat_list(self):
        cfg = ReviewConfig(topic="t", keywords=["x", "y"])
        assert cfg.display_keywords() == "x, y"

    def test_display_keywords_empty_block_does_not_misalign(self):
        """A prior ad hoc implementation built one list filtered by truthiness
        and indexed a SECOND, unfiltered list by position -- an empty block in
        the middle shifted every later block's rendering out of alignment."""
        cfg = ReviewConfig(topic="t", keyword_blocks=[["a"], [], ["b", "c"]])
        assert cfg.display_keywords() == "a AND (b OR c)"


class TestSecretsNeverPersist:
    """runs/<id>/ is the artifact users are told to share with co-authors and
    attach as a reproducibility appendix. It must not contain credentials."""

    def test_to_dict_drops_every_secret(self):
        cfg = ReviewConfig(topic="t", s2_api_key="S2SECRET", ieee_api_key="IEEESECRET",
                            scopus_api_key="SCSECRET", scopus_insttoken="TOKSECRET",
                            springer_api_key="SPSECRET", core_api_key="CORESECRET",
                            pubmed_api_key="PMSECRET")
        blob = json.dumps(cfg.to_dict())
        assert "SECRET" not in blob
        for name in SECRET_FIELDS:
            assert name not in cfg.to_dict()

    def test_config_json_on_disk_has_no_secrets(self, tmp_path):
        cfg = ReviewConfig(topic="t", ieee_api_key="IEEESECRET")
        RunState.create(tmp_path, "run1", cfg.to_dict())
        assert "IEEESECRET" not in (tmp_path / "run1" / "config.json").read_text(encoding="utf-8")

    def test_configured_key_names_returns_names_not_values(self):
        cfg = ReviewConfig(topic="t", ieee_api_key="IEEESECRET")
        assert "IEEESECRET" not in " ".join(cfg.configured_key_names())
        assert "ieee" in cfg.configured_key_names()

    def test_secret_env_maps_to_the_vars_the_scripts_read(self):
        cfg = ReviewConfig(topic="t", ieee_api_key="k1", scopus_insttoken="k2")
        assert cfg.secret_env() == {"IEEE_API_KEY": "k1", "SCOPUS_INSTTOKEN": "k2"}

    def test_blank_keys_are_not_forwarded(self):
        assert ReviewConfig(topic="t").secret_env() == {}

    def test_resume_rehydrates_from_env(self, monkeypatch):
        monkeypatch.setenv("IEEE_API_KEY", "from-env")
        cfg = ReviewConfig.from_dict(ReviewConfig(topic="t", ieee_api_key="typed").to_dict())
        assert cfg.ieee_api_key == ""
        assert cfg.hydrate_secrets_from_env() == ["ieee_api_key"]
        assert cfg.ieee_api_key == "from-env"

    def test_hydrate_does_not_clobber_an_explicit_value(self, monkeypatch):
        monkeypatch.setenv("IEEE_API_KEY", "from-env")
        cfg = ReviewConfig(topic="t", ieee_api_key="explicit")
        cfg.hydrate_secrets_from_env()
        assert cfg.ieee_api_key == "explicit"

    def test_old_config_without_new_fields_still_loads(self):
        cfg = ReviewConfig.from_dict({"topic": "t", "keywords": ["a"], "mailto": "x@y.z"})
        assert cfg.topic == "t" and cfg.ieee_api_key == ""


class TestDotenv:
    def test_parses_the_shipped_example_shapes(self):
        parsed = parse_env_text(
            "# comment\n"
            "MAILTO=you@example.com\n"
            "S2_API_KEY=\n"
            "export IEEE_API_KEY=abc123\n"
            'SCOPUS_INSTTOKEN="quoted value"\n'
            "CORE_API_KEY=k3y  # inline comment\n"
            "NO_EQUALS_LINE\n"
        )
        assert parsed == {
            "MAILTO": "you@example.com", "S2_API_KEY": "",
            "IEEE_API_KEY": "abc123", "SCOPUS_INSTTOKEN": "quoted value",
            "CORE_API_KEY": "k3y",
        }

    def test_hash_inside_a_value_survives(self):
        assert parse_env_text("K=pa#ss")["K"] == "pa#ss"

    def test_real_env_var_wins_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAILTO", "real@env")
        (tmp_path / ".env").write_text("MAILTO=file@env\n", encoding="utf-8")
        load_dotenv(tmp_path / ".env")
        assert os.environ["MAILTO"] == "real@env"

    def test_override_true_replaces(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAILTO", "real@env")
        (tmp_path / ".env").write_text("MAILTO=file@env\n", encoding="utf-8")
        load_dotenv(tmp_path / ".env", override=True)
        assert os.environ["MAILTO"] == "file@env"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load_dotenv(tmp_path / "nope.env") == []


class TestNormalizersAgree:
    def test_dedup_and_state_share_one_implementation(self):
        """state.py used to carry its own copy under a comment claiming it matched
        dedup.py. It did not: they disagreed on 'doi.org/10.1/x'."""
        assert dedup.normalize_doi is normalize_doi
        assert dedup.normalize_title is normalize_title

    @pytest.mark.parametrize("raw", [
        "doi.org/10.1/X", "https://doi.org/10.1/x", "doi:10.1/x", "10.1/x", "",
    ])
    def test_same_answer_from_both_entry_points(self, raw):
        assert dedup.normalize_doi(raw) == normalize_doi(raw)

    def test_record_key_prefers_doi(self):
        assert record_key("10.1/x", "A Title") == "doi:10.1/x"

    def test_record_key_falls_back_to_title(self):
        assert record_key("", "A Title!") == "title:a title"

    def test_record_key_empty(self):
        assert record_key("", "") == ""


class TestAtomicState:
    def test_interrupted_save_does_not_destroy_the_checkpoint(self, tmp_path, monkeypatch):
        st = RunState.create(tmp_path, "run1", {"topic": "t"})
        st.state["stages"]["1:search"] = {"done": True, "counts": {"n_hits": 90}}
        st.save()
        good = (st.run_dir / "state.json").read_text(encoding="utf-8")

        def boom(obj, fp, **kw):
            fp.write('{"run_id": "run1", "stages": {"1:sea')
            raise KeyboardInterrupt

        monkeypatch.setattr(json, "dump", boom)
        with pytest.raises(KeyboardInterrupt):
            st.state["current_phase"] = 2
            st.save()
        monkeypatch.undo()

        assert (st.run_dir / "state.json").read_text(encoding="utf-8") == good
        assert RunState.load(st.run_dir).state["stages"]["1:search"]["counts"]["n_hits"] == 90

    def test_no_temp_files_left_behind(self, tmp_path):
        st = RunState.create(tmp_path, "run1", {"topic": "t"})
        st.save()
        assert not [p for p in os.listdir(st.run_dir) if p.endswith(".tmp")]


class TestScreenPreservesDecisions:
    def _decided(self, tmp_path):
        cands = make_candidates([(i, f"Paper {i}", "A Author", f"10.1/{i}", 2021)
                                  for i in range(1, 6)])
        cands["duplicate_of"] = None
        sheet = build_sheet(cands)
        sheet["ta_decision"] = ["include", "include", "exclude", "", ""]
        sheet["reviewer"] = ["MNH", "MNH", "MNH", "", ""]
        return cands, sheet

    def test_rerun_preserves_every_decision(self, tmp_path):
        cands, existing = self._decided(tmp_path)
        merged, stats = merge_decisions(build_sheet(cands), existing)
        assert list(merged["ta_decision"]) == ["include", "include", "exclude", "", ""]
        assert list(merged["reviewer"])[:3] == ["MNH", "MNH", "MNH"]
        assert stats["carried"] == 3

    def test_new_records_arrive_blank(self, tmp_path):
        cands, existing = self._decided(tmp_path)
        bigger = pd.concat([cands, make_candidates([(99, "New Paper", "B", "10.1/99", 2022)])],
                            ignore_index=True)
        bigger["duplicate_of"] = None
        merged, stats = merge_decisions(build_sheet(bigger), existing)
        assert stats["new"] == 1
        assert merged.loc[merged["id"] == 99, "ta_decision"].iloc[0] == ""

    def test_dropped_decided_records_are_counted_not_silent(self, tmp_path):
        cands, existing = self._decided(tmp_path)
        smaller = cands[cands["id"] != 1]
        _, stats = merge_decisions(build_sheet(smaller), existing)
        assert stats["dropped_decided"] == 1

    def test_first_build_reports_no_carry(self):
        cands = make_candidates([(1, "P", "A", "10.1/1", 2021)])
        cands["duplicate_of"] = None
        _, stats = merge_decisions(build_sheet(cands), None)
        assert stats["carried"] == 0 and stats["new"] == 1

    def test_abstract_is_carried_through(self):
        """Without it, assist.py renders 'Abstract: (not available)' for every
        record while the prompt says to screen by title AND abstract."""
        cands = make_candidates([(1, "P", "A", "10.1/1", 2021)])
        cands["duplicate_of"] = None
        sheet = build_sheet(cands)
        assert "abstract" in SCREENING_COLUMNS
        assert sheet["abstract"].iloc[0] == "abstract text"

    def test_duplicates_are_excluded_from_the_sheet(self):
        cands = make_candidates([(1, "P", "A", "10.1/1", 2021), (2, "P", "A", "10.1/1", 2021)])
        cands["duplicate_of"] = [None, 1]
        assert list(build_sheet(cands)["id"]) == [1]

    def test_decided_ids_detects_either_stage(self):
        df = pd.DataFrame({"id": [1, 2, 3], "ta_decision": ["include", "", ""],
                            "ft_decision": ["", "exclude", ""]})
        assert decided_ids(df) == {1, 2}


class TestDecisionIngestion:
    def _sheet(self, tmp_path, ta=("", "")):
        p = tmp_path / "screening.csv"
        pd.DataFrame({
            "id": [1, 2], "title": ["One", "Two"], "doi": ["10.1/1", "10.1/2"],
            "ta_decision": list(ta), "ta_reason": ["", ""],
            "ft_decision": ["", ""], "ft_reason": ["", ""],
            "reviewer": ["MNH" if ta[0] else "", ""],
        }).to_csv(p, index=False)
        return p

    def test_applies_to_blank_rows(self, tmp_path):
        p = self._sheet(tmp_path)
        r = apply_decisions(p, [{"id": 1, "decision": "include", "reason": "ok"}],
                             None, phase=1)
        df = pd.read_csv(p)
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "include"
        assert df.loc[df["id"] == 1, "reviewer"].iloc[0] == AI_REVIEWER
        assert r.matched == [1]

    def test_never_overwrites_an_existing_decision_by_default(self, tmp_path):
        """The AI used to overwrite a human's decision while LEAVING the human's
        initials on the row -- a false attribution in the file that exists to make
        inter-rater reliability computable."""
        p = self._sheet(tmp_path, ta=("include", ""))
        r = apply_decisions(p, [{"id": 1, "decision": "exclude", "reason": "AI says no"}],
                             None, phase=1)
        df = pd.read_csv(p)
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "include"
        assert df.loc[df["id"] == 1, "reviewer"].iloc[0] == "MNH"
        assert r.skipped_decided == [1]

    def test_overwrite_moves_attribution_to_the_ai(self, tmp_path):
        p = self._sheet(tmp_path, ta=("include", ""))
        apply_decisions(p, [{"id": 1, "decision": "exclude", "reason": "x"}],
                         None, phase=1, overwrite=True)
        df = pd.read_csv(p)
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "exclude"
        assert df.loc[df["id"] == 1, "reviewer"].iloc[0] == AI_REVIEWER

    def test_unknown_ids_reported(self, tmp_path):
        p = self._sheet(tmp_path)
        r = apply_decisions(p, [{"id": 99, "decision": "include", "reason": ""}],
                             None, phase=1)
        assert r.unmatched == [99] and r.matched == []

    def test_ft_stage_writes_the_ft_columns(self, tmp_path):
        p = self._sheet(tmp_path)
        apply_decisions(p, [{"id": 1, "decision": "exclude", "reason": "no data"}],
                         None, phase=1, stage="ft")
        df = pd.read_csv(p)
        assert df.loc[df["id"] == 1, "ft_decision"].iloc[0] == "exclude"
        assert pd.isna(df.loc[df["id"] == 1, "ta_decision"].iloc[0])

    def test_ft_stage_refuses_a_maybe_rather_than_writing_it(self, tmp_path):
        """ft_decision is terminal (include/exclude only) -- the prompt no longer
        offers MAYBE at this stage, but a chatbot can still write one, and it must
        be refused rather than silently written where it would vanish from the
        PRISMA counts with no exclusion reason recorded (PRISMA item 16b)."""
        p = self._sheet(tmp_path)
        r = apply_decisions(p, [{"id": 1, "decision": "maybe", "reason": "borderline"},
                                 {"id": 2, "decision": "include", "reason": "clear"}],
                             None, phase=1, stage="ft")
        df = pd.read_csv(p)
        assert pd.isna(df.loc[df["id"] == 1, "ft_decision"].iloc[0])
        assert df.loc[df["id"] == 2, "ft_decision"].iloc[0] == "include"
        assert r.matched == [2]
        assert r.rejected_ft_maybe == [1]
        assert any("maybe" in p for p in r.problems())

    def test_ta_stage_maybe_still_writes_normally(self, tmp_path):
        p = self._sheet(tmp_path)
        r = apply_decisions(p, [{"id": 1, "decision": "maybe", "reason": "unclear"}],
                             None, phase=1, stage="ta")
        df = pd.read_csv(p)
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "maybe"
        assert r.matched == [1]
        assert r.rejected_ft_maybe == []

    @pytest.mark.parametrize("raw, expected", [
        (61, "61"), (61.0, "61"), ("61", "61"), ("p1_61", "p1_61"), (" 61 ", "61"),
    ])
    def test_id_to_str_survives_csv_round_trip_shapes(self, raw, expected):
        assert id_to_str(raw) == expected


class TestYearDtype:
    """Candidate.year is Optional[int]; a single missing year used to upcast the
    whole column to float64, so every sheet downstream (candidates_dedup.csv,
    screening.csv, extraction.csv) showed "2020.0" instead of "2020"."""

    def test_missing_year_does_not_force_the_column_to_float(self):
        import sys
        sys.path.insert(0, "scripts")
        from search import Candidate
        from dataclasses import asdict

        records = [Candidate(source="x", title="t", authors="a", year=2020,
                              venue="v", doi="d", url="u", abstract="ab"),
                   Candidate(source="x", title="t2", authors="a", year=None,
                              venue="v", doi="d", url="u", abstract="ab")]
        df = pd.DataFrame(asdict(r) for r in records)
        df["year"] = df["year"].astype("Int64")
        assert str(df["year"].dtype) == "Int64"
        csv_text = df[["title", "year"]].to_csv(index=False)
        assert "2020.0" not in csv_text
        assert "t,2020" in csv_text  # bare int, not "t,2020.0"
