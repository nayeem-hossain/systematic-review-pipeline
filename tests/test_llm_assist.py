"""
The parser turns a chatbot's free text into recorded screening decisions. It is
the boundary where machine output enters the review's audit trail, so the property
that matters most is NEGATIVE: it must never invent a decision the reply did not
actually contain, and never attach one to a record that was not in the batch.
"""
import pytest

from srp.llm_assist import (build_screening_prompt, parse_screening_response,
                             compose_criteria, DEFAULT_CRITERIA)

RECS = [
    {"id": 61, "title": "A Benchmark Study of X", "year": 2021, "venue": "V",
     "abstract": "benchmark abstract"},
    {"id": 7, "title": "Deep Learning for Y", "year": 2020, "venue": "W",
     "abstract": "dl abstract"},
]


class TestPromptRendering:
    def test_no_enumeration_index(self):
        """Studies used to render as '1. [61] Title' -- two integers per line under
        the instruction 'keep the same id'. A model answering '1 | INCLUDE' meaning
        the first study wrote its decision onto record id 1, a different paper."""
        prompt = build_screening_prompt(RECS, stage="ta", topic="T")
        assert "[61] A Benchmark Study of X" in prompt
        assert "1. [61]" not in prompt
        assert "2. [7]" not in prompt

    def test_criteria_appear_verbatim(self):
        crit = compose_criteria("ta", "must be peer reviewed", "must not be a survey")
        prompt = build_screening_prompt(RECS, stage="ta", topic="T", criteria=crit)
        assert "must be peer reviewed" in prompt
        assert "must not be a survey" in prompt

    def test_falls_back_to_default_criteria_when_none_given(self):
        prompt = build_screening_prompt(RECS, stage="ta", topic="T")
        assert DEFAULT_CRITERIA["ta"] in prompt

    def test_abstract_included_when_present(self):
        assert "benchmark abstract" in build_screening_prompt(RECS, stage="ta")

    def test_missing_abstract_is_marked_not_hidden(self):
        prompt = build_screening_prompt([{"id": 1, "title": "T", "abstract": ""}], stage="ta")
        assert "(not available)" in prompt


class TestNoMaybeAtFullText:
    """ft_decision is the terminal stage (srp/decisions.py's TA_PROCEED_DECISIONS):
    a "maybe" there silently vanishes from the PRISMA counts with no exclusion
    reason recorded (PRISMA item 16b). The prompt must never offer it at stage=ft,
    unlike stage=ta where MAYBE is the correct, deliberate escape hatch."""

    def test_ta_prompt_offers_maybe(self):
        prompt = build_screening_prompt(RECS, stage="ta", topic="T")
        assert "MAYBE" in prompt

    def test_ft_prompt_never_offers_maybe(self):
        prompt = build_screening_prompt(RECS, stage="ft", topic="T")
        assert "MAYBE" not in prompt
        assert "<INCLUDE|EXCLUDE>" in prompt

    def test_ft_default_criteria_does_not_offer_maybe(self):
        assert "MAYBE" not in DEFAULT_CRITERIA["ft"]

    def test_ft_composed_criteria_does_not_offer_maybe(self):
        """The composed ft-stage guidance may still mention "MAYBE" while
        explicitly ruling it out ("no MAYBE at the full-text stage") -- what
        matters is that it never offers MAYBE as a usable decision."""
        crit = compose_criteria("ft", "usable evidence", "no full text")
        assert "no MAYBE" in crit


class TestSilenceOnACriterionMeansMaybe:
    """A criterion the abstract simply never addresses (neither confirmed nor
    contradicted) is missing information, not a failure to meet it -- without
    an explicit rule, a model is prone to read silence as EXCLUDE, producing a
    systematic false-exclusion bias on exactly the borderline papers a
    title/abstract pass exists to flag for full-text review."""

    def test_ta_criteria_distinguish_contradicted_from_unaddressed(self):
        crit = compose_criteria("ta", "reports quantitative results", "not in English")
        assert "not addressed by the abstract" in crit
        assert "insufficient information" in crit
        assert "not a failure to meet it" in crit


class TestReasonMustCiteTheCriterion:
    """A free-form reason ('not relevant') is unauditable in bulk; the README's
    own recommended human step is to spot-check a sample of decisions for a
    systematic false-exclusion pattern, which requires a reason that names
    which criterion actually fired."""

    def test_output_format_requires_a_criterion_named_reason(self):
        prompt = build_screening_prompt(RECS, stage="ta", topic="T")
        assert "reason naming the specific criterion" in prompt

    def test_still_parses_normally_as_free_text_after_the_second_pipe(self):
        r = parse_screening_response(
            "61 | INCLUDE | criterion: reports quantitative results", valid_ids=[61])
        assert r.decisions[0]["reason"] == "criterion: reports quantitative results"


class TestTaStillOffersMaybe:
    def test_ta_composed_criteria_still_offers_maybe(self):
        crit = compose_criteria("ta", "usable evidence", "off topic")
        assert "MAYBE" in crit


class TestComposeCriteria:
    def test_empty_protocol_returns_empty_so_callers_can_warn(self):
        assert compose_criteria("ta", "", "") == ""

    def test_inclusion_only(self):
        out = compose_criteria("ta", "peer reviewed", "")
        assert "INCLUSION CRITERIA" in out and "EXCLUSION CRITERIA" not in out

    def test_both_sections(self):
        out = compose_criteria("ta", "inc text", "exc text")
        assert "INCLUSION CRITERIA" in out and "EXCLUSION CRITERIA" in out

    def test_instructs_against_own_judgement(self):
        out = compose_criteria("ta", "inc", "exc")
        assert "Apply ONLY the criteria below" in out


class TestNamespacedIdsSurviveMarkdownStripping:
    """_ID_RE explicitly supports "p<phase>_<n>" ids (used once phases are
    merged, e.g. AI-assisting full-text screening against included_final.csv),
    but the markdown-noise stripper used to remove EVERY underscore in a line
    -- turning "p1_3" into "p13" -- so every reply against a namespaced batch
    silently failed to parse, with no id in the reply ever matching valid_ids."""

    def test_phase_namespaced_id_parses(self):
        r = parse_screening_response("p1_3 | INCLUDE | on-topic", valid_ids=["p1_3"])
        assert r.decisions == [{"id": "p1_3", "decision": "include", "reason": "on-topic"}]

    def test_snowball_namespaced_id_parses(self):
        r = parse_screening_response("sb_12 | EXCLUDE | off-topic", valid_ids=["sb_12"])
        assert r.decisions == [{"id": "sb_12", "decision": "exclude", "reason": "off-topic"}]

    def test_underscore_italics_around_a_plain_id_still_stripped(self):
        """The fix must not stop stripping genuine underscore-italic emphasis
        around a token boundary, only the underscore embedded inside an id."""
        r = parse_screening_response("_61_ | INCLUDE | good", valid_ids=[61])
        assert r.decisions == [{"id": 61, "decision": "include", "reason": "good"}]


class TestParsingHappyPath:
    def test_pipe_format(self):
        r = parse_screening_response("61 | INCLUDE | relevant\n7 | EXCLUDE | off topic",
                                      valid_ids=[61, 7])
        assert r.decisions == [
            {"id": 61, "decision": "include", "reason": "relevant"},
            {"id": 7, "decision": "exclude", "reason": "off topic"},
        ]

    def test_json_format(self):
        r = parse_screening_response(
            '[{"id": 61, "decision": "include", "reason": "yes"}]', valid_ids=[61, 7])
        assert r.decisions == [{"id": 61, "decision": "include", "reason": "yes"}]

    def test_fenced_json(self):
        r = parse_screening_response(
            '```json\n[{"id": 61, "decision": "exclude", "reason": "no"}]\n```',
            valid_ids=[61])
        assert len(r.decisions) == 1 and r.decisions[0]["decision"] == "exclude"


class TestJsonPathDoesNotSilentlyDropItems:
    """_try_parse_json used to have no equivalent to _parse_lines' unparsed_lines
    -- a non-dict entry, a missing id/decision key, or a decision word
    _normalize_decision doesn't recognize (e.g. "Included" vs "include") vanished
    with zero trace, and the id was then folded into missing_ids alongside
    genuinely-unanswered ids with the misleading message "got no decision back"
    even though the model had, in fact, answered."""

    def test_unrecognized_decision_word_is_reported_not_silently_dropped(self):
        r = parse_screening_response(
            '[{"id": 61, "decision": "include", "reason": "on-topic"}, '
            '{"id": 3, "decision": "Included", "reason": "wrong casing"}]',
            valid_ids=[61, 3])
        assert {d["id"] for d in r.decisions} == {61}
        assert any("3" in u and "Included" in u for u in r.unparsed_lines)
        assert r.missing_ids == [3]

    def test_missing_decision_key_is_reported(self):
        r = parse_screening_response(
            '[{"id": 61, "reason": "no decision key at all"}]', valid_ids=[61])
        assert r.decisions == []
        assert r.unparsed_lines

    def test_non_dict_item_is_reported_alongside_a_valid_one(self):
        """A JSON array with zero valid decisions at all falls back to
        line-based parsing entirely (reported at line granularity instead) --
        this checks the per-item tracking that fires once the array yields at
        least one real decision."""
        r = parse_screening_response(
            '[{"id": 61, "decision": "include", "reason": "a"}, "not a dict"]',
            valid_ids=[61])
        assert {d["id"] for d in r.decisions} == {61}
        assert any("not a dict" in u for u in r.unparsed_lines)

    def test_valid_items_still_parse_normally_alongside_dropped_ones(self):
        r = parse_screening_response(
            '[{"id": 61, "decision": "include", "reason": "a"}, '
            '{"id": 7, "decision": "bogus", "reason": "b"}]',
            valid_ids=[61, 7])
        assert r.decisions == [{"id": 61, "decision": "include", "reason": "a"}]

    def test_yes_no_map_to_include_exclude(self):
        r = parse_screening_response("61 | yes | a\n7 | no | b", valid_ids=[61, 7])
        assert [d["decision"] for d in r.decisions] == ["include", "exclude"]

    @pytest.mark.parametrize("line", [
        "61 | INCLUDE | good",
        "1. 61 | INCLUDE | good",      # markdown list numbering
        "**61** | INCLUDE | good",     # markdown bold
        "[61] | INCLUDE | good",       # bracketed id, as rendered
        "- 61 | INCLUDE | good",       # markdown dash bullet -- the default list
                                       # style most chatbots reach for unprompted
        "* 61 | INCLUDE | good",       # markdown asterisk bullet
        "+ 61 | INCLUDE | good",       # markdown plus bullet
        "- [61] | INCLUDE | good",     # dash bullet AND bracketed id together
        "| 61 | INCLUDE | good |",     # rendered as a markdown table row
    ])
    def test_common_chatbot_decorations(self, line):
        r = parse_screening_response(line, valid_ids=[61])
        assert len(r.decisions) == 1 and r.decisions[0]["id"] == 61

    def test_dash_bulleted_batch_reply(self):
        """A dash-bulleted list is a plausible whole-reply shape even though the
        prompt asks for plain pipe-delimited lines -- every id in the batch must
        still resolve, not just a lone line in isolation."""
        r = parse_screening_response(
            "- 61 | INCLUDE | on-topic\n- 7 | EXCLUDE | survey paper", valid_ids=[61, 7])
        assert {(d["id"], d["decision"]) for d in r.decisions} == {
            (61, "include"), (7, "exclude")}

    def test_markdown_table_with_header_and_separator_rows(self):
        """A full GFM table (header row + '|---|---|---|' separator row + data
        rows) -- the header/separator rows correctly land in unparsed_lines as
        noise, but must not prevent the real decision rows from parsing."""
        r = parse_screening_response(
            "| id | decision | reason |\n|---|---|---|\n"
            "| 61 | INCLUDE | on-topic |\n| 7 | EXCLUDE | survey |",
            valid_ids=[61, 7])
        assert {(d["id"], d["decision"]) for d in r.decisions} == {
            (61, "include"), (7, "exclude")}
        assert len(r.unparsed_lines) == 2

    def test_markdown_header_above_bulleted_decisions(self):
        r = parse_screening_response(
            "### Screening decisions\n- 61 | INCLUDE | on-topic\n- 7 | EXCLUDE | survey",
            valid_ids=[61, 7])
        assert {(d["id"], d["decision"]) for d in r.decisions} == {
            (61, "include"), (7, "exclude")}
        assert r.unparsed_lines == ["### Screening decisions"]

    def test_dash_as_field_separator_is_a_known_unhandled_case(self):
        """If a model abandons the pipe-delimited format entirely and separates
        fields with ' - ' instead of '|', this is NOT recovered -- documented as
        a real, accepted limitation rather than silently expected to work.
        Reported cleanly as unparsed, not corrupted or misattributed."""
        r = parse_screening_response(
            "- [61] INCLUDE - on-topic", valid_ids=[61])
        assert r.decisions == []
        assert r.unparsed_lines


class TestParsingRefusals:
    def test_positional_answers_are_refused(self):
        """The wrong-paper scenario: a model answering by position, not by id."""
        r = parse_screening_response("1 | INCLUDE | first\n2 | EXCLUDE | second",
                                      valid_ids=[61, 7])
        assert r.decisions == []
        assert set(r.unknown_ids) == {1, 2}

    def test_hallucinated_id_is_refused(self):
        r = parse_screening_response("999 | INCLUDE | invented", valid_ids=[61, 7])
        assert r.decisions == []
        assert r.unknown_ids == [999]

    def test_refusal_reply_yields_no_decisions(self):
        r = parse_screening_response("I'm sorry, I can't help with that.", valid_ids=[61])
        assert r.decisions == []

    def test_prose_preamble_does_not_fabricate(self):
        r = parse_screening_response(
            "Sure! Here are my screening results for your review:\n"
            "61 | INCLUDE | relevant\n"
            "Let me know if you'd like more detail.",
            valid_ids=[61, 7])
        assert len(r.decisions) == 1
        assert r.unparsed_lines
        assert r.missing_ids == [7]

    def test_empty_reply(self):
        r = parse_screening_response("", valid_ids=[61])
        assert r.decisions == [] and r.missing_ids == [61]


class TestParsingDiagnostics:
    def test_contradictions_are_reported_not_silently_resolved(self):
        r = parse_screening_response("61 | INCLUDE | a\n61 | EXCLUDE | b", valid_ids=[61])
        assert 61 in r.conflicts
        assert r.conflicts[61] == ["include", "exclude"]
        assert any("contradictory" in p for p in r.problems())

    def test_duplicate_identical_decisions_are_not_conflicts(self):
        r = parse_screening_response("61 | INCLUDE | a\n61 | INCLUDE | a", valid_ids=[61])
        assert not r.conflicts
        assert len(r.decisions) == 1

    def test_missing_ids_reported(self):
        r = parse_screening_response("61 | INCLUDE | a", valid_ids=[61, 7, 9])
        assert set(r.missing_ids) == {7, 9}

    def test_missing_ids_preserve_batch_order_not_scrambled_by_set_iteration(self):
        """problems() truncates missing_ids to the first 5 as a triage sample --
        a set-iteration order shows an arbitrary, non-adjacent subset instead of
        the batch's actual first few undecided records."""
        namespaced = [f"p1_{i}" for i in range(10, 20)]
        r = parse_screening_response("", valid_ids=namespaced)
        assert r.missing_ids == namespaced

    def test_missing_ids_order_survives_partial_answers(self):
        namespaced = [f"p1_{i}" for i in range(1, 8)]
        r = parse_screening_response("p1_3 | INCLUDE | a\np1_6 | EXCLUDE | b",
                                       valid_ids=namespaced)
        assert {d["id"] for d in r.decisions} == {"p1_3", "p1_6"}
        assert r.missing_ids == ["p1_1", "p1_2", "p1_4", "p1_5", "p1_7"]

    def test_clean_reply_has_no_problems(self):
        r = parse_screening_response("61 | INCLUDE | a\n7 | EXCLUDE | b", valid_ids=[61, 7])
        assert r.problems() == []

    def test_result_is_list_like_for_callers(self):
        r = parse_screening_response("61 | INCLUDE | a", valid_ids=[61])
        assert len(r) == 1
        assert [d["id"] for d in r] == [61]
        assert r[0]["id"] == 61

    def test_without_valid_ids_nothing_is_refused(self):
        """Backwards-compatible: omitting valid_ids disables reconciliation."""
        r = parse_screening_response("999 | INCLUDE | x")
        assert len(r.decisions) == 1
        assert r.unknown_ids == []
