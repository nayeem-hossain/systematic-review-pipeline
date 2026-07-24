"""
env.py -- load a .env file into the process environment.

Why this exists: the README's first setup step is `cp .env.example .env`, and
every stage script's error text offers "set MAILTO in the environment / .env" --
but nothing in the codebase ever read that file, so .env silently did nothing.
This is the reader those docs always implied.

Deliberately dependency-free: python-dotenv would be a seventh runtime dependency
for ~30 lines of parsing, and this project's dependency list is a reproducibility
surface, not just an install cost.

Deliberately conservative about precedence. A real environment variable always
beats the file, and an explicit CLI flag always beats both (the scripts use
`default=os.environ.get(...)`, so argparse resolves in that order for free).
That ordering matters: a user who exports a key for one run must not be silently
overridden by a stale value in .env.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


def _strip_inline_comment(value: str) -> str:
    """Remove a trailing ` # comment` from an UNQUOTED value.

    Requires whitespace before the '#' so that a value which legitimately
    contains one (`pass#word`) survives. Quoted values never reach here.
    """
    for i, ch in enumerate(value):
        if ch == "#" and i > 0 and value[i - 1] in " \t":
            return value[:i]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """Parse .env content into a mapping. Ignores blanks, comments, and any line
    without an '='. Tolerates a leading `export `. Strips one matching pair of
    surrounding quotes."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')
        if quoted:
            value = value[1:-1]
        else:
            value = _strip_inline_comment(value).strip()
        out[key] = value
    return out


def load_dotenv(path=None, override: bool = False) -> list[str]:
    """Load `path` (default: .env at the repo root) into os.environ.

    Returns the names of the variables actually set, so a caller can tell the
    user what was picked up. A missing or unreadable .env is not an error -- it
    is the normal case for someone passing every value as a flag.
    """
    env_path = Path(path) if path is not None else DEFAULT_ENV_PATH
    if not env_path.is_file():
        return []
    try:
        text = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    loaded: list[str] = []
    for key, value in parse_env_text(text).items():
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
