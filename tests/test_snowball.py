"""
Citation snowballing (backward/forward via the OpenAlex citation graph) --
distinct from slr.py's title-term "query expansion", which is NOT snowballing.
These tests use response shapes verified against the live OpenAlex API
(api.openalex.org/works/https://doi.org/... and ?filter=cites:...), not
invented ones.
"""
import snowball


def make_work(oa_id, title, year=2021, doi="", referenced=None, authors=None):
    return {
        "id": f"https://openalex.org/{oa_id}",
        "title": title,
        "publication_year": year,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "primary_location": {"source": {"display_name": "Venue"}},
        "authorships": [{"author": {"display_name": a}} for a in (authors or ["A Author"])],
        "referenced_works": [f"https://openalex.org/{r}" for r in (referenced or [])],
        "abstract_inverted_index": None,
    }


class TestOpenalexToCandidate:
    def test_maps_fields_correctly(self):
        w = make_work("W1", "A Title", year=2020, doi="10.1/x", authors=["Jane Smith"])
        c = snowball._openalex_to_candidate(w, "backward")
        assert c.title == "A Title"
        assert c.year == 2020
        assert c.doi == "10.1/x"
        assert c.authors == "Jane Smith"
        assert c.source == "openalex-backward-snowball"

    def test_forward_direction_labeled_distinctly(self):
        w = make_work("W1", "T")
        c = snowball._openalex_to_candidate(w, "forward")
        assert c.source == "openalex-forward-snowball"


class TestBackwardSnowball:
    def test_collects_referenced_ids_across_seeds(self, monkeypatch):
        seed_a = make_work("WA", "Seed A", referenced=["W10", "W11"])
        seed_b = make_work("WB", "Seed B", referenced=["W11", "W12"])  # W11 shared

        captured_filters = []

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        def fake_request(method, url, limiter, **kwargs):
            filt = kwargs["params"]["filter"]
            captured_filters.append(filt)
            ids_in_filter = filt.split(":", 1)[1].split("|")
            results = [make_work(i, f"Title {i}") for i in ids_in_filter]
            return FakeResp({"results": results})

        monkeypatch.setattr(snowball, "_request", fake_request)
        limiter = snowball.RateLimiter(0)
        pairs = snowball.backward_snowball({"a": seed_a, "b": seed_b}, "x@y.z", 50, limiter)

        found_ids = {c.url.rsplit("/", 1)[-1] for c, _ in pairs}
        assert found_ids == {"W10", "W11", "W12"}
        # W11 was referenced by both seeds -- both must be recorded as contributors.
        w11_seeds = next(seeds for c, seeds in pairs if c.url.endswith("W11"))
        assert set(w11_seeds) == {"a", "b"}

    def test_respects_max_per_seed_cap(self, monkeypatch):
        seed = make_work("WA", "Seed A", referenced=[f"W{i}" for i in range(10)])

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        def fake_request(method, url, limiter, **kwargs):
            filt = kwargs["params"]["filter"]
            ids_in_filter = filt.split(":", 1)[1].split("|")
            return FakeResp({"results": [make_work(i, f"T{i}") for i in ids_in_filter]})

        monkeypatch.setattr(snowball, "_request", fake_request)
        limiter = snowball.RateLimiter(0)
        pairs = snowball.backward_snowball({"a": seed}, "x@y.z", max_per_seed=3, limiter=limiter)
        assert len(pairs) == 3


class TestForwardSnowball:
    def test_paginates_until_max_per_seed(self, monkeypatch):
        seed = make_work("WA", "Seed A")
        pages = [
            {"results": [make_work(f"C{i}", f"Citing {i}") for i in range(2)],
             "meta": {"next_cursor": "next"}},
            {"results": [make_work(f"C{i}", f"Citing {i}") for i in range(2, 4)],
             "meta": {"next_cursor": None}},
        ]
        call_count = {"n": 0}

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        def fake_request(method, url, limiter, **kwargs):
            i = call_count["n"]
            call_count["n"] += 1
            return FakeResp(pages[i])

        monkeypatch.setattr(snowball, "_request", fake_request)
        limiter = snowball.RateLimiter(0)
        pairs = snowball.forward_snowball({"seed1": seed}, "x@y.z", max_per_seed=10, limiter=limiter)
        assert len(pairs) == 4
        assert all(seeds == ["seed1"] for _, seeds in pairs)


class TestResolveSeedTitleGuard:
    """The bug this guards against: OpenAlex's title search always returns its
    single best hit, even for a title with no genuine match, so an ungated
    fallback silently resolves an unrelated paper and pulls in ITS entire
    citation subgraph as if it were the seed's."""

    def test_rejects_a_poor_title_match(self, monkeypatch):
        unrelated = make_work("W999", "Completely Unrelated Gardening Techniques")

        class FakeResp:
            def __init__(self, payload, ok=True):
                self._payload = payload
                self.ok = ok
            def json(self):
                return self._payload

        def fake_request(method, url, limiter, **kwargs):
            if "works/https" in url:
                raise __import__("requests").HTTPError("404")
            return FakeResp({"results": [unrelated]})

        monkeypatch.setattr(snowball, "_request", fake_request)
        limiter = snowball.RateLimiter(0)
        result = snowball.resolve_seed(
            "", "Nonexistent Paper Title That Does Not Match Anything At All",
            "x@y.z", limiter)
        assert result is None

    def test_accepts_a_close_title_match(self, monkeypatch):
        close = make_work("W1", "Deep Learning for Network Intrusion Detection Systems")

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        def fake_request(method, url, limiter, **kwargs):
            if "works/https" in url:
                raise __import__("requests").HTTPError("404")
            return FakeResp({"results": [close]})

        monkeypatch.setattr(snowball, "_request", fake_request)
        limiter = snowball.RateLimiter(0)
        result = snowball.resolve_seed(
            "", "Deep Learning for Network Intrusion Detection System", "x@y.z", limiter)
        assert result is not None
        assert result["title"] == close["title"]

    def test_doi_lookup_used_first_when_available(self, monkeypatch):
        calls = []

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        def fake_request(method, url, limiter, **kwargs):
            calls.append(url)
            return FakeResp(make_work("W1", "Found by DOI"))

        monkeypatch.setattr(snowball, "_request", fake_request)
        limiter = snowball.RateLimiter(0)
        result = snowball.resolve_seed("10.1/x", "irrelevant title", "x@y.z", limiter)
        assert result["title"] == "Found by DOI"
        assert "works/https://doi.org/10.1/x" in calls[0]

    def test_doi_with_url_prefix_is_normalized_not_lstripped(self, monkeypatch):
        """Regression: the original code used str.lstrip('https://doi.org/'),
        which strips characters from a REMOVAL SET, not a literal prefix -- a
        latent bug that happened not to fire only because bare DOIs start with
        a digit. normalize_doi is the correct, already-tested fix."""
        calls = []

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        def fake_request(method, url, limiter, **kwargs):
            calls.append(url)
            return FakeResp(make_work("W1", "Found"))

        monkeypatch.setattr(snowball, "_request", fake_request)
        limiter = snowball.RateLimiter(0)
        snowball.resolve_seed("https://doi.org/10.1/dogs", "t", "x@y.z", limiter)
        assert calls[0].endswith("works/https://doi.org/10.1/dogs")
