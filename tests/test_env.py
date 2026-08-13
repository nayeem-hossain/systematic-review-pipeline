"""
load_dotenv()'s default .env path used to resolve relative to wherever
srp/env.py itself was installed (REPO_ROOT = Path(__file__).parent.parent) --
correct for a git clone, since the documented workflow `cd
systematic-review-pipeline` first makes cwd == repo root, but for a pip/pipx
install srp/env.py lives inside site-packages, so the old default resolved to
somewhere buried in the installed package -- nowhere a user would ever
plausibly put a .env file. It now resolves relative to the current working
directory instead, which is correct for both cases: the git-clone workflow's
cwd is already the repo root, and a pipx install's cwd is wherever the user
actually ran `slr` from, which is exactly where they'd create a .env.
"""
import os

from srp.env import load_dotenv, parse_env_text, set_env_var, unset_env_var


class TestParseEnvText:
    def test_parses_key_value(self):
        assert parse_env_text("FOO=bar") == {"FOO": "bar"}

    def test_ignores_comments_and_blank_lines(self):
        assert parse_env_text("# a comment\n\nFOO=bar") == {"FOO": "bar"}


class TestLoadDotenvDefaultPath:
    def test_default_path_resolves_from_cwd_not_package_location(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TEST_KEY_XYZ", raising=False)
        (tmp_path / ".env").write_text("TEST_KEY_XYZ=hello\n", encoding="utf-8")

        loaded = load_dotenv()

        assert "TEST_KEY_XYZ" in loaded
        assert os.environ["TEST_KEY_XYZ"] == "hello"
        monkeypatch.delenv("TEST_KEY_XYZ", raising=False)

    def test_no_env_file_in_cwd_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert load_dotenv() == []

    def test_different_cwd_picks_up_a_different_env_file(self, tmp_path, monkeypatch):
        """The whole point of the fix: the SAME call, from a DIFFERENT working
        directory, must load a DIFFERENT .env -- not one fixed at install time."""
        dir_a = tmp_path / "project_a"
        dir_b = tmp_path / "project_b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / ".env").write_text("TEST_KEY_WHICH=a\n", encoding="utf-8")
        (dir_b / ".env").write_text("TEST_KEY_WHICH=b\n", encoding="utf-8")

        monkeypatch.delenv("TEST_KEY_WHICH", raising=False)
        monkeypatch.chdir(dir_a)
        load_dotenv()
        assert os.environ["TEST_KEY_WHICH"] == "a"

        monkeypatch.delenv("TEST_KEY_WHICH", raising=False)
        monkeypatch.chdir(dir_b)
        load_dotenv()
        assert os.environ["TEST_KEY_WHICH"] == "b"
        monkeypatch.delenv("TEST_KEY_WHICH", raising=False)

    def test_explicit_path_still_overrides_the_cwd_default(self, tmp_path, monkeypatch):
        other_dir = tmp_path / "elsewhere"
        other_dir.mkdir()
        (other_dir / "custom.env").write_text("TEST_KEY_ABC=world\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)  # cwd itself has no .env at all
        monkeypatch.delenv("TEST_KEY_ABC", raising=False)

        loaded = load_dotenv(other_dir / "custom.env")

        assert "TEST_KEY_ABC" in loaded
        monkeypatch.delenv("TEST_KEY_ABC", raising=False)


class TestSetEnvVar:
    """Powers the consolidation menu's 'Manage API keys' action -- add/update
    a key in .env without disturbing anything else already in the file."""

    def test_creates_the_file_when_it_does_not_exist(self, tmp_path):
        env_path = tmp_path / ".env"
        set_env_var(env_path, "FOO", "bar")
        assert parse_env_text(env_path.read_text(encoding="utf-8")) == {"FOO": "bar"}

    def test_appends_a_new_key_to_an_existing_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=1\n", encoding="utf-8")
        set_env_var(env_path, "FOO", "bar")
        parsed = parse_env_text(env_path.read_text(encoding="utf-8"))
        assert parsed == {"EXISTING": "1", "FOO": "bar"}

    def test_updates_an_existing_key_in_place_without_duplicating_it(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("# a comment\nFOO=old\nOTHER=2\n", encoding="utf-8")
        set_env_var(env_path, "FOO", "new")
        text = env_path.read_text(encoding="utf-8")
        assert text.count("FOO=") == 1
        assert "# a comment" in text
        assert parse_env_text(text) == {"FOO": "new", "OTHER": "2"}


class TestUnsetEnvVar:
    def test_missing_file_is_a_no_op_not_an_error(self, tmp_path):
        env_path = tmp_path / ".env"
        assert unset_env_var(env_path, "FOO") is False

    def test_removes_the_key_and_reports_it_removed_something(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("FOO=bar\nOTHER=2\n", encoding="utf-8")
        removed = unset_env_var(env_path, "FOO")
        assert removed is True
        assert parse_env_text(env_path.read_text(encoding="utf-8")) == {"OTHER": "2"}

    def test_key_not_present_reports_nothing_removed_and_leaves_file_untouched(self, tmp_path):
        env_path = tmp_path / ".env"
        original = "OTHER=2\n"
        env_path.write_text(original, encoding="utf-8")
        removed = unset_env_var(env_path, "FOO")
        assert removed is False
        assert env_path.read_text(encoding="utf-8") == original

    def test_preserves_comments_and_other_keys(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("# header comment\nFOO=bar\nOTHER=2\n", encoding="utf-8")
        unset_env_var(env_path, "FOO")
        text = env_path.read_text(encoding="utf-8")
        assert "# header comment" in text
        assert "OTHER=2" in text
        assert "FOO" not in text
