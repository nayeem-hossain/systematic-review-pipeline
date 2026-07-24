"""
Orchestration-integrity tests.

These cover findings where the tool reported success it had not earned: a phase
whose search died being marked complete, a fabricated-citation guard that crashed
being reported as a pass, a corrupt file being laundered into an authoritative
"0 included". The common failure mode is silence, so every test here asserts that
something is *refused* or *reported* rather than defaulted.
"""
import io
from pathlib import Path

import pandas as pd
import pytest
from rich.console import Console

import slr
from srp.config import ReviewConfig
from srp.provenance import Provenance
from srp.state import RunState


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
        "_menu_review_self_appraisal",
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
