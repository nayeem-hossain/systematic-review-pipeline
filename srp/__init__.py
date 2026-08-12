from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: pyproject.toml's [project] version. Reading it
    # here instead of hardcoding a second copy is what makes this number
    # impossible to drift out of sync with what pip/pipx actually installed.
    __version__ = version("systematic-review-pipeline")
except PackageNotFoundError:
    # Running from a git clone that was never `pip install`-ed (the current
    # default workflow: `pip install -r requirements.txt` only installs
    # dependencies, not this package itself).
    __version__ = "0.0.0-dev"

