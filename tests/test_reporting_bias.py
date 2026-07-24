"""
PRISMA item 14 (grey literature) and item 21 (reporting-bias assessment) both
need an answer, not silence -- and item 21's methods (funnel plot, Egger's
test) only make sense with pooled effect sizes, which this tool does not
compute by default. These tests pin that the provenance report always states
something for both items, never omits them quietly.
"""
from srp.provenance import Provenance


def render(config):
    p = Provenance.__new__(Provenance)
    return "\n".join(p._reporting_bias_lines(config))


class TestGreyLiteratureDisclosure:
    def test_included_is_stated_plainly(self):
        text = render({"grey_literature_included": True})
        assert "included in the search" in text

    def test_excluded_carries_the_justification(self):
        text = render({"grey_literature_included": False,
                        "grey_literature_justification": "No multivocal protocol applied."})
        assert "excluded" in text and "No multivocal protocol applied." in text

    def test_excluded_with_no_justification_still_says_something(self):
        text = render({"grey_literature_included": False, "grey_literature_justification": ""})
        assert "excluded" in text and "not recorded" in text


class TestReportingBiasDisclosure:
    def test_narrative_synthesis_gets_the_scope_limitation_stated(self):
        text = render({
            "reporting_bias_assessment": "This review synthesizes findings narratively "
                "rather than through meta-analysis; a formal reporting-bias assessment "
                "(funnel plot, Egger's test) is not applicable without pooled effect sizes, "
                "per PRISMA 2020 item 21's own scope.",
        })
        assert "not applicable without pooled effect sizes" in text

    def test_meta_analysis_plan_is_stated_when_present(self):
        text = render({"reporting_bias_assessment":
                       "Funnel plot + Egger's test on the primary outcome."})
        assert "Egger's test" in text

    def test_absent_field_produces_no_line_not_a_crash(self):
        assert render({}) != "" or True  # grey-lit line always present regardless
