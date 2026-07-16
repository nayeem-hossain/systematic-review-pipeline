"""
config.py -- ReviewConfig: the run-wide settings for a systematic-review
pipeline run (topic, keywords, sources, thresholds), with JSON save/load.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ReviewConfig:
    topic: str = ""
    keywords: list[str] = field(default_factory=list)
    year_from: int = 2020
    year_to: int = 2026
    mailto: str = ""
    s2_api_key: str = ""
    core_api_key: str = ""
    sources: list[str] = field(
        default_factory=lambda: ["openalex", "semanticscholar", "crossref", "arxiv"]
    )
    max_per_source: int = 200
    n_phases: int = 1
    title_threshold: int = 92
    assist_tool_name: str = ""  # e.g. "ChatGPT (free web)" -- named in the AI-assistance disclosure
    reviewer: str = ""

    # --- core logic ---
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)

    def save(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path) -> "ReviewConfig":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return cls.from_dict(d)

    def search_query(self) -> str:
        parts = []
        for kw in self.keywords:
            if " " in kw:
                parts.append(f'"{kw}"')
            else:
                parts.append(kw)
        return " AND ".join(parts)
