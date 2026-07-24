"""
The methods-report paragraph is what a user pastes into their manuscript. Every
sentence must be conditioned on what the pipeline actually recorded -- a
truncation caveat must appear only when a source was actually truncated, a
skipped-source note must name only sources actually skipped, and the numbers
must trace back to the same PRISMA counts the figure and PROVENANCE.md use.
"""
from srp.methods_report import (PhaseSearchRecord, SourceStrategyRow,
                                 render_search_methods, render_search_strategy_table)

CFG = {"topic": "ML-IDS", "keywords": ["intrusion detection", "machine learning"],
       "year_from": 2020, "year_to": 2026}

PRISMA = {"identified": 90, "duplicates_removed": 10, "screened": 80,
          "excluded_ta": 50, "assessed_ft": 30, "excluded_ft": 5,
          "included": 25, "undecided_ta": 0,
          "ft_reasons": {"no empirical evaluation": 3, "wrong population": 2}}


def make_phase(phase, rows):
    return PhaseSearchRecord(phase=phase, label=f"Phase {phase}", rows=rows)


class TestRenderSearchMethods:
    def test_names_only_sources_actually_attempted(self):
        rows = [SourceStrategyRow(source="openalex", query_sent="q", retrieved_at="t",
                                    n_retrieved=40, total_available=40, status="ok")]
        para = render_search_methods(CFG, [make_phase(1, rows)], PRISMA)
        assert "openalex" in para

    def test_truncation_caveat_only_when_truncated(self):
        rows = [SourceStrategyRow(source="crossref", query_sent="q", retrieved_at="t",
                                    n_retrieved=40, total_available=40, truncated=False)]
        para = render_search_methods(CFG, [make_phase(1, rows)], PRISMA)
        assert "sample rather than" not in para

    def test_truncation_caveat_present_when_truncated(self):
        rows = [SourceStrategyRow(source="crossref", query_sent="q", retrieved_at="t",
                                    n_retrieved=3, total_available=4297586, truncated=True)]
        para = render_search_methods(CFG, [make_phase(1, rows)], PRISMA)
        assert "crossref" in para
        assert "sample rather than" in para

    def test_skipped_source_named_not_silently_omitted(self):
        rows = [SourceStrategyRow(source="ieee", status="skipped_no_key")]
        para = render_search_methods(CFG, [make_phase(1, rows)], PRISMA)
        assert "ieee" in para and "not queried" in para

    def test_single_phase_has_no_expansion_sentence(self):
        rows = [SourceStrategyRow(source="openalex", n_retrieved=1, total_available=1)]
        para = render_search_methods(CFG, [make_phase(1, rows)], PRISMA)
        assert "query expansion" not in para

    def test_multi_phase_mentions_expansion_not_snowballing(self):
        rows1 = [SourceStrategyRow(source="openalex", n_retrieved=1, total_available=1)]
        rows2 = [SourceStrategyRow(source="openalex", n_retrieved=1, total_available=1)]
        para = render_search_methods(
            CFG, [make_phase(1, rows1), make_phase(2, rows2)], PRISMA)
        assert "2 phases" in para
        assert "query expansion" in para
        assert "citation snowballing" in para  # explicitly disclaimed, not silently implied

    def test_prisma_numbers_appear_verbatim(self):
        rows = [SourceStrategyRow(source="openalex", n_retrieved=1, total_available=1)]
        para = render_search_methods(CFG, [make_phase(1, rows)], PRISMA)
        assert "90" in para and "25" in para and "80" in para

    def test_no_search_strategy_log_is_reported_not_silent(self):
        para = render_search_methods(CFG, [], PRISMA)
        assert "could not be drafted" in para

    def test_undecided_ta_surfaces_a_caveat(self):
        rows = [SourceStrategyRow(source="openalex", n_retrieved=1, total_available=1)]
        prisma = {**PRISMA, "undecided_ta": 12}
        para = render_search_methods(CFG, [make_phase(1, rows)], prisma)
        assert "12" in para and "awaiting" in para

    def test_keyword_blocks_config_is_not_reported_as_no_keywords(self):
        """Regression: the wizard builds keyword_blocks and deliberately leaves
        the legacy flat `keywords` field empty. The base-query sentence used to
        read only `keywords`, so every guided-wizard review (the recommended,
        default path) got a drafted paragraph literally saying 'using the base
        query (no keywords recorded)' -- wrong, and exactly the sentence that
        gets pasted into a manuscript without being rechecked."""
        cfg = {"topic": "ML-IDS", "keywords": [],
               "keyword_blocks": [["intrusion detection", "IDS"],
                                   ["machine learning", "deep learning"]],
               "year_from": 2020, "year_to": 2026}
        rows = [SourceStrategyRow(source="openalex", n_retrieved=1, total_available=1)]
        para = render_search_methods(cfg, [make_phase(1, rows)], PRISMA)
        assert "no keywords recorded" not in para
        assert "intrusion detection" in para and "machine learning" in para
        assert "OR IDS" in para and "AND" in para


class TestRenderSearchStrategyTable:
    def test_one_row_per_source_per_phase(self):
        rows = [SourceStrategyRow(source="openalex", query_sent='search="x"',
                                    retrieved_at="2026-01-01T00:00:00Z",
                                    n_retrieved=200, total_available=19281, truncated=True)]
        table = render_search_strategy_table([make_phase(1, rows)])
        assert "openalex" in table and "19,281" in table and "200" in table
        assert "truncated" in table

    def test_pipe_in_query_does_not_break_the_table(self):
        rows = [SourceStrategyRow(source="scopus", query_sent="TITLE-ABS-KEY(a|b)")]
        table = render_search_strategy_table([make_phase(1, rows)])
        assert table.count("\n") >= 2  # still renders, didn't explode into extra columns

    def test_empty_input_still_renders_a_valid_table(self):
        table = render_search_strategy_table([])
        assert table.startswith("| Phase |")
