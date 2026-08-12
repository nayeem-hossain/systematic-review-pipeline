"""
update_check.py's whole job is to fail quietly: a missing network, a GitHub
outage, a rate limit, or a malformed response must all resolve to "couldn't
check", never an exception that interrupts a review session over something as
unimportant as a version nag.
"""
from unittest.mock import patch, MagicMock

import pytest
import requests

from srp.update_check import (parse_version, is_newer, latest_release_version,
                               check_for_update)


class TestParseVersion:
    @pytest.mark.parametrize("raw, expected", [
        ("1.2.3", (1, 2, 3)),
        ("v1.2.3", (1, 2, 3)),
        ("V1.2.3", (1, 2, 3)),
        ("1.2", (1, 2, 0)),
        ("1", (1, 0, 0)),
        ("1.2.3.4", (1, 2, 3)),
        ("1.2.3-rc1", (1, 2, 3)),
        ("0.0.0-dev", (0, 0, 0)),
        ("garbage", (0, 0, 0)),
        ("", (0, 0, 0)),
    ])
    def test_parses(self, raw, expected):
        assert parse_version(raw) == expected


class TestIsNewer:
    def test_higher_version_is_newer(self):
        assert is_newer("1.1.0", "1.0.0") is True

    def test_equal_version_is_not_newer(self):
        assert is_newer("1.0.0", "1.0.0") is False

    def test_lower_version_is_not_newer(self):
        assert is_newer("1.0.0", "1.1.0") is False

    def test_v_prefix_does_not_affect_comparison(self):
        assert is_newer("v1.1.0", "1.0.0") is True

    def test_patch_level_is_compared(self):
        assert is_newer("1.0.2", "1.0.1") is True


class TestLatestReleaseVersion:
    def test_returns_tag_name_on_success(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"tag_name": "v1.2.0"}
        with patch("srp.update_check.requests.get", return_value=resp):
            assert latest_release_version() == "v1.2.0"

    def test_network_failure_returns_none_not_raises(self):
        with patch("srp.update_check.requests.get",
                    side_effect=requests.ConnectionError("offline")):
            assert latest_release_version() is None

    def test_timeout_returns_none_not_raises(self):
        with patch("srp.update_check.requests.get",
                    side_effect=requests.Timeout("timed out")):
            assert latest_release_version() is None

    def test_http_error_returns_none_not_raises(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("404")
        with patch("srp.update_check.requests.get", return_value=resp):
            assert latest_release_version() is None

    def test_malformed_json_returns_none_not_raises(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("not json")
        with patch("srp.update_check.requests.get", return_value=resp):
            assert latest_release_version() is None

    def test_missing_tag_name_returns_none(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {}
        with patch("srp.update_check.requests.get", return_value=resp):
            assert latest_release_version() is None


class TestCheckForUpdate:
    def test_returns_latest_when_current_is_behind(self):
        with patch("srp.update_check.latest_release_version", return_value="v1.1.0"):
            assert check_for_update("1.0.0") == "v1.1.0"

    def test_returns_none_when_up_to_date(self):
        with patch("srp.update_check.latest_release_version", return_value="v1.0.0"):
            assert check_for_update("1.0.0") is None

    def test_returns_none_when_check_fails(self):
        with patch("srp.update_check.latest_release_version", return_value=None):
            assert check_for_update("1.0.0") is None

    def test_dev_install_is_never_flagged(self):
        """Running from a git clone (not pip-installed) could be AHEAD of the
        last tagged release, not behind it -- comparing '0.0.0-dev' against
        any real version would always look 'outdated', which is actively
        wrong for someone on a newer main branch."""
        with patch("srp.update_check.latest_release_version", return_value="v99.0.0"):
            assert check_for_update("0.0.0-dev") is None
