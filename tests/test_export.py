"""
Export tests. The key one is bibtex_key collisions: LaTeX does NOT error on a
duplicate key, it silently picks one, so a citation in the manuscript ends up
pointing at the wrong paper with no warning anywhere.
"""
import pytest

from srp.export import (bibtex_key, to_bibtex, to_ris, _escape_bibtex,
                         _split_authors, _year_digits, _suffix_for)


class TestBibtexKeyUniqueness:
    def test_identical_metadata_yields_distinct_keys(self):
        """Same first author, same year, same first title word -- the collision
        case. LaTeX does not error on a duplicate key; it silently picks one, so a
        citation in the manuscript points at the wrong paper."""
        recs = [
            {"authors": "Jane Lee", "year": 2020, "title": "Deep Learning for X"},
            {"authors": "Jane Lee", "year": 2020, "title": "Deep Learning for Y"},
            {"authors": "Jane Lee", "year": 2020, "title": "Deep Learning for Z"},
        ]
        used = set()
        keys = []
        for r in recs:
            k = bibtex_key(r, used)
            used.add(k)
            keys.append(k)
        assert len(set(keys)) == 3, f"duplicate BibTeX keys: {keys}"

    def test_generated_bib_has_no_duplicate_keys(self):
        recs = [{"authors": "Jane Lee", "year": 2020, "title": "Deep Learning for X",
                  "doi": f"10.1/{i}"} for i in range(30)]
        bib = to_bibtex(recs)
        keys = [ln.split("{", 1)[1].rstrip(",\n") for ln in bib.splitlines()
                if ln.startswith("@")]
        assert len(keys) == len(set(keys)) == 30

    def test_suffix_sequence_is_bijective(self):
        """Index 0 is never used as a suffix -- the first record keeps the
        unsuffixed key -- so the sequence starts at 'b' for the second record."""
        assert _suffix_for(1) == "b"
        assert _suffix_for(25) == "z"
        assert _suffix_for(26) == "aa"
        assert _suffix_for(27) == "ab"
        seen = {_suffix_for(i) for i in range(200)}
        assert len(seen) == 200, "suffixes collide"


class TestBibtexEscaping:
    @pytest.mark.parametrize("ch", ["&", "%", "_", "#", "$"])
    def test_specials_are_escaped(self, ch):
        assert f"\\{ch}" in _escape_bibtex(f"a {ch} b")

    def test_backslash_becomes_a_command_not_a_control_sequence(self):
        r"""A title containing '\ok' emitted a literal \ok -- an undefined control
        sequence that fails the LaTeX build on the user's Overleaf project."""
        assert _escape_bibtex(r"100% \ok") == r"100\% \textbackslash{}ok"

    def test_tilde_and_caret_use_literal_commands(self):
        r"""\~ and \^ alone are accents that swallow the next character."""
        assert _escape_bibtex("~approx") == r"\textasciitilde{}approx"
        assert _escape_bibtex("x^2") == r"x\textasciicircum{}2"

    def test_backslash_is_escaped_first_no_double_escaping(self):
        out = _escape_bibtex("a & b")
        assert out == r"a \& b"
        assert r"\textbackslash{}&" not in out

    def test_case_protection_braces_are_preserved(self):
        assert "{Deep}" in _escape_bibtex("Analysis of {Deep} Nets")

    def test_title_is_double_braced(self):
        bib = to_bibtex([{"authors": "A B", "year": 2020, "title": "A Title"}])
        assert "title = {{A Title}}" in bib


class TestSplitAuthors:
    def test_semicolon_is_the_reliable_separator(self):
        assert _split_authors("Jane Smith; Bob Lee") == ["Jane Smith", "Bob Lee"]

    def test_empty(self):
        assert _split_authors("") == []


class TestYearDigits:
    @pytest.mark.parametrize("raw, expected", [
        (2020, "2020"), ("2020", "2020"), (2020.0, "2020"), ("2020-01-01", "2020"),
        ("", ""), (None, ""),
    ])
    def test_shapes(self, raw, expected):
        assert _year_digits(raw) == expected


class TestRis:
    def test_basic_record(self):
        ris = to_ris([{"authors": "Jane Smith", "year": 2020, "title": "A Title",
                        "doi": "10.1/x", "venue": "J"}])
        assert "TI  - A Title" in ris
        assert "DO  - 10.1/x" in ris
        assert ris.strip().endswith("ER  -")
