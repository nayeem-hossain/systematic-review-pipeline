"""
PROVENANCE.md's Configuration section must show whichever keyword form a review
actually used. The wizard builds keyword_blocks and deliberately leaves the
legacy flat `keywords` field empty; render_markdown used to read only
`keywords`, silently dropping the "Keywords:" line for every guided-wizard
review (the recommended, default path).
"""
import tempfile

from srp.config import ReviewConfig
from srp.provenance import Provenance
from srp.state import RunState


def render_config_section(config: dict) -> str:
    with tempfile.TemporaryDirectory() as td:
        st = RunState.create(td, "demo", config)
        prov = Provenance(st.run_dir / "provenance.jsonl")
        out = st.run_dir / "PROVENANCE.md"
        prov.render_markdown(out, config=config, prisma=None)
        return out.read_text(encoding="utf-8")


class TestKeywordBlocksInProvenance:
    def test_keyword_blocks_review_shows_keywords_line(self):
        cfg = ReviewConfig(topic="t", mailto="a@b.c",
                            keyword_blocks=[["intrusion detection", "IDS"],
                                            ["machine learning", "deep learning"]])
        text = render_config_section(cfg.to_dict())
        assert "**Keywords:**" in text
        assert "intrusion detection" in text and "machine learning" in text

    def test_flat_keywords_review_still_works(self):
        cfg = ReviewConfig(topic="t", mailto="a@b.c", keywords=["a", "b"])
        text = render_config_section(cfg.to_dict())
        assert "**Keywords:** a, b" in text

    def test_neither_form_set_omits_the_line_not_crashes(self):
        cfg = ReviewConfig(topic="t", mailto="a@b.c")
        text = render_config_section(cfg.to_dict())
        assert "**Keywords:**" not in text
