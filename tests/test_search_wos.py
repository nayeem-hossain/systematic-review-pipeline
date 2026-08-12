"""
search_wos() -- Clarivate Web of Science Expanded API. Field paths and the
dict-or-list cardinality quirk (a repeatable field collapses to a bare dict
when there is exactly one, a list when there are more -- names, identifiers,
titles, abstract paragraphs all do this) were verified against a real record
pulled live from the API, not reconstructed from documentation alone.
"""
from unittest.mock import patch, MagicMock

import pytest

from search import (search_wos, _wos_as_list, _wos_titles, _wos_authors,
                     _wos_doi, _wos_abstract, Candidate, SourceReport)


def _record(title="A Study", source="SOME JOURNAL", pubyear=2024,
            authors=None, doi="10.1/x", abstract_paragraphs=None):
    """Build a WoS REC-shaped dict. authors/abstract_paragraphs accept a list
    to exercise the list-cardinality shape; a single string exercises the
    bare-dict shape (via the caller wrapping it appropriately)."""
    if authors is None:
        authors = [{"role": "author", "display_name": "Kumar, Pawan"}]
    name_node = authors[0] if len(authors) == 1 else authors

    identifiers = [{"type": "issn", "value": "1234-5678"}]
    if doi:
        identifiers.append({"type": "doi", "value": doi})
    ident_node = identifiers[0] if len(identifiers) == 1 else identifiers

    if abstract_paragraphs is None:
        abstract_paragraphs = ["Background text.", "Method text."]
    p_node = abstract_paragraphs[0] if len(abstract_paragraphs) == 1 else abstract_paragraphs

    return {
        "UID": "WOS:000000000000001",
        "static_data": {
            "summary": {
                "titles": {"title": [
                    {"type": "source", "content": source},
                    {"type": "item", "content": title},
                ]},
                "pub_info": {"pubyear": pubyear},
                "names": {"name": name_node},
            },
            "fullrecord_metadata": {
                "abstracts": {"count": len(abstract_paragraphs),
                              "abstract": {"abstract_text": {"p": p_node}}},
            },
        },
        "dynamic_data": {
            "cluster_related": {"identifiers": {"identifier": ident_node}},
        },
    }


class TestWosAsList:
    def test_none_becomes_empty_list(self):
        assert _wos_as_list(None) == []

    def test_bare_dict_becomes_one_item_list(self):
        assert _wos_as_list({"a": 1}) == [{"a": 1}]

    def test_list_passes_through_unchanged(self):
        assert _wos_as_list([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]

    def test_bare_string_becomes_one_item_list(self):
        assert _wos_as_list("hello") == ["hello"]


class TestWosFieldExtraction:
    def test_titles_separates_item_from_source(self):
        rec = _record(title="Paper Title", source="Journal Name")
        titles = _wos_titles(rec)
        assert titles["item"] == "Paper Title"
        assert titles["source"] == "Journal Name"

    def test_single_author_bare_dict_is_extracted(self):
        rec = _record(authors=[{"role": "author", "display_name": "Kumar, Pawan"}])
        assert _wos_authors(rec) == "Kumar, Pawan"

    def test_multiple_authors_list_are_joined(self):
        rec = _record(authors=[
            {"role": "author", "display_name": "Kumar, Pawan"},
            {"role": "author", "display_name": "Singh, Raj"},
        ])
        assert _wos_authors(rec) == "Kumar, Pawan; Singh, Raj"

    def test_single_identifier_bare_dict_finds_doi(self):
        rec = _record(doi="10.9/only")
        # force a single-identifier record (no issn)
        rec["dynamic_data"]["cluster_related"]["identifiers"]["identifier"] = \
            {"type": "doi", "value": "10.9/only"}
        assert _wos_doi(rec) == "10.9/only"

    def test_multiple_identifiers_list_finds_doi_among_others(self):
        rec = _record(doi="10.1/x")
        assert _wos_doi(rec) == "10.1/x"

    def test_no_doi_identifier_returns_empty(self):
        rec = _record(doi=None)
        assert _wos_doi(rec) == ""

    def test_single_abstract_paragraph_bare_string(self):
        rec = _record(abstract_paragraphs=["Only one paragraph."])
        assert _wos_abstract(rec) == "Only one paragraph."

    def test_multiple_abstract_paragraphs_are_joined(self):
        rec = _record(abstract_paragraphs=["First.", "Second."])
        assert _wos_abstract(rec) == "First. Second."

    def test_missing_abstracts_returns_empty(self):
        rec = _record()
        del rec["static_data"]["fullrecord_metadata"]["abstracts"]
        assert _wos_abstract(rec) == ""


def _fake_response(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _wos_payload(records, records_found):
    return {
        "QueryResult": {"QueryID": 1, "RecordsSearched": 1000, "RecordsFound": records_found},
        "Data": {"Records": {"records": {"REC": records} if records else ""}},
    }


class TestSearchWos:
    def test_sends_x_apikey_header_and_ts_py_query(self):
        with patch("search.requests.request") as mock_request:
            mock_request.return_value = _fake_response(_wos_payload([], 0))
            search_wos("machine learning", 2020, 2024, "fake-key", 10)

        _, kwargs = mock_request.call_args
        assert kwargs["headers"]["X-ApiKey"] == "fake-key"
        assert kwargs["params"]["usrQuery"] == "TS=(machine learning) AND PY=(2020-2024)"
        assert kwargs["params"]["databaseId"] == "WOS"

    def test_parses_records_into_candidates(self):
        rec = _record(title="On Machine Learning", source="J. of Testing",
                       pubyear=2023, doi="10.1/abc")
        with patch("search.requests.request") as mock_request:
            mock_request.return_value = _fake_response(_wos_payload([rec], 1))
            out = search_wos("test", 2020, 2024, "fake-key", 10)

        assert len(out) == 1
        c = out[0]
        assert isinstance(c, Candidate)
        assert c.source == "wos"
        assert c.title == "On Machine Learning"
        assert c.venue == "J. of Testing"
        assert c.year == 2023
        assert c.doi == "10.1/abc"
        assert c.url == "https://doi.org/10.1/abc"
        assert "Background" in c.abstract

    def test_stops_when_fewer_records_returned_than_requested(self):
        rec = _record()
        with patch("search.requests.request") as mock_request:
            mock_request.return_value = _fake_response(_wos_payload([rec], 1))
            out = search_wos("test", 2020, 2024, "fake-key", 50)

        assert len(out) == 1
        assert mock_request.call_count == 1

    def test_pages_across_multiple_requests(self):
        """max_records exceeds the per-request page-size cap, so a full first
        page (== the cap) must trigger a second request rather than stopping."""
        page1 = _wos_payload([_record(title="First")], 2)
        page2 = _wos_payload([_record(title="Second")], 2)
        with patch("search.requests.request") as mock_request, \
             patch("search.WOS_MAX_PER_REQUEST", 1):
            mock_request.side_effect = [_fake_response(page1), _fake_response(page2)]
            out = search_wos("test", 2020, 2024, "fake-key", 2)

        assert [c.title for c in out] == ["First", "Second"]
        assert mock_request.call_count == 2
        second_call_params = mock_request.call_args_list[1].kwargs["params"]
        assert second_call_params["firstRecord"] == 2

    def test_empty_results_returns_empty_list(self):
        with patch("search.requests.request") as mock_request:
            mock_request.return_value = _fake_response(_wos_payload([], 0))
            out = search_wos("nonexistent topic xyz", 2020, 2024, "fake-key", 10)
        assert out == []

    def test_records_total_available_on_report(self):
        report = SourceReport(source="wos")
        with patch("search.requests.request") as mock_request:
            mock_request.return_value = _fake_response(_wos_payload([], 525642))
            search_wos("test", 2020, 2024, "fake-key", 10, report)
        assert report.total_available == 525642
