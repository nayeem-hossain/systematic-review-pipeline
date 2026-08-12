"""
update_check.py -- a soft, non-blocking check against this project's GitHub
Releases for a version newer than the one currently running. Never raises out
to a caller: a missing network, a GitHub outage, a rate limit, or a malformed
response all resolve to "couldn't check right now", not an interrupted
session over something as unimportant as a version nag. This deliberately
does not enforce anything -- see README's "Keeping up to date" section for
why an outdated version is disclosed loudly rather than blocked.
"""
from __future__ import annotations

import re

import requests

REPO = "nayeem-hossain/systematic-review-pipeline"
_RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"


def parse_version(v: str) -> tuple:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). A segment that isn't a plain integer
    (a pre-release suffix, garbage input) sorts as 0 rather than raising --
    a version string this can't parse should never crash a check whose whole
    point is to fail quietly."""
    v = (v or "").strip().lstrip("vV")
    parts = re.split(r"[.\-+]", v)
    out = [int(p) if p.isdigit() else 0 for p in parts[:3]]
    out += [0] * (3 - len(out))
    return tuple(out)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def latest_release_version(timeout: float = 2.0) -> "str | None":
    """The latest GitHub release tag for this project, or None if the check
    could not be completed for any reason. Callers must treat None as
    "unknown", never as "up to date"."""
    try:
        resp = requests.get(_RELEASES_API, timeout=timeout,
                             headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        tag = resp.json().get("tag_name")
        return tag.strip() if isinstance(tag, str) and tag.strip() else None
    except (requests.RequestException, ValueError):
        return None


def check_for_update(current_version: str, timeout: float = 2.0) -> "str | None":
    """The latest version string if it's newer than current_version, else
    None -- either already up to date, or the check couldn't complete, or
    current_version is a dev/uninstalled build (see module docstring: a git
    checkout can be ahead of the last tagged release, not behind it, so it
    is never flagged)."""
    if not current_version or current_version.startswith("0.0.0-dev"):
        return None
    latest = latest_release_version(timeout)
    if latest is None:
        return None
    return latest if is_newer(latest, current_version) else None
