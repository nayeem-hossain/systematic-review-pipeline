"""
Orchestration-integrity tests.

These cover findings where the tool reported success it had not earned: a phase
whose search died being marked complete, a fabricated-citation guard that crashed
being reported as a pass, a corrupt file being laundered into an authoritative
"0 included". The common failure mode is silence, so every test here asserts that
something is *refused* or *reported* rather than defaulted.
"""
import io
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from rich.console import Console

import slr
from srp.config import ReviewConfig
from srp.env import parse_env_text
from srp.provenance import Provenance
from srp.state import RunState


class TestRunSubprocessCwd:
    """run_subprocess used to force cwd=_SCRIPT_DIR (wherever slr.py itself
    is installed) on every child process it launches -- for a pipx install
    that's deep inside site-packages, nowhere near the user's actual runs/
    folder. Every --out/--in path a stage script receives is relative, so
    the child resolved them against the WRONG directory: search.py/dedup.py/
    screen.py wrote real output inside the installed package instead of the
    run folder, while state.json/config.json (written in-process, never via
    a subprocess) correctly used the real cwd. The parent's own post-hoc
    re-read of candidates.csv (used to compute n_hits for state.json) then
    ALSO looked in the wrong place and found nothing -- recording a false
    zero even when the search itself had actually succeeded. This is almost
    certainly what earlier 'phase folder is empty but state.json says done'
    reports actually were, not a network issue."""

    def test_child_process_is_not_pinned_to_the_install_directory(self, monkeypatch):
        captured = {}

        def fake_run(cmd, capture_output=True, text=True, cwd=None, env=None):
            captured["cwd"] = cwd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(slr.subprocess, "run", fake_run)
        console = Console(file=io.StringIO())

        slr.run_subprocess(["python", "-c", "pass"], console, "test")

        assert captured["cwd"] is None


class TestReadCsvSafe:
    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert slr._read_csv_safe(tmp_path / "nope.csv").empty

    def test_zero_byte_file_is_empty(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("", encoding="utf-8")
        assert slr._read_csv_safe(p).empty

    def test_header_only_file_is_empty(self, tmp_path):
        p = tmp_path / "hdr.csv"
        p.write_text("id,title\n", encoding="utf-8")
        assert slr._read_csv_safe(p).empty

    def test_corrupt_file_raises_rather_than_returning_empty(self, tmp_path):
        """An unreadable file used to be indistinguishable from an absent one, so
        run_review_gate marked the gate done and logged n_included=0 -- recording
        'this phase included zero studies' when the truth was 'unreadable'."""
        p = tmp_path / "bad.csv"
        p.write_text('id,title\n1,"unterminated\n2,ok,extra,cols\n', encoding="utf-8")
        with pytest.raises(slr.CorruptCsvError):
            slr._read_csv_safe(p)

    def test_undecodable_file_raises(self, tmp_path):
        p = tmp_path / "bin.csv"
        p.write_bytes(b"id,title\n1,\xff\xfe\x00bad\n")
        with pytest.raises(slr.CorruptCsvError):
            slr._read_csv_safe(p)


class TestUndecidedDetection:
    def _sheet(self, tmp_path, decisions):
        p = tmp_path / "screening.csv"
        pd.DataFrame({"id": range(1, len(decisions) + 1),
                       "title": [f"P{i}" for i in range(len(decisions))],
                       "ta_decision": decisions}).to_csv(p, index=False)
        return p

    def test_finds_blank_decisions(self, tmp_path):
        p = self._sheet(tmp_path, ["include", "", "exclude", ""])
        assert slr._undecided_ta_ids(p) == {"2", "4"}

    def test_none_left_when_all_decided(self, tmp_path):
        p = self._sheet(tmp_path, ["include", "exclude"])
        assert slr._undecided_ta_ids(p) == set()

    def test_missing_file_is_not_a_crash(self, tmp_path):
        assert slr._undecided_ta_ids(tmp_path / "nope.csv") == set()


class TestRequiredPhaseStages:
    def test_ai_assist_is_not_required(self):
        """Screening entirely by hand is a legitimate -- indeed stronger -- choice,
        so a phase must be able to complete without any AI assist."""
        assert "assist_ta" not in slr._REQUIRED_PHASE_STAGES

    def test_corpus_and_gate_stages_are_required(self):
        for stage in ("search", "dedup", "prescreen", "review_gate"):
            assert stage in slr._REQUIRED_PHASE_STAGES

    def test_incomplete_phase_is_detected(self, tmp_path):
        """The resume pointer used to advance unconditionally, so a phase whose
        search failed became permanently unreachable and the review silently
        shipped missing that phase's corpus."""
        cfg = ReviewConfig(topic="t", mailto="a@b.c", n_phases=2)
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        st.mark_stage(1, "search")
        st.mark_stage(1, "dedup")
        # prescreen and review_gate never completed
        missing = [s for s in slr._REQUIRED_PHASE_STAGES
                   if st.stage_status(1, s) != "done"]
        assert missing == ["prescreen", "review_gate"]

    def test_complete_phase_has_no_missing_stages(self, tmp_path):
        cfg = ReviewConfig(topic="t", mailto="a@b.c", n_phases=1)
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        for stage in slr._REQUIRED_PHASE_STAGES:
            st.mark_stage(1, stage)
        assert [s for s in slr._REQUIRED_PHASE_STAGES
                if st.stage_status(1, s) != "done"] == []


class TestProvenanceCoverage:
    """Every consolidation action must leave a trace, or PROVENANCE.md cannot show
    whether citation verification ever ran, or what it found."""

    @pytest.mark.parametrize("func", [
        "_menu_merge", "_menu_download", "_menu_full_text_screening",
        "_menu_verify_citations", "_menu_extract", "_menu_figures",
        "_menu_export_refs", "_menu_export_exclusions", "_menu_kappa",
        "_menu_review_self_appraisal", "_menu_review_extraction",
        "_menu_check_for_updates", "_menu_diagnose_run", "_menu_manage_api_keys",
        "_menu_rerun_search",
    ])
    def test_action_takes_provenance_and_logs(self, func):
        import inspect
        fn = getattr(slr, func)
        assert "prov" in inspect.signature(fn).parameters, f"{func} cannot log"
        assert "prov.log" in inspect.getsource(fn), f"{func} never logs"


class TestMenuReviewSelfAppraisal:
    """_menu_review_self_appraisal is the CLI-facing wiring around
    srp.appraisal.render_review_self_appraisal() (unit-tested on its own in
    tests/test_appraisal.py) -- nothing previously exercised the menu function
    itself: the happy path where the wizard already recorded a valid
    review_level_instrument, the interactive-picker fallback when it didn't,
    and that fallback's own "Skip" branch."""

    def _state_and_prov(self, tmp_path, cfg):
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        return st, prov

    def _console(self):
        return Console(file=io.StringIO())

    def test_writes_checklist_when_a_valid_key_is_configured(self, tmp_path):
        cfg = ReviewConfig(topic="t", mailto="a@b.c", review_level_instrument="amstar2")
        st, prov = self._state_and_prov(tmp_path, cfg)
        slr._menu_review_self_appraisal(st, cfg, prov, self._console())
        out = st.run_dir / "review_self_appraisal_amstar2.md"
        assert out.exists()
        assert "AMSTAR 2" in out.read_text(encoding="utf-8")

    def test_logs_the_chosen_instrument_to_provenance(self, tmp_path):
        cfg = ReviewConfig(topic="t", mailto="a@b.c", review_level_instrument="robis")
        st, prov = self._state_and_prov(tmp_path, cfg)
        slr._menu_review_self_appraisal(st, cfg, prov, self._console())
        events = [e for e in prov.events() if e["event"] == "review_self_appraisal_drafted"]
        assert events and events[0]["instrument"] == "robis"

    def test_falls_back_to_a_picker_when_no_valid_key_is_configured(self, tmp_path, monkeypatch):
        """An empty review_level_instrument (the dataclass default, or a run
        started before this field existed) must not silently do nothing --
        the user picks an instrument on the spot instead."""
        cfg = ReviewConfig(topic="t", mailto="a@b.c", review_level_instrument="")
        st, prov = self._state_and_prov(tmp_path, cfg)
        from srp.appraisal import INSTRUMENTS
        dare_name = INSTRUMENTS["dare"].name

        class _FakeSelect:
            def __init__(self, *a, **kw):
                pass

            def ask(self):
                return dare_name

        monkeypatch.setattr(slr.questionary, "select", lambda *a, **kw: _FakeSelect())
        slr._menu_review_self_appraisal(st, cfg, prov, self._console())
        assert (st.run_dir / "review_self_appraisal_dare.md").exists()

    def test_stale_key_not_in_the_registry_also_falls_back_to_the_picker(self, tmp_path, monkeypatch):
        cfg = ReviewConfig(topic="t", mailto="a@b.c", review_level_instrument="not_a_real_key")
        st, prov = self._state_and_prov(tmp_path, cfg)
        from srp.appraisal import INSTRUMENTS
        amstar_name = INSTRUMENTS["amstar2"].name

        monkeypatch.setattr(
            slr.questionary, "select",
            lambda *a, **kw: type("F", (), {"ask": lambda self: amstar_name})())
        slr._menu_review_self_appraisal(st, cfg, prov, self._console())
        assert (st.run_dir / "review_self_appraisal_amstar2.md").exists()

    def test_skip_writes_no_file_and_logs_nothing(self, tmp_path, monkeypatch):
        cfg = ReviewConfig(topic="t", mailto="a@b.c", review_level_instrument="")
        st, prov = self._state_and_prov(tmp_path, cfg)
        monkeypatch.setattr(
            slr.questionary, "select",
            lambda *a, **kw: type("F", (), {"ask": lambda self: "Skip"})())
        slr._menu_review_self_appraisal(st, cfg, prov, self._console())
        assert not list(st.run_dir.glob("review_self_appraisal_*.md"))
        assert not [e for e in prov.events() if e["event"] == "review_self_appraisal_drafted"]

    def test_cancelling_the_picker_behaves_like_skip(self, tmp_path, monkeypatch):
        """questionary returns None on Ctrl-C -- must not crash or write a file
        for a None instrument key."""
        cfg = ReviewConfig(topic="t", mailto="a@b.c", review_level_instrument="")
        st, prov = self._state_and_prov(tmp_path, cfg)
        monkeypatch.setattr(
            slr.questionary, "select",
            lambda *a, **kw: type("F", (), {"ask": lambda self: None})())
        slr._menu_review_self_appraisal(st, cfg, prov, self._console())
        assert not list(st.run_dir.glob("review_self_appraisal_*.md"))


class TestAiAssistReplyFile:
    """The reply-file prompt now pre-creates an empty file so a user only ever
    pastes into an existing file rather than having to create one themselves at
    the exact right path -- and submitting that file before actually pasting
    into it must be caught, not silently parsed as zero decisions."""

    def _setup(self, tmp_path):
        cfg = ReviewConfig(topic="ML-IDS", mailto="a@b.c",
                            inclusion_criteria="peer-reviewed", exclusion_criteria="survey")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        pdir = tmp_path / "phase_1"
        pdir.mkdir()
        pd.DataFrame({
            "id": [61], "title": ["A Benchmark Study"], "abstract": ["abc"],
            "year": [2021], "venue": ["V"],
        }).to_csv(pdir / "candidates_dedup.csv", index=False)
        return cfg, st, prov, pdir

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def test_reply_file_is_pre_created_before_the_path_prompt(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        seen_path = {}

        def fake_confirm(prompt, default=False):
            if "still empty" in prompt or "Screen another batch" in prompt:
                return self._fake(False)
            return self._fake(default)

        def fake_select(prompt, choices):
            return self._fake(choices[0])

        def fake_text(prompt, default=""):
            if "Path to the chatbot reply file" in prompt:
                seen_path["path"] = default
                assert Path(default).exists(), "reply file must already exist by prompt time"
            return self._fake(default)

        monkeypatch.setattr(slr.questionary, "confirm", fake_confirm)
        monkeypatch.setattr(slr.questionary, "select", fake_select)
        monkeypatch.setattr(slr.questionary, "text", fake_text)

        slr.run_ai_assist_loop(st, cfg, prov, 1, pdir, console)
        assert seen_path.get("path")

    def test_still_empty_reply_file_prompts_retry_not_silent_zero_decisions(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        confirms_seen = []

        def fake_confirm(prompt, default=False):
            confirms_seen.append(prompt)
            if "still empty" in prompt or "Screen another batch" in prompt:
                return self._fake(False)
            return self._fake(default)

        def fake_select(prompt, choices):
            return self._fake(choices[0])

        def fake_text(prompt, default=""):
            return self._fake(default)  # accept the pre-created (still-empty) default path

        monkeypatch.setattr(slr.questionary, "confirm", fake_confirm)
        monkeypatch.setattr(slr.questionary, "select", fake_select)
        monkeypatch.setattr(slr.questionary, "text", fake_text)

        slr.run_ai_assist_loop(st, cfg, prov, 1, pdir, console)

        assert any("still empty" in p for p in confirms_seen)
        screening_path = pdir / "screening.csv"
        if screening_path.exists():
            screening = pd.read_csv(screening_path)
            if "ta_decision" in screening.columns:
                decided = screening["ta_decision"].notna() & (screening["ta_decision"] != "")
                assert not decided.any(), "no decision should have been recorded for id 61"


class TestAiAssistRefusesGenericFallback:
    """DEFAULT_CRITERIA reproduces the exact 'plausibly relevant' judgement a
    systematic review's screening exists to foreclose -- AI-assist must refuse
    to build a prompt on it without an explicit, once-per-phase override."""

    def _setup(self, tmp_path):
        cfg = ReviewConfig(topic="ML-IDS", mailto="a@b.c",
                            inclusion_criteria="", exclusion_criteria="")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        pdir = tmp_path / "phase_1"
        pdir.mkdir()
        pd.DataFrame({
            "id": [61], "title": ["A Benchmark Study"], "abstract": ["abc"],
            "year": [2021], "venue": ["V"],
        }).to_csv(pdir / "candidates_dedup.csv", index=False)
        return cfg, st, prov, pdir

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def test_declining_the_override_never_writes_a_prompt(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(False))

        slr.run_ai_assist_loop(st, cfg, prov, 1, pdir, console)

        assert not list(pdir.glob("prompt_ta_*.txt"))

    def test_cancelling_the_override_prompt_also_refuses(self, tmp_path, monkeypatch):
        """Ctrl-C (questionary returns None) must not be treated as consent."""
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(None))

        slr.run_ai_assist_loop(st, cfg, prov, 1, pdir, console)

        assert not list(pdir.glob("prompt_ta_*.txt"))

    def test_explicit_override_lets_it_proceed(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())

        def fake_confirm(prompt, default=False):
            if "generic fallback" in prompt:
                return self._fake(True)
            return self._fake(False)  # decline every prompt after that to end quickly

        def fake_select(prompt, choices):
            return self._fake("Stop AI-assist screening for this phase")

        monkeypatch.setattr(slr.questionary, "confirm", fake_confirm)
        monkeypatch.setattr(slr.questionary, "select", fake_select)

        slr.run_ai_assist_loop(st, cfg, prov, 1, pdir, console)

        assert list(pdir.glob("prompt_ta_*.txt")), "override was granted -- a prompt should exist"


class TestAiAssistLoopReportsProgress:
    """run_ai_assist_loop used to show only 'batch of N', with no indication of
    how many undecided records exist in total or will remain after this batch --
    a user who just finished pasting one 20-row batch had no way to tell whether
    another batch was waiting or that was the whole corpus."""

    def _setup(self, tmp_path, n=3):
        cfg = ReviewConfig(topic="ML-IDS", mailto="a@b.c",
                            inclusion_criteria="peer-reviewed", exclusion_criteria="survey")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        pdir = tmp_path / "phase_1"
        pdir.mkdir()
        ids = list(range(1, n + 1))
        pd.DataFrame({
            "id": ids, "title": [f"Study {i}" for i in ids], "abstract": ["abc"] * n,
            "year": [2021] * n, "venue": ["V"] * n, "doi": [""] * n,
        }).to_csv(pdir / "candidates_dedup.csv", index=False)
        pd.DataFrame({
            "id": ids, "title": [f"Study {i}" for i in ids], "doi": [""] * n,
            "ta_decision": [""] * n, "ta_reason": [""] * n,
            "ft_decision": [""] * n, "ft_reason": [""] * n, "reviewer": [""] * n,
        }).to_csv(pdir / "screening.csv", index=False)
        return cfg, st, prov, pdir

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def test_batch_panel_shows_total_undecided_and_remaining(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path, n=3)
        monkeypatch.setattr(slr, "_BATCH_SIZE", 2)
        console = Console(file=io.StringIO())

        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(False))
        monkeypatch.setattr(
            slr.questionary, "select",
            lambda *a, **kw: self._fake("Stop AI-assist screening for this phase"))

        slr.run_ai_assist_loop(st, cfg, prov, 1, pdir, console)

        text = console.file.getvalue()
        assert "3" in text and "undecided" in text
        assert "1" in text  # 1 record(s) will remain undecided after this batch of 2

    def test_after_parsing_a_batch_reports_updated_remaining_count(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path, n=3)
        monkeypatch.setattr(slr, "_BATCH_SIZE", 2)
        console = Console(file=io.StringIO())

        def fake_confirm(prompt, default=False):
            if "Screen another batch" in prompt:
                return self._fake(False)  # stop after the first batch
            return self._fake(default)

        def fake_select(prompt, choices):
            return self._fake(choices[0])  # "I've pasted it and saved the reply..."

        def fake_text(prompt, default=""):
            if "Path to the chatbot reply file" in prompt:
                Path(default).write_text(
                    "1 | INCLUDE | on-topic\n2 | EXCLUDE | off-topic", encoding="utf-8")
            return self._fake(default)

        monkeypatch.setattr(slr.questionary, "confirm", fake_confirm)
        monkeypatch.setattr(slr.questionary, "select", fake_select)
        monkeypatch.setattr(slr.questionary, "text", fake_text)

        slr.run_ai_assist_loop(st, cfg, prov, 1, pdir, console)

        df = pd.read_csv(pdir / "screening.csv")
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "include"
        text = console.file.getvalue()
        assert "1 record(s) still undecided" in text


class TestReviewExtractionMenu:
    """_menu_review_extraction is the guided, one-record-at-a-time editor for
    extraction.csv -- R/A/T/C and venue_tier are edited through a select menu
    so an invalid value cannot be typed, free-text fields are edited with the
    current value as the default so pressing Enter never blanks them, every
    change is logged to provenance (there was previously no audit trail for
    hand-edited extraction data), and quality_tier auto-recomputes from
    R/A/T/C via compute_quality_tier() unless manually overridden, which is
    logged as a distinct event rather than silently diverging."""

    def _setup(self, tmp_path, quality_tier=""):
        cfg = ReviewConfig(topic="t", mailto="a@b.c", reviewer="AB")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        pd.DataFrame({
            "id": [1], "title": ["Study One"], "authors": [""], "year": [2021],
            "venue": ["V"], "doi": [""],
            "thematic_class": [""], "study_type": [""], "contribution": [""],
            "key_findings": [""], "rq_mapping": [""], "limitations": [""],
            "venue_tier": [""], "R": [""], "A": [""], "T": [""], "C": [""],
            "quality_tier": [quality_tier],
            "extraction_reviewer": [""], "extraction_date": [""], "notes": [""],
        }).to_csv(st.run_dir / "extraction.csv", index=False)
        return cfg, st, prov

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def _sequence(self, answers):
        it = iter(answers)
        return lambda *a, **kw: self._fake(next(it))

    def test_missing_extraction_csv_prints_guidance(self, tmp_path):
        cfg = ReviewConfig(topic="t", mailto="a@b.c")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        console = Console(file=io.StringIO())

        slr._menu_review_extraction(st, cfg, prov, console)

        assert "extraction" in console.file.getvalue().lower()

    def test_unknown_study_id_reports_error_and_reprompts(self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._sequence(["999", ""]))

        slr._menu_review_extraction(st, cfg, prov, console)

        assert "no study" in console.file.getvalue().lower()

    def test_editing_a_constrained_field_writes_value_and_logs(self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._sequence(["1", ""]))
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence(["venue_tier", "T1", "Done with this study"]))

        slr._menu_review_extraction(st, cfg, prov, console)

        df = pd.read_csv(st.run_dir / "extraction.csv")
        assert df.loc[0, "venue_tier"] == "T1"
        events = [e for e in prov.events() if e["event"] == "extraction_field_edited"]
        assert events and events[0]["field"] == "venue_tier" and events[0]["new_value"] == "T1"

    def test_free_text_field_edit_is_written_and_logged(self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._sequence(
            ["1", "A convolutional IDS approach.", ""]))
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence(["contribution", "Done with this study"]))

        slr._menu_review_extraction(st, cfg, prov, console)

        df = pd.read_csv(st.run_dir / "extraction.csv")
        assert df.loc[0, "contribution"] == "A convolutional IDS approach."
        events = [e for e in prov.events() if e["event"] == "extraction_field_edited"]
        assert events and events[0]["field"] == "contribution"

    def test_pressing_enter_on_free_text_leaves_it_unchanged_and_does_not_log(
            self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        study_id_calls = {"n": 0}

        def fake_text(prompt, default=""):
            if "Study id" in prompt:
                study_id_calls["n"] += 1
                return self._fake("1" if study_id_calls["n"] == 1 else "")
            return self._fake(default)  # Enter -> keep current value

        monkeypatch.setattr(slr.questionary, "text", fake_text)
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence(["contribution", "Done with this study"]))

        slr._menu_review_extraction(st, cfg, prov, console)

        df = pd.read_csv(st.run_dir / "extraction.csv")
        assert df.loc[0, "contribution"] == "" or pd.isna(df.loc[0, "contribution"])
        assert not [e for e in prov.events() if e["event"] == "extraction_field_edited"]

    def test_completing_ratc_autocomputes_quality_tier(self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._sequence(["1", ""]))
        monkeypatch.setattr(slr.questionary, "select", self._sequence([
            "R", "L", "A", "L", "T", "L", "C", "L", "Done with this study",
        ]))

        slr._menu_review_extraction(st, cfg, prov, console)

        df = pd.read_csv(st.run_dir / "extraction.csv")
        assert df.loc[0, "quality_tier"] == "A"
        events = [e for e in prov.events() if e["event"] == "quality_tier_recomputed"]
        assert events and events[-1]["quality_tier"] == "A"

    def test_manual_quality_tier_override_is_logged_distinctly(self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._sequence(["1", ""]))
        monkeypatch.setattr(slr.questionary, "select", self._sequence([
            "R", "L", "A", "L", "T", "L", "C", "L",   # computed -> A
            "quality_tier", "B",                       # manual override -> B
            "Done with this study",
        ]))

        slr._menu_review_extraction(st, cfg, prov, console)

        df = pd.read_csv(st.run_dir / "extraction.csv")
        assert df.loc[0, "quality_tier"] == "B"
        events = [e for e in prov.events() if e["event"] == "quality_tier_manual_override"]
        assert events and events[0]["computed"] == "A" and events[0]["override"] == "B"

    def test_reviewer_and_date_stamped_on_edit(self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._sequence(["1", ""]))
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence(["venue_tier", "T2", "Done with this study"]))

        slr._menu_review_extraction(st, cfg, prov, console)

        df = pd.read_csv(st.run_dir / "extraction.csv")
        assert df.loc[0, "extraction_reviewer"] == "AB"
        assert str(df.loc[0, "extraction_date"]).strip() != ""


class TestReviewGatePreviewBeforeFlip:
    """A mistyped id landing on a DIFFERENT valid id already in the same list
    used to be applied silently -- only an aggregate count was shown afterward.
    The gate now previews exactly which id/title will flip which way and asks
    for confirmation before writing anything."""

    def _setup(self, tmp_path):
        cfg = ReviewConfig(topic="t", mailto="a@b.c")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        pdir = tmp_path / "phase_1"
        pdir.mkdir()
        pd.DataFrame({
            "id": [1, 2, 3], "title": ["A", "B", "C"], "doi": ["", "", ""],
            "year": [2020, 2021, 2022], "venue": ["V", "V", "V"],
            "ta_decision": ["include", "include", "exclude"],
            "ta_reason": ["", "", ""], "reviewer": ["ai-assisted"] * 3,
        }).to_csv(pdir / "screening.csv", index=False)
        return cfg, st, prov, pdir

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def _fake_text_sequence(self, answers):
        it = iter(answers)
        return lambda prompt, default="": self._fake(next(it))

    def test_declining_the_preview_writes_nothing(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["1", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(False))

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        df = pd.read_csv(pdir / "screening.csv")
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "include"

    def test_confirming_the_preview_writes_the_flip(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["1", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(True))

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        df = pd.read_csv(pdir / "screening.csv")
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "exclude"

    def test_cancelling_the_preview_is_the_same_as_declining(self, tmp_path, monkeypatch):
        """Ctrl-C (questionary returns None) must not be treated as consent."""
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["1", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(None))

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        df = pd.read_csv(pdir / "screening.csv")
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "include"

    def test_no_flips_requested_needs_no_confirm(self, tmp_path, monkeypatch):
        """Leaving both id-flip prompts blank must not trigger the new confirm
        at all -- there is nothing to preview or apply."""
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        confirm_calls = []

        def fake_confirm(prompt, default=False):
            confirm_calls.append(prompt)
            return self._fake(True)

        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", fake_confirm)

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        assert not confirm_calls

    def test_unknown_ids_still_reported_regardless_of_confirm_answer(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["999", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(True))

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        assert "999" in console.file.getvalue()


class TestReviewGateBlocksOnUndecidedRows:
    """run_review_gate used to call state.mark_stage() unconditionally, so a
    phase could be marked 'done' -- and the pipeline's resume pointer could
    advance past it -- while most of its screening.csv was still blank. Only
    the AI-assist loop had this guard (it refuses to mark assist_ta done while
    undecided rows remain); the review gate itself had none. This mirrors that
    same guard: no override, matching assist_ta's own pattern exactly."""

    def _setup(self, tmp_path, decisions):
        cfg = ReviewConfig(topic="t", mailto="a@b.c")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        pdir = tmp_path / "phase_1"
        pdir.mkdir()
        pd.DataFrame({
            "id": range(1, len(decisions) + 1),
            "title": [f"P{i}" for i in range(len(decisions))],
            "doi": [""] * len(decisions),
            "year": [2020] * len(decisions), "venue": ["V"] * len(decisions),
            "ta_decision": decisions,
            "ta_reason": [""] * len(decisions), "reviewer": ["ai-assisted"] * len(decisions),
        }).to_csv(pdir / "screening.csv", index=False)
        return cfg, st, prov, pdir

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def _fake_text_sequence(self, answers):
        it = iter(answers)
        return lambda prompt, default="": self._fake(next(it))

    def test_undecided_rows_leave_the_stage_unmarked(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path, ["include", "exclude", ""])
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(True))

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        assert st.stage_status(1, "review_gate") != "done"

    def test_undecided_rows_show_a_warning(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path, ["include", "exclude", "", ""])
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(True))

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        text = console.file.getvalue().lower()
        assert "2" in text and "not marked complete" in text

    def test_undecided_rows_log_a_provenance_event(self, tmp_path, monkeypatch):
        cfg, st, prov, pdir = self._setup(tmp_path, ["include", ""])
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(True))

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        events = [e for e in prov.events() if e["event"] == "review_gate_incomplete"]
        assert events and events[0]["n_undecided"] == 1

    def test_fully_decided_sheet_still_marks_the_stage_done(self, tmp_path, monkeypatch):
        """Regression guard: the new check must not accidentally block the
        already-working, fully-decided case."""
        cfg, st, prov, pdir = self._setup(tmp_path, ["include", "exclude", "include"])
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "text", self._fake_text_sequence(["", ""]))
        monkeypatch.setattr(slr.questionary, "confirm", lambda *a, **kw: self._fake(True))

        slr.run_review_gate(st, cfg, prov, 1, pdir, console)

        assert st.stage_status(1, "review_gate") == "done"


class TestFullTextScreeningBlocksOnUndecidedRows:
    """Same gap, second instance: _menu_full_text_screening marked itself done
    unconditionally too."""

    def _setup(self, tmp_path, ft_decisions):
        cfg = ReviewConfig(topic="t", mailto="a@b.c", n_phases=1)
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        n = len(ft_decisions)
        pd.DataFrame({
            "id": range(1, n + 1), "title": [f"P{i}" for i in range(n)],
            "doi": [""] * n, "authors": [""] * n, "year": [2020] * n, "venue": ["V"] * n,
            "url": [""] * n, "ta_decision": ["include"] * n, "ta_reason": [""] * n,
            "ft_decision": ft_decisions, "ft_reason": [""] * n,
        }).to_csv(st.run_dir / "included_final.csv", index=False)
        return cfg, st, prov

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def test_undecided_rows_leave_the_stage_unmarked(self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path, ["include", "exclude", ""])
        console = Console(file=io.StringIO())
        monkeypatch.setattr(
            slr.questionary, "select",
            lambda *a, **kw: self._fake("stop for now"))

        slr._menu_full_text_screening(st, cfg, prov, console)

        assert st.stage_status(1, "full_text_screening") != "done"

    def test_undecided_rows_log_a_provenance_event(self, tmp_path, monkeypatch):
        cfg, st, prov = self._setup(tmp_path, ["include", ""])
        console = Console(file=io.StringIO())
        monkeypatch.setattr(
            slr.questionary, "select",
            lambda *a, **kw: self._fake("stop for now"))

        slr._menu_full_text_screening(st, cfg, prov, console)

        events = [e for e in prov.events() if e["event"] == "full_text_screening_incomplete"]
        assert events and events[0]["n_undecided"] == 1

    def test_fully_decided_sheet_marks_the_stage_done(self, tmp_path):
        cfg, st, prov = self._setup(tmp_path, ["include", "exclude"])
        console = Console(file=io.StringIO())

        slr._menu_full_text_screening(st, cfg, prov, console)

        assert st.stage_status(1, "full_text_screening") == "done"


class TestMainHandlesCorruptCsv:
    """_read_csv_safe() raises CorruptCsvError for a genuinely unreadable CSV
    (as opposed to missing/empty/header-only, which are legitimate and return
    an empty frame) -- but nothing previously caught it anywhere in slr.py, so
    it propagated as an unhandled traceback instead of a clean message. main()
    is the single top-level entry point every guided-TUI code path runs
    through, so it's the one place this needs to be caught."""

    def test_corrupt_csv_error_is_caught_with_a_clean_message_not_a_traceback(
            self, tmp_path, monkeypatch, capsys):
        def boom(runs_dir, console, preselect_run=None):
            raise slr.CorruptCsvError("runs/x/phase_1/screening.csv could not be parsed: boom")

        monkeypatch.setattr(slr, "load_dotenv", lambda *a, **kw: [])
        monkeypatch.setattr(slr, "main_interactive", boom)
        monkeypatch.setattr(sys, "argv", ["slr.py", "--runs-dir", str(tmp_path)])

        code = slr.main()

        assert code == 1
        out = capsys.readouterr().out
        assert "corrupt" in out.lower()
        assert "screening.csv" in out

    def test_exit_code_is_distinct_from_keyboard_interrupt(self, tmp_path, monkeypatch):
        def boom(runs_dir, console, preselect_run=None):
            raise slr.CorruptCsvError("bad file")

        monkeypatch.setattr(slr, "load_dotenv", lambda *a, **kw: [])
        monkeypatch.setattr(slr, "main_interactive", boom)
        monkeypatch.setattr(sys, "argv", ["slr.py", "--runs-dir", str(tmp_path)])
        assert slr.main() != 130  # 130 is KeyboardInterrupt's code, not this error's


class TestAskSecretPromptRendering:
    """questionary renders a multi-line message's cursor immediately after the
    last character, not on a fresh line -- for _ask_secret's two-line
    '[detected in .env]' message this put the input line in a visibly wrong
    spot. A trailing newline in the message forces the input onto its own
    line (this is now more likely to be hit at all, now that load_dotenv()
    actually runs -- see TestMainLoadsDotenv)."""

    def test_detected_key_message_ends_with_a_newline(self, monkeypatch):
        captured = {}

        class _FakeAnswer:
            def ask(self):
                return "reused-value"

        def fake_text(message, default=""):
            captured["message"] = message
            return _FakeAnswer()

        monkeypatch.setattr(slr.questionary, "text", fake_text)
        monkeypatch.setenv("S2_API_KEY", "already-set")

        slr._ask_secret("Semantic Scholar API key", "S2_API_KEY")

        assert captured["message"].endswith("\n")
        assert "[detected in .env" in captured["message"]

    def test_undetected_key_message_is_a_single_line(self, monkeypatch):
        captured = {}

        class _FakeAnswer:
            def ask(self):
                return ""

        def fake_text(message, default=""):
            captured["message"] = message
            return _FakeAnswer()

        monkeypatch.setattr(slr.questionary, "text", fake_text)
        monkeypatch.delenv("S2_API_KEY", raising=False)

        slr._ask_secret("Semantic Scholar API key", "S2_API_KEY")

        assert "\n" not in captured["message"]


class TestMainLoadsDotenv:
    """main() must call load_dotenv() itself -- it's the single top-level entry
    point for both a git-clone `python slr.py` and a pipx-installed `slr`, and
    nothing else in the guided TUI ever did, so a .env silently did nothing
    regardless of install method."""

    def test_main_calls_load_dotenv(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(slr, "load_dotenv", lambda *a, **kw: calls.append(True))
        monkeypatch.setattr(slr, "main_interactive", lambda runs_dir, console, preselect_run=None: None)
        monkeypatch.setattr(sys, "argv", ["slr.py", "--runs-dir", str(tmp_path)])

        slr.main()

        assert calls == [True]


class TestStartupBanner:
    def test_banner_always_shows_the_running_version(self, tmp_path, monkeypatch):
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "1.2.3")
        monkeypatch.setattr(slr, "latest_release_version", lambda: "v1.2.3")
        monkeypatch.setattr(slr.RunState, "list_runs", staticmethod(lambda runs_dir: []))
        monkeypatch.setattr(slr, "new_review_wizard", lambda console, runs_dir: None)

        slr.main_interactive(tmp_path, console)

        assert "1.2.3" in console.file.getvalue()


class TestUpdateNotice:
    """The startup line must never block or spam, and it must never leave the
    user unable to tell whether the check even ran -- it always shows the
    running version, plus a dim confirmation when up to date or a yellow nag
    only when a real newer release exists."""

    def test_shows_version_and_confirms_up_to_date(self, monkeypatch):
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "1.0.0")
        monkeypatch.setattr(slr, "latest_release_version", lambda: "v1.0.0")
        slr._maybe_print_update_notice(console)
        text = console.file.getvalue()
        assert "1.0.0" in text
        assert "up to date" in text.lower()

    def test_prints_notice_when_a_newer_release_exists(self, monkeypatch):
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "1.0.0")
        monkeypatch.setattr(slr, "latest_release_version", lambda: "v9.9.9")
        slr._maybe_print_update_notice(console)
        text = console.file.getvalue()
        assert "9.9.9" in text
        assert "pipx upgrade" in text

    def test_still_shows_version_when_the_check_itself_fails(self, monkeypatch):
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "1.0.0")
        monkeypatch.setattr(slr, "latest_release_version", lambda: None)
        slr._maybe_print_update_notice(console)
        text = console.file.getvalue()
        assert "1.0.0" in text
        assert "up to date" not in text.lower()

    def test_dev_build_is_not_falsely_claimed_up_to_date(self, monkeypatch):
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "0.0.0-dev")
        monkeypatch.setattr(slr, "latest_release_version", lambda: "v1.0.0")
        slr._maybe_print_update_notice(console)
        text = console.file.getvalue().lower()
        assert "source" in text
        assert "up to date" not in text


class TestMenuCheckForUpdates:
    def _state_cfg_prov(self, tmp_path):
        cfg = ReviewConfig(topic="t", mailto="a@b.c")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        return st, cfg, prov

    def test_up_to_date(self, tmp_path, monkeypatch):
        st, cfg, prov = self._state_cfg_prov(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "1.0.0")
        monkeypatch.setattr(slr, "latest_release_version", lambda: "v1.0.0")

        slr._menu_check_for_updates(st, cfg, prov, console)

        assert "up to date" in console.file.getvalue().lower()
        events = [e for e in prov.events() if e["event"] == "update_check"]
        assert events and events[0]["outcome"] == "up_to_date"

    def test_outdated(self, tmp_path, monkeypatch):
        st, cfg, prov = self._state_cfg_prov(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "1.0.0")
        monkeypatch.setattr(slr, "latest_release_version", lambda: "v9.9.9")

        slr._menu_check_for_updates(st, cfg, prov, console)

        text = console.file.getvalue()
        assert "9.9.9" in text and "pipx upgrade" in text
        events = [e for e in prov.events() if e["event"] == "update_check"]
        assert events and events[0]["outcome"] == "outdated"

    def test_check_failed(self, tmp_path, monkeypatch):
        st, cfg, prov = self._state_cfg_prov(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "1.0.0")
        monkeypatch.setattr(slr, "latest_release_version", lambda: None)

        slr._menu_check_for_updates(st, cfg, prov, console)

        assert "could not check" in console.file.getvalue().lower()
        events = [e for e in prov.events() if e["event"] == "update_check"]
        assert events and events[0]["outcome"] == "check_failed"

    def test_dev_build_is_not_falsely_flagged(self, tmp_path, monkeypatch):
        """A source checkout can be ahead of the last tag, not behind it --
        must not claim 'outdated' just because it isn't a real release."""
        st, cfg, prov = self._state_cfg_prov(tmp_path)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "_VERSION", "0.0.0-dev")
        monkeypatch.setattr(slr, "latest_release_version", lambda: "v1.0.0")

        slr._menu_check_for_updates(st, cfg, prov, console)

        text = console.file.getvalue().lower()
        assert "outdated" not in text
        assert "source" in text
        events = [e for e in prov.events() if e["event"] == "update_check"]
        assert events and events[0]["outcome"] == "dev_build"


class TestRerunSearch:
    """The diagnose-run action can tell a user a phase's search came back
    empty -- this is how they actually fix it: re-run that phase's search
    (and its downstream dedup/prescreen, which would otherwise go stale
    against a fresh candidates.csv) either as-is, or after correcting the
    settings that likely caused it (a typo'd mailto, wrong year range, no
    sources selected), without abandoning the run."""

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def _sequence(self, answers):
        it = iter(answers)
        return lambda *a, **kw: self._fake(next(it))

    def _cfg(self):
        return ReviewConfig(topic="t", mailto="a@b.c", keyword_blocks=[["x"]])

    def _state_and_prov(self, tmp_path, cfg):
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        return st, prov

    def _console(self):
        return Console(file=io.StringIO(), width=200)

    def _fake_run_subprocess(self, monkeypatch, n_hits=3, n_dupes=0):
        """Every stage's subprocess call succeeds and writes a plausible
        output CSV, so downstream _read_csv_safe() counts come out
        deterministic instead of depending on a real search/dedup/screen run."""
        def fake(cmd, console, description, success_codes=(0,), secret_env=None):
            script = Path(cmd[1]).name
            out_path = Path(cmd[cmd.index("--out") + 1])
            if script == "search.py":
                rows = "\n".join(f"{i},Title {i}" for i in range(n_hits))
                out_path.write_text(f"id,title\n{rows}\n", encoding="utf-8")
            elif script == "dedup.py":
                lines = ["id,title,duplicate_of"]
                for i in range(n_hits):
                    dup = "1" if i < n_dupes else ""
                    lines.append(f"{i},Title {i},{dup}")
                out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            elif script == "screen.py":
                n_rows = n_hits - n_dupes
                lines = ["id,title,ta_decision"] + [f"{i},Title {i}," for i in range(n_rows)]
                out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

        monkeypatch.setattr(slr, "run_subprocess", fake)

    def _fake_assist_and_gate(self, monkeypatch, n_included=1):
        """run_ai_assist_loop/run_review_gate are exercised by their own
        dedicated test classes -- here they're stood in for so these tests
        stay focused on whether _menu_rerun_search correctly hands off into
        them, not on re-testing their own internal decision trees (which,
        unmocked, would need real eligibility criteria and a batch-reply
        file to get through)."""
        calls = []

        def fake_assist(state, cfg, prov, phase, pdir, console):
            calls.append(("assist_ta", phase))

        def fake_gate(state, cfg, prov, phase, pdir, console):
            calls.append(("review_gate", phase))
            state.mark_stage(phase, "review_gate", counts={"n_included": n_included, "n_overridden": 0})

        monkeypatch.setattr(slr, "run_ai_assist_loop", fake_assist)
        monkeypatch.setattr(slr, "run_review_gate", fake_gate)
        return calls

    def test_no_search_has_ever_run_reports_guidance(self, tmp_path):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = self._console()

        slr._menu_rerun_search(st, cfg, prov, console)

        assert "no phase" in console.file.getvalue().lower()

    def test_declining_the_confirm_changes_nothing(self, tmp_path, monkeypatch):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        st.phase_dir(1)
        st.mark_stage(1, "search", counts={"n_hits": 0})
        console = self._console()
        monkeypatch.setattr(slr.questionary, "select", self._sequence([1]))
        monkeypatch.setattr(slr.questionary, "confirm", self._sequence([False]))
        called = []
        monkeypatch.setattr(slr, "run_subprocess", lambda *a, **kw: called.append(True))

        slr._menu_rerun_search(st, cfg, prov, console)

        assert called == []
        events = [e for e in prov.events() if e["event"] == "rerun_search"]
        assert events and events[0]["confirmed"] is False

    def test_rerun_with_no_changes_reruns_search_dedup_prescreen(self, tmp_path, monkeypatch):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        st.phase_dir(1)
        st.mark_stage(1, "search", counts={"n_hits": 0})
        console = self._console()
        self._fake_run_subprocess(monkeypatch, n_hits=5, n_dupes=1)
        calls = self._fake_assist_and_gate(monkeypatch)
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence([1, "Re-run with current settings (no changes)"]))
        monkeypatch.setattr(slr.questionary, "confirm", self._sequence([True]))

        slr._menu_rerun_search(st, cfg, prov, console)

        st2 = RunState.load(st.run_dir)
        assert st2.stage_status(1, "search") == "done"
        assert st2.state["stages"]["1:search"]["counts"]["n_hits"] == 5
        assert st2.stage_status(1, "dedup") == "done"
        assert st2.stage_status(1, "prescreen") == "done"
        events = [e for e in prov.events() if e["event"] == "rerun_search"]
        assert events and events[0]["outcome"] == "done"
        assert ("assist_ta", 1) in calls
        assert ("review_gate", 1) in calls
        assert st2.stage_status(1, "review_gate") == "done"

    def test_existing_screening_decisions_trigger_a_loss_warning(self, tmp_path, monkeypatch):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        pdir = st.phase_dir(1)
        st.mark_stage(1, "search", counts={"n_hits": 3})
        pd.DataFrame({"id": [1, 2], "title": ["A", "B"], "ta_decision": ["include", ""]}
                     ).to_csv(pdir / "screening.csv", index=False)
        console = self._console()
        monkeypatch.setattr(slr.questionary, "select", self._sequence([1]))
        monkeypatch.setattr(slr.questionary, "confirm", self._sequence([False]))

        slr._menu_rerun_search(st, cfg, prov, console)

        assert "lost" in console.file.getvalue().lower()

    def test_edit_settings_then_rerun_persists_the_change(self, tmp_path, monkeypatch):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        st.phase_dir(1)
        st.mark_stage(1, "search", counts={"n_hits": 0})
        console = self._console()
        self._fake_run_subprocess(monkeypatch, n_hits=2, n_dupes=0)
        self._fake_assist_and_gate(monkeypatch)
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence([1, "Edit settings, then re-run"]))
        monkeypatch.setattr(slr.questionary, "confirm", self._sequence([True]))
        monkeypatch.setattr(slr.questionary, "checkbox", self._sequence([["mailto"]]))
        monkeypatch.setattr(slr.questionary, "text", self._sequence(["fixed@b.c"]))

        slr._menu_rerun_search(st, cfg, prov, console)

        assert cfg.mailto == "fixed@b.c"
        st2 = RunState.load(st.run_dir)
        assert st2.config["mailto"] == "fixed@b.c"
        events = [e for e in prov.events() if e["event"] == "rerun_search"]
        assert events and events[0]["changed_fields"] == ["mailto"]

    def test_stale_review_gate_counts_are_replaced_not_left_stale(self, tmp_path, monkeypatch):
        """review_gate was marked done against the OLD candidates -- after
        search is redone, the rerun continues all the way through a fresh
        review gate (like a normal phase run does), so the stale count gets
        replaced by a real one instead of just being cleared and abandoned."""
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        st.phase_dir(1)
        st.mark_stage(1, "search", counts={"n_hits": 3})
        st.mark_stage(1, "assist_ta", counts={})
        st.mark_stage(1, "review_gate", counts={"n_included": 2, "n_overridden": 0})
        console = self._console()
        self._fake_run_subprocess(monkeypatch, n_hits=1, n_dupes=0)
        calls = self._fake_assist_and_gate(monkeypatch, n_included=999)
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence([1, "Re-run with current settings (no changes)"]))
        monkeypatch.setattr(slr.questionary, "confirm", self._sequence([True]))

        slr._menu_rerun_search(st, cfg, prov, console)

        assert ("assist_ta", 1) in calls
        assert ("review_gate", 1) in calls
        st2 = RunState.load(st.run_dir)
        assert st2.state["stages"]["1:review_gate"]["counts"]["n_included"] == 999


class TestDiagnoseRun:
    """The user's real report: a test run returned 0 hits from every source,
    and phase_1/phase_2 folders ended up empty, with no trail explaining
    either -- state.json and provenance.jsonl both claimed success. This menu
    action gives that trail: a per-phase funnel table, the first stage where
    a count drops to zero, and any stage marked done whose output file isn't
    actually on disk."""

    def _cfg(self):
        return ReviewConfig(topic="t", mailto="a@b.c")

    def _state_and_prov(self, tmp_path, cfg):
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        return st, prov

    def _console(self):
        return Console(file=io.StringIO())

    def test_no_stages_run_yet_is_reported_plainly(self, tmp_path):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = self._console()

        slr._menu_diagnose_run(st, cfg, prov, console)

        assert "no stages" in console.file.getvalue().lower()

    def test_healthy_funnel_has_no_zero_drop_or_missing_file_flags(self, tmp_path):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = self._console()
        pdir = st.phase_dir(1)
        (pdir / "candidates.csv").write_text("id\n1\n2\n", encoding="utf-8")
        (pdir / "candidates_dedup.csv").write_text("id\n1\n2\n", encoding="utf-8")
        (pdir / "screening.csv").write_text("id\n1\n2\n", encoding="utf-8")
        st.mark_stage(1, "search", counts={"n_hits": 2})
        st.mark_stage(1, "dedup", counts={"n_in": 2, "n_out": 2, "n_dupes": 0})
        st.mark_stage(1, "prescreen", counts={"n_rows": 2})
        st.mark_stage(1, "review_gate", counts={"n_included": 1, "n_overridden": 0})

        slr._menu_diagnose_run(st, cfg, prov, console)

        text = console.file.getvalue().lower()
        assert "zero-drop" not in text
        assert "missing" not in text

    def test_zero_hits_at_search_is_flagged_as_the_first_zero_drop_stage(self, tmp_path):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = self._console()
        pdir = st.phase_dir(1)
        (pdir / "candidates.csv").write_text("id\n", encoding="utf-8")
        st.mark_stage(1, "search", counts={"n_hits": 0})

        slr._menu_diagnose_run(st, cfg, prov, console)

        assert "zero-drop" in console.file.getvalue().lower()

    def test_stage_marked_done_with_a_missing_output_file_is_flagged(self, tmp_path):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = self._console()
        st.phase_dir(1)  # creates the phase dir, but never writes candidates.csv
        st.mark_stage(1, "search", counts={"n_hits": 5})

        slr._menu_diagnose_run(st, cfg, prov, console)

        text = console.file.getvalue().lower()
        assert "does not exist" in text or "missing" in text

    def test_logs_a_diagnose_event_to_provenance(self, tmp_path):
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = self._console()
        st.mark_stage(1, "search", counts={"n_hits": 1})

        slr._menu_diagnose_run(st, cfg, prov, console)

        events = [e for e in prov.events() if e["event"] == "diagnose_run"]
        assert events


class TestRunStateSaveConfig:
    """config.json was previously written once at create() and never again --
    _menu_rerun_search's 'edit settings' path needs a way to persist a
    change so it survives a resume, not just live in the in-memory cfg for
    the rest of this process."""

    def test_save_config_updates_the_file_on_disk(self, tmp_path):
        cfg = ReviewConfig(topic="t", mailto="old@b.c")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())

        cfg.mailto = "new@b.c"
        st.save_config(cfg.to_dict())

        reloaded = RunState.load(st.run_dir)
        assert reloaded.config["mailto"] == "new@b.c"


class TestConsolidationMenuLegend:
    """The consolidation menu used to be 18 bare labels with no indication of
    what each one does or produces -- this legend prints a one-line
    description for every action before the menu prompt, so nothing in the
    real actions dict can silently drift out of sync with its description."""

    def test_every_real_action_has_a_description_no_placeholder_shown(self, tmp_path, monkeypatch):
        cfg = ReviewConfig(topic="t", mailto="a@b.c")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        console = Console(file=io.StringIO(), width=200)
        monkeypatch.setattr(slr.questionary, "select",
                             lambda *a, **kw: type("F", (), {"ask": lambda self: "Finish"})())

        slr.consolidation_menu(st, cfg, prov, console)

        text = console.file.getvalue()
        assert "[no description]" not in text
        assert "Diagnose this run" in text
        assert "Re-run a phase's search" in text
        assert "Manage API keys" in text


class TestManageApiKeys:
    """The user's real question, after a pipx install: 'where is the .env
    file, and how do I add/remove keys from it?' -- this menu action shows
    the .env path in use (resolved from cwd, matching load_dotenv()), and
    lets the user add/update or delete keys without hand-editing the file or
    knowing where pipx put anything."""

    def _fake(self, answer):
        return type("F", (), {"ask": lambda self: answer})()

    def _sequence(self, answers):
        it = iter(answers)
        return lambda *a, **kw: self._fake(next(it))

    def _cfg(self):
        return ReviewConfig(topic="t", mailto="a@b.c")

    def _state_and_prov(self, tmp_path, cfg):
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        return st, prov

    def test_shows_the_env_path_in_use_and_key_status(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("S2_API_KEY=already-set\n", encoding="utf-8")
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = Console(file=io.StringIO(), width=200)  # avoid wrapping the long tmp_path
        monkeypatch.setattr(slr.questionary, "select", self._sequence(["Back"]))

        slr._menu_manage_api_keys(st, cfg, prov, console)

        text = console.file.getvalue()
        assert str(tmp_path / ".env") in text
        assert "S2_API_KEY" in text

    def test_add_or_update_writes_to_env_and_current_process(self, tmp_path, monkeypatch):
        # _menu_manage_api_keys mutates os.environ directly (not through
        # monkeypatch), so a monkeypatch.delenv() called AFTER that raw
        # mutation would capture the raw value as what to restore at
        # teardown -- which sets it right back instead of clearing it. A
        # plain try/finally sidesteps that trap.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("S2_API_KEY", raising=False)
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence(["Add or update a key", "S2_API_KEY"]))
        monkeypatch.setattr(slr.questionary, "text", self._sequence(["new-value"]))

        try:
            slr._menu_manage_api_keys(st, cfg, prov, console)

            assert parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))["S2_API_KEY"] == "new-value"
            assert os.environ["S2_API_KEY"] == "new-value"
            events = [e for e in prov.events() if e["event"] == "manage_api_keys"]
            assert events and events[0]["action"] == "set"
        finally:
            os.environ.pop("S2_API_KEY", None)

    def test_delete_a_key_removes_it_from_env_and_current_process(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("S2_API_KEY=secret\n", encoding="utf-8")
        monkeypatch.setenv("S2_API_KEY", "secret")
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "select",
                             self._sequence(["Delete a key", "S2_API_KEY"]))
        monkeypatch.setattr(slr.questionary, "confirm", self._sequence([True]))

        slr._menu_manage_api_keys(st, cfg, prov, console)

        assert "S2_API_KEY" not in parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))
        assert "S2_API_KEY" not in os.environ
        events = [e for e in prov.events() if e["event"] == "manage_api_keys"]
        assert events and events[0]["action"] == "delete"

    def test_delete_all_keys_removes_every_managed_key(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "S2_API_KEY=a\nCORE_API_KEY=b\nMAILTO=keep@me.com\n", encoding="utf-8")
        monkeypatch.setenv("S2_API_KEY", "a")
        monkeypatch.setenv("CORE_API_KEY", "b")
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "select", self._sequence(["Delete ALL keys"]))
        monkeypatch.setattr(slr.questionary, "confirm", self._sequence([True]))

        slr._menu_manage_api_keys(st, cfg, prov, console)

        remaining = parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))
        assert "S2_API_KEY" not in remaining
        assert "CORE_API_KEY" not in remaining
        assert remaining.get("MAILTO") == "keep@me.com"
        assert "S2_API_KEY" not in os.environ
        assert "CORE_API_KEY" not in os.environ
        events = [e for e in prov.events() if e["event"] == "manage_api_keys"]
        assert events and events[0]["action"] == "delete_all"

    def test_declining_the_delete_all_confirmation_changes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("S2_API_KEY=a\n", encoding="utf-8")
        monkeypatch.setenv("S2_API_KEY", "a")
        cfg = self._cfg()
        st, prov = self._state_and_prov(tmp_path, cfg)
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr.questionary, "select", self._sequence(["Delete ALL keys"]))
        monkeypatch.setattr(slr.questionary, "confirm", self._sequence([False]))

        slr._menu_manage_api_keys(st, cfg, prov, console)

        assert parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))["S2_API_KEY"] == "a"
        monkeypatch.delenv("S2_API_KEY", raising=False)


class TestToolVersionStampedToProvenance:
    def test_main_interactive_logs_tool_version_for_a_new_review(self, tmp_path, monkeypatch):
        console = Console(file=io.StringIO())
        monkeypatch.setattr(slr, "latest_release_version", lambda: None)

        cfg = ReviewConfig(topic="t", mailto="a@b.c")
        st = RunState.create(tmp_path, "run1", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")

        monkeypatch.setattr(slr, "new_review_wizard", lambda console, runs_dir: (st, cfg, prov))
        monkeypatch.setattr(slr, "run_phase_loop", lambda *a, **kw: None)
        monkeypatch.setattr(slr, "consolidation_menu", lambda *a, **kw: None)
        monkeypatch.setattr(slr.RunState, "list_runs", staticmethod(lambda runs_dir: []))

        slr.main_interactive(tmp_path, console)

        events = [e for e in prov.events() if e["event"] == "tool_version"]
        assert events and events[0]["version"] == slr._VERSION
