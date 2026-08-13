"""
PROVENANCE.md's Configuration section never showed the tool version anywhere
except buried inside the raw Search & processing log table -- and a review
worked on across an upgrade logs a tool_version event every time it's
resumed, so a reader had to find and compare multiple log rows themselves to
work out which version actually produced the file. A one-line summary,
naming the latest version and flagging if it changed mid-review, is what
that table entry always implied but never actually said.
"""
import tempfile

from srp.config import ReviewConfig
from srp.provenance import Provenance
from srp.state import RunState


def render_with_versions(versions: list) -> str:
    with tempfile.TemporaryDirectory() as td:
        cfg = ReviewConfig(topic="t", mailto="a@b.c")
        st = RunState.create(td, "demo", cfg.to_dict())
        prov = Provenance(st.run_dir / "provenance.jsonl")
        for v in versions:
            prov.log("tool_version", version=v)
        out = st.run_dir / "PROVENANCE.md"
        prov.render_markdown(out, config=cfg.to_dict(), prisma=None)
        return out.read_text(encoding="utf-8")


class TestToolVersionInProvenance:
    def test_single_version_shows_a_plain_summary_line(self):
        text = render_with_versions(["1.2.3"])
        assert "**Tool version:** 1.2.3" in text

    def test_no_tool_version_event_omits_the_line_not_crashes(self):
        text = render_with_versions([])
        assert "**Tool version:**" not in text

    def test_version_changed_mid_review_names_the_latest_and_flags_the_change(self):
        text = render_with_versions(["1.2.0", "1.2.3"])
        assert "**Tool version:** 1.2.3" in text
        assert "1.2.0" in text

    def test_resuming_under_the_same_version_twice_does_not_claim_a_change(self):
        text = render_with_versions(["1.2.3", "1.2.3"])
        line = next(l for l in text.splitlines() if l.startswith("- **Tool version:**"))
        assert "1.2.0" not in line
        assert line.count("1.2.3") == 1
