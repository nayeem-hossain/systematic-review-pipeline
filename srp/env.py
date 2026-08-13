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


def _line_key(line: str) -> "str | None":
    """The KEY a .env line defines, or None if the line is blank, a comment,
    or has no '=' -- mirrors parse_env_text's own line-skipping rules so
    set/unset never mistake a comment for the key it happens to mention."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    key, sep, _ = stripped.partition("=")
    return key.strip() if sep and key.strip() else None


def set_env_var(path, key: str, value: str) -> None:
    """Add or update KEY=value in the .env at `path`, in place -- every other
    line (comments, blanks, unrelated keys, their order) is left untouched.
    Creates the file if it doesn't exist yet. The value is written raw (no
    quoting), matching how .env.example itself writes plain values."""
    env_path = Path(path)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    new_line = f"{key}={value}"
    out_lines = []
    replaced = False
    for line in lines:
        if not replaced and _line_key(line) == key:
            out_lines.append(new_line)
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        out_lines.append(new_line)
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def unset_env_var(path, key: str) -> bool:
    """Remove KEY's line from the .env at `path`, if present. Returns whether
    a line was actually removed. A missing file is a no-op, not an error."""
    env_path = Path(path)
    if not env_path.is_file():
        return False
    lines = env_path.read_text(encoding="utf-8").splitlines()
    out_lines = [line for line in lines if _line_key(line) != key]
    removed = len(out_lines) != len(lines)
    if removed:
        env_path.write_text("\n".join(out_lines) + "\n" if out_lines else "", encoding="utf-8")
    return removed


def load_dotenv(path=None, override: bool = False) -> list[str]:
    """Load `path` (default: .env in the current working directory) into os.environ.

    The default is resolved against the cwd at call time, not the location of
    this installed module -- a pip/pipx install puts this file inside
    site-packages, nowhere a user would ever put a .env, so the file that
    actually matters is the one next to wherever the user ran the tool from.

    Returns the names of the variables actually set, so a caller can tell the
    user what was picked up. A missing or unreadable .env is not an error -- it
    is the normal case for someone passing every value as a flag.
    """
    env_path = Path(path) if path is not None else Path.cwd() / ".env"
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
