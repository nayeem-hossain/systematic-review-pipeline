"""
assist.py's `parse` command used to validate a chatbot reply's ids against the
WHOLE screening sheet, not the batch `build` actually sent -- so a hallucinated
or echoed id belonging to a different, undecided row elsewhere in the same
sheet passed validation and got written as if the model had genuinely been
asked about it. `build` now writes a `<prompt>.ids.json` sidecar with exactly
the batch's ids, and `parse --prompt <that file>` scopes validation to it.
"""
import json

import pandas as pd

from assist import build_arg_parser, cmd_build, cmd_parse


def _sheet(tmp_path, rows):
    p = tmp_path / "screening.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def _candidates(tmp_path):
    p = tmp_path / "candidates_dedup.csv"
    pd.DataFrame({
        "id": [1, 2], "title": ["A", "B"], "abstract": ["x", "y"],
        "year": [2020, 2021], "venue": ["V", "W"],
    }).to_csv(p, index=False)
    return p


class TestBuildRefusesTheGenericFallbackByDefault:
    """DEFAULT_CRITERIA recreates the exact 'plausibly relevant' judgement a
    systematic review's screening exists to foreclose -- build must refuse to
    write that prompt unless the user explicitly opts in."""

    def test_no_criteria_is_a_hard_error(self, tmp_path, capsys):
        out = tmp_path / "prompt_ta.txt"
        ap = build_arg_parser()
        args = ap.parse_args([
            "build", "--in", str(_candidates(tmp_path)), "--stage", "ta", "--out", str(out),
        ])
        assert cmd_build(args) == 2
        assert not out.exists()
        assert "allow-generic-fallback" in capsys.readouterr().err

    def test_explicit_override_still_builds_the_prompt(self, tmp_path, capsys):
        out = tmp_path / "prompt_ta.txt"
        ap = build_arg_parser()
        args = ap.parse_args([
            "build", "--in", str(_candidates(tmp_path)), "--stage", "ta", "--out", str(out),
            "--allow-generic-fallback",
        ])
        assert cmd_build(args) == 0
        assert out.exists()
        assert "WARNING" in capsys.readouterr().err

    def test_real_criteria_needs_no_override(self, tmp_path):
        out = tmp_path / "prompt_ta.txt"
        ap = build_arg_parser()
        args = ap.parse_args([
            "build", "--in", str(_candidates(tmp_path)), "--stage", "ta", "--out", str(out),
            "--criteria", "must report quantitative results",
        ])
        assert cmd_build(args) == 0
        assert out.exists()


class TestBuildWritesBatchIdsSidecar:
    def test_sidecar_contains_exactly_the_batch_ids(self, tmp_path):
        candidates = tmp_path / "candidates_dedup.csv"
        pd.DataFrame({
            "id": [1, 2, 3, 4], "title": ["A", "B", "C", "D"],
            "abstract": ["x", "y", "z", "w"], "year": [2020] * 4, "venue": ["V"] * 4,
        }).to_csv(candidates, index=False)
        out = tmp_path / "prompt_ta.txt"

        ap = build_arg_parser()
        args = ap.parse_args([
            "build", "--in", str(candidates), "--stage", "ta",
            "--out", str(out), "--batch-size", "2", "--start", "0",
            "--criteria", "must report quantitative results",
        ])
        assert cmd_build(args) == 0

        ids_path = tmp_path / "prompt_ta.txt.ids.json"
        assert ids_path.exists()
        assert json.loads(ids_path.read_text(encoding="utf-8")) == [1, 2]


class TestParseIdScoping:
    def _build_batch(self, tmp_path, batch_ids):
        """Simulate what `build` writes for a batch, without needing real candidates."""
        prompt_path = tmp_path / "prompt_ta.txt"
        prompt_path.write_text("prompt text", encoding="utf-8")
        (tmp_path / "prompt_ta.txt.ids.json").write_text(
            json.dumps(batch_ids), encoding="utf-8")
        return prompt_path

    def test_with_prompt_flag_scopes_to_the_batch_actually_sent(self, tmp_path):
        """id 3 belongs to the sheet but NOT to the batch that was sent (ids 1-2)
        -- with --prompt, a reply that answers for id 3 must be refused."""
        screening = _sheet(tmp_path, {
            "id": [1, 2, 3], "title": ["A", "B", "C"],
            "ta_decision": ["", "", ""], "ta_reason": ["", "", ""],
            "ft_decision": ["", "", ""], "ft_reason": ["", "", ""], "reviewer": ["", "", ""],
        })
        prompt_path = self._build_batch(tmp_path, batch_ids=[1, 2])
        reply = tmp_path / "reply.txt"
        reply.write_text("1 | INCLUDE | on-topic\n3 | INCLUDE | hallucinated echo",
                          encoding="utf-8")

        ap = build_arg_parser()
        args = ap.parse_args([
            "parse", "--response", str(reply), "--into", str(screening),
            "--stage", "ta", "--prompt", str(prompt_path),
        ])
        assert cmd_parse(args) == 0

        df = pd.read_csv(screening)
        assert df.loc[df["id"] == 1, "ta_decision"].iloc[0] == "include"
        assert pd.isna(df.loc[df["id"] == 3, "ta_decision"].iloc[0]), (
            "id 3 was not in the batch actually sent -- must be refused, not applied")

    def test_without_prompt_flag_falls_back_to_whole_sheet_and_warns(self, tmp_path, capsys):
        """Documented, backward-compatible fallback: without --prompt, id 3 is
        accepted because it exists somewhere in the sheet -- but a clear warning
        must explain the weaker guarantee."""
        screening = _sheet(tmp_path, {
            "id": [1, 2, 3], "title": ["A", "B", "C"],
            "ta_decision": ["", "", ""], "ta_reason": ["", "", ""],
            "ft_decision": ["", "", ""], "ft_reason": ["", "", ""], "reviewer": ["", "", ""],
        })
        reply = tmp_path / "reply.txt"
        reply.write_text("3 | INCLUDE | not actually in this batch", encoding="utf-8")

        ap = build_arg_parser()
        args = ap.parse_args([
            "parse", "--response", str(reply), "--into", str(screening), "--stage", "ta",
        ])
        assert cmd_parse(args) == 0

        df = pd.read_csv(screening)
        assert df.loc[df["id"] == 3, "ta_decision"].iloc[0] == "include"
        assert "whole sheet" in capsys.readouterr().err.lower()

    def test_stale_prompt_path_warns_and_falls_back(self, tmp_path):
        """--prompt pointing at a file with no matching .ids.json sidecar (e.g. a
        prompt built by an older assist.py) must warn, not crash or silently
        under-validate without saying so."""
        screening = _sheet(tmp_path, {
            "id": [1], "title": ["A"], "ta_decision": [""], "ta_reason": [""],
            "ft_decision": [""], "ft_reason": [""], "reviewer": [""],
        })
        prompt_path = tmp_path / "prompt_ta.txt"
        prompt_path.write_text("prompt text", encoding="utf-8")  # no .ids.json sidecar
        reply = tmp_path / "reply.txt"
        reply.write_text("1 | INCLUDE | fine", encoding="utf-8")

        ap = build_arg_parser()
        args = ap.parse_args([
            "parse", "--response", str(reply), "--into", str(screening),
            "--stage", "ta", "--prompt", str(prompt_path),
        ])
        assert cmd_parse(args) == 0


class TestBuildReportsRemainingCount:
    """build used to print only 'wrote N record(s)...', with no indication of how
    many undecided rows exist in total or will be left after this batch -- a user
    who just pasted one 20-row batch had no way to tell whether another batch was
    waiting or that was the whole corpus."""

    def test_message_shows_total_undecided_and_remaining_after_batch(self, tmp_path, capsys):
        candidates = tmp_path / "candidates_dedup.csv"
        pd.DataFrame({
            "id": [1, 2, 3, 4], "title": ["A", "B", "C", "D"],
            "abstract": ["x", "y", "z", "w"], "year": [2020] * 4, "venue": ["V"] * 4,
        }).to_csv(candidates, index=False)
        out = tmp_path / "prompt_ta.txt"

        ap = build_arg_parser()
        args = ap.parse_args([
            "build", "--in", str(candidates), "--stage", "ta",
            "--out", str(out), "--batch-size", "2", "--start", "0",
            "--criteria", "must report quantitative results",
        ])
        assert cmd_build(args) == 0
        err = capsys.readouterr().err
        assert "2 of 4 undecided" in err
        assert "2 record(s) will remain" in err

    def test_final_batch_reports_zero_remaining(self, tmp_path, capsys):
        candidates = tmp_path / "candidates_dedup.csv"
        pd.DataFrame({
            "id": [1, 2], "title": ["A", "B"], "abstract": ["x", "y"],
            "year": [2020, 2021], "venue": ["V", "W"],
        }).to_csv(candidates, index=False)
        out = tmp_path / "prompt_ta.txt"

        ap = build_arg_parser()
        args = ap.parse_args([
            "build", "--in", str(candidates), "--stage", "ta",
            "--out", str(out), "--batch-size", "20", "--start", "0",
            "--criteria", "must report quantitative results",
        ])
        assert cmd_build(args) == 0
        err = capsys.readouterr().err
        assert "0 record(s) will remain" in err


class TestParseReportsRemainingCount:
    """parse used to print counts-by-decision for the batch just applied, but never
    said how many rows in the sheet still have no decision -- the same 'is there
    more to do' gap as build."""

    def test_reports_how_many_still_undecided_after_applying(self, tmp_path, capsys):
        screening = _sheet(tmp_path, {
            "id": [1, 2, 3], "title": ["A", "B", "C"],
            "ta_decision": ["", "", ""], "ta_reason": ["", "", ""],
            "ft_decision": ["", "", ""], "ft_reason": ["", "", ""], "reviewer": ["", "", ""],
        })
        reply = tmp_path / "reply.txt"
        reply.write_text("1 | INCLUDE | on-topic", encoding="utf-8")

        ap = build_arg_parser()
        args = ap.parse_args([
            "parse", "--response", str(reply), "--into", str(screening), "--stage", "ta",
        ])
        assert cmd_parse(args) == 0
        out = capsys.readouterr().out
        assert "2 record(s) still undecided" in out

    def test_reports_all_decided_when_nothing_left(self, tmp_path, capsys):
        screening = _sheet(tmp_path, {
            "id": [1], "title": ["A"], "ta_decision": [""], "ta_reason": [""],
            "ft_decision": [""], "ft_reason": [""], "reviewer": [""],
        })
        reply = tmp_path / "reply.txt"
        reply.write_text("1 | INCLUDE | on-topic", encoding="utf-8")

        ap = build_arg_parser()
        args = ap.parse_args([
            "parse", "--response", str(reply), "--into", str(screening), "--stage", "ta",
        ])
        assert cmd_parse(args) == 0
        out = capsys.readouterr().out
        assert "All records now decided" in out
