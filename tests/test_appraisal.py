"""
Every domain name in the appraisal registry becomes a literal extraction.csv
column header, so these tests pin structural invariants (uniqueness, every
field profile pointing at a real instrument) rather than re-asserting the
verbatim text itself -- that text's correctness rests on the cited source, not
on a test.
"""
import re
from dataclasses import replace

import pytest

from srp.appraisal import (INSTRUMENTS, FIELD_PROFILES, PRIMARY_STUDY,
                            REVIEW_SELF_CHECK, CERTAINTY, instrument_columns,
                            compose_appraisal_disclosure,
                            render_review_self_appraisal)


class TestRegistryIntegrity:
    def test_every_field_profile_instrument_exists(self):
        missing = []
        for profile in FIELD_PROFILES.values():
            for key in profile.primary_study_instruments:
                if key not in INSTRUMENTS:
                    missing.append(key)
            if profile.certainty_framework and profile.certainty_framework not in INSTRUMENTS:
                missing.append(profile.certainty_framework)
            if profile.review_level_instrument and profile.review_level_instrument not in INSTRUMENTS:
                missing.append(profile.review_level_instrument)
        assert not missing

    def test_every_instrument_has_a_citation_and_url(self):
        for inst in INSTRUMENTS.values():
            assert inst.citation.strip(), inst.key
            assert inst.url.strip(), inst.key

    def test_certainty_instruments_are_tagged_certainty(self):
        for key in ("grade", "grade_cerqual"):
            assert INSTRUMENTS[key].level == CERTAINTY

    def test_review_level_instruments_are_tagged_review_self_check(self):
        for key in ("dare", "amstar2", "robis", "meccir"):
            assert INSTRUMENTS[key].level == REVIEW_SELF_CHECK

    def test_primary_study_instruments_have_domains(self):
        for key, inst in INSTRUMENTS.items():
            if inst.level == PRIMARY_STUDY:
                assert inst.domains, key

    def test_instrument_that_is_not_fully_verbatim_says_so(self):
        """amstar2 and meccir's full item text was not independently verified for
        every item -- the registry must not silently claim completeness it
        doesn't have."""
        assert INSTRUMENTS["amstar2"].verbatim is False
        assert INSTRUMENTS["meccir"].verbatim is False

    def test_a_field_with_no_established_certainty_framework_carries_a_justification(self):
        """software_engineering has certainty_framework == "" -- PRISMA items 15/22
        must get an explicit reason, not silence."""
        se = FIELD_PROFILES["software_engineering"]
        assert se.certainty_framework == ""
        assert se.justification.strip()

    def test_generic_other_is_the_documented_escape_hatch(self):
        assert FIELD_PROFILES["generic_other"].primary_study_instruments == []
        assert FIELD_PROFILES["generic_other"].justification.strip()


class TestInstrumentColumns:
    def test_dyba_dingsoyr_has_11_columns(self):
        cols = instrument_columns("dyba_dingsoyr")
        assert len(cols) == 11
        assert len(set(cols)) == 11  # no collisions

    def test_quadas2_has_7_columns_risk_plus_applicability(self):
        """4 domains, but the first 3 are ALSO rated for applicability -- 7 total,
        not 4. Getting this wrong silently drops 3 required judgements."""
        cols = instrument_columns("quadas2")
        assert len(cols) == 7

    def test_columns_are_namespaced_per_instrument(self):
        """Two instruments must never collide on a column header if both are
        selected for the same review."""
        a = set(instrument_columns("rob2"))
        b = set(instrument_columns("robins_i"))
        assert not (a & b)

    def test_columns_are_valid_csv_headers(self):
        for key in INSTRUMENTS:
            if INSTRUMENTS[key].level != PRIMARY_STUDY:
                continue
            for col in instrument_columns(key):
                assert "," not in col and "\n" not in col
                assert col == col.strip()


class TestComposeDisclosure:
    def test_names_the_chosen_instrument(self):
        text = compose_appraisal_disclosure("health_rct", ["rob2"], "grade", "amstar2")
        assert "RoB 2" in text or "Cochrane RoB" in text
        assert "GRADE" in text
        assert "AMSTAR" in text

    def test_falls_back_to_field_justification_when_no_certainty_chosen(self):
        text = compose_appraisal_disclosure("software_engineering", ["dyba_dingsoyr"], "", "")
        assert "Santos" in text or "GRADE" in text  # the SE justification cites this

    def test_unrecognized_field_does_not_crash(self):
        text = compose_appraisal_disclosure("not_a_real_field", [], "", "")
        assert "no field-appropriate" in text or "No research field" in text

    def test_empty_primary_list_is_stated_not_silent(self):
        text = compose_appraisal_disclosure("generic_other", [], "", "")
        assert "No primary-study appraisal instrument" in text


class TestProvenanceAppraisalSection:
    """PRISMA item 11 (risk-of-bias assessment) and items 15/22 (certainty of
    evidence) each need an answer in the provenance report, not silence."""

    def _lines(self, config):
        from srp.provenance import Provenance
        p = Provenance.__new__(Provenance)
        return p._appraisal_lines(config)

    def test_no_field_recorded_says_so(self):
        text = "\n".join(self._lines({}))
        assert "not recorded" in text

    def test_cites_the_chosen_primary_instrument(self):
        text = "\n".join(self._lines({
            "research_field": "health_rct",
            "primary_study_instruments": ["rob2"],
        }))
        assert "RoB 2" in text
        assert "training.cochrane.org" in text

    def test_certainty_framework_cited_when_present(self):
        text = "\n".join(self._lines({
            "primary_study_instruments": ["rob2"],
            "certainty_framework": "grade",
        }))
        assert "GRADE" in text

    def test_justification_used_when_no_certainty_framework(self):
        text = "\n".join(self._lines({
            "primary_study_instruments": ["dyba_dingsoyr"],
            "certainty_framework": "",
            "appraisal_justification": "SE has no established certainty framework.",
        }))
        assert "SE has no established certainty framework." in text

    def test_review_level_instrument_cited_when_present(self):
        text = "\n".join(self._lines({
            "primary_study_instruments": ["rob2"],
            "review_level_instrument": "amstar2",
        }))
        assert "AMSTAR" in text

    def test_unknown_instrument_key_does_not_crash(self):
        """Defensive: a hand-edited config.json with a stale/typo'd key must not
        break provenance rendering."""
        text = "\n".join(self._lines({
            "primary_study_instruments": ["not_a_real_key"],
        }))
        assert isinstance(text, str)


class TestRenderReviewSelfAppraisal:
    """The review-level counterpart to instrument_columns(): AMSTAR 2/ROBIS/DARE/
    MECCIR appraise the review itself, once, not any per-study row, so they get
    their own fillable checklist file rather than an extraction.csv column."""

    def test_every_domain_appears_as_a_row(self):
        text = render_review_self_appraisal("amstar2")
        for domain in INSTRUMENTS["amstar2"].domains:
            assert domain.replace("|", "\\|") in text

    def test_includes_citation_and_source(self):
        text = render_review_self_appraisal("robis")
        assert INSTRUMENTS["robis"].citation in text
        assert INSTRUMENTS["robis"].url in text

    def test_rating_and_justification_columns_are_blank(self):
        text = render_review_self_appraisal("dare")
        n_domains = len(INSTRUMENTS["dare"].domains)
        blank_rows = [ln for ln in text.splitlines() if ln.startswith("| ") and ln.endswith("| | |")]
        assert len(blank_rows) == n_domains

    def test_non_verbatim_instrument_carries_its_caveat(self):
        text = render_review_self_appraisal("amstar2")
        assert INSTRUMENTS["amstar2"].notes in text

    def test_rejects_a_primary_study_instrument(self):
        """dyba_dingsoyr appraises individual studies, not the review -- calling
        this on it would silently produce a checklist for the wrong object."""
        with pytest.raises(ValueError):
            render_review_self_appraisal("dyba_dingsoyr")

    def test_rejects_a_certainty_instrument(self):
        with pytest.raises(ValueError):
            render_review_self_appraisal("grade")

    def test_pipe_in_domain_text_is_escaped_not_left_as_a_raw_separator(self):
        """No real instrument's domains happen to contain a '|' today, so this
        injects one via a temporary instrument -- a raw (unescaped) pipe would
        be read by a markdown renderer as an extra column separator, silently
        misaligning the row. Counting raw '|' characters (as an earlier version
        of this test did) can't tell escaped-and-present apart from
        unescaped-and-broken, since escaping inserts a backslash rather than
        removing the character -- so this checks the escape itself is there,
        and that only *unescaped* pipes are counted as separators."""
        original = INSTRUMENTS["meccir"]
        patched_domains = list(original.domains)
        patched_domains[0] = "Scope of review | still one cell"
        INSTRUMENTS["meccir"] = replace(original, domains=patched_domains)
        try:
            text = render_review_self_appraisal("meccir")
        finally:
            INSTRUMENTS["meccir"] = original

        assert "Scope of review \\| still one cell" in text
        table_lines = [ln for ln in text.splitlines() if ln.startswith("| ")]
        unescaped_pipe = re.compile(r"(?<!\\)\|")
        header_cols = len(unescaped_pipe.findall(table_lines[0]))
        for ln in table_lines[2:]:
            assert len(unescaped_pipe.findall(ln)) == header_cols
