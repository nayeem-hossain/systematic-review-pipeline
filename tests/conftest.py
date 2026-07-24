"""
Shared pytest fixtures.

Makes both `srp` (package) and `scripts` (standalone CLI modules) importable
without installing the project, which is what the sys.path shims in scripts/*.py
do at runtime.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


CANDIDATE_COLUMNS = ["id", "title", "authors", "doi", "year", "venue", "url",
                      "source", "abstract"]


def make_candidates(rows) -> pd.DataFrame:
    """Build a candidates-shaped frame from (id, title, authors, doi, year) tuples.

    An empty `rows` still yields the full column set, matching what search.py
    writes for a zero-hit search -- otherwise tests of the empty case exercise a
    shape the pipeline never actually produces.
    """
    return pd.DataFrame([
        {"id": i, "title": t, "authors": a, "doi": d, "year": y,
         "venue": "V", "url": f"http://example/{i}", "source": "openalex",
         "abstract": "abstract text"}
        for i, t, a, d, y in rows
    ], columns=CANDIDATE_COLUMNS)


@pytest.fixture
def candidates():
    return make_candidates
