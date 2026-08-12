"""
search_scopus()'s COMPLETE-view 401/403 handler used to unconditionally blame
"entitlement" and fall back to STANDARD view -- but Elsevier authenticates
Scopus keys by IP range by default, so the exact same 401/403 can just as
easily mean "off-campus with no SCOPUS_INSTTOKEN set", which the old message
never mentioned. The fix doesn't try to pattern-match Elsevier's exact error
text (unverified, and fragile if it changes) -- it just stops asserting a
cause the code cannot actually confirm, and mentions the insttoken angle
whenever one isn't configured.
"""
from unittest.mock import patch, MagicMock

import requests

from search import search_scopus, SourceReport


def _response(status_code=None, payload=None):
    resp = MagicMock()
    if status_code is not None:
        resp.status_code = status_code
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload or {"search-results": {"entry": []}}
    return resp


class TestScopusRefusalDiagnosis:
    def test_with_insttoken_message_says_entitlement(self, capsys):
        """insttoken is set, so an IP-authentication failure is unlikely --
        the message can reasonably point at COMPLETE-view entitlement."""
        with patch("search.requests.request") as mock_request:
            mock_request.side_effect = [
                _response(status_code=401),
                _response(payload={"search-results": {"entry": []}}),
            ]
            search_scopus("test", 2020, 2024, "key", "real-insttoken", 10)

        err = capsys.readouterr().err
        assert "entitlement" in err
        assert "off-campus" not in err.lower()

    def test_without_insttoken_mentions_off_campus_possibility(self, capsys):
        with patch("search.requests.request") as mock_request:
            mock_request.side_effect = [
                _response(status_code=401),
                _response(payload={"search-results": {"entry": []}}),
            ]
            search_scopus("test", 2020, 2024, "key", "", 10)

        err = capsys.readouterr().err
        assert "insttoken" in err.lower() or "off-campus" in err.lower()

    def test_still_falls_back_to_standard_view_and_succeeds(self):
        """Control flow is unchanged: whatever the diagnosis, COMPLETE -> 401
        still retries as STANDARD rather than failing the whole source."""
        with patch("search.requests.request") as mock_request:
            mock_request.side_effect = [
                _response(status_code=401),
                _response(payload={"search-results": {"entry": [
                    {"dc:title": "A Paper", "prism:coverDate": "2021-01-01",
                     "dc:creator": "Smith, J.", "prism:publicationName": "J",
                     "prism:doi": "10.1/x"},
                ]}}),
            ]
            out = search_scopus("test", 2020, 2024, "key", "", 10)

        assert len(out) == 1
        assert mock_request.call_count == 2
        second_params = mock_request.call_args_list[1].kwargs["params"]
        assert second_params["view"] == "STANDARD"

    def test_standard_view_failure_still_raises(self):
        """Once already on STANDARD (not COMPLETE), a 401 is a real failure --
        must propagate, not loop or swallow it."""
        with patch("search.requests.request") as mock_request:
            mock_request.return_value = _response(status_code=401)
            try:
                search_scopus("test", 2020, 2024, "key", "", 10, view="STANDARD")
                assert False, "expected HTTPError to propagate"
            except requests.HTTPError:
                pass
        assert mock_request.call_count == 1

    def test_records_total_available_on_report(self):
        report = SourceReport(source="scopus")
        with patch("search.requests.request") as mock_request:
            mock_request.return_value = _response(payload={
                "search-results": {"entry": [], "opensearch:totalResults": "42"},
            })
            search_scopus("test", 2020, 2024, "key", "", 10, report=report)
        assert report.total_available == 42
