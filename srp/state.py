"""
state.py -- RunState: one run workspace on disk (config.json, state.json,
decisions.jsonl) giving per-phase/stage checkpoints for resume and an
append-only decisions log that doubles as a cross-phase "already judged" cache.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# normalization must match the repo's dedup.py:
# --- core logic ---
def normalize_doi(doi) -> str:
    if not doi or str(doi).lower() == "nan":
        return ""
    s = str(doi).strip().lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi.org/",
    ):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip()


def normalize_title(t) -> str:
    if not t:
        return ""
    s = str(t).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def record_key(doi, title) -> str:
    k = normalize_doi(doi)
    if k:
        return "doi:" + k
    t = normalize_title(title)
    if t:
        return "title:" + t
    return ""


class RunState:
    def __init__(self, run_dir: Path, config: dict, state: dict):
        self.run_dir = Path(run_dir)
        self.config = config
        self.state = state
        self.run_id = state.get("run_id", self.run_dir.name)

    # --- core logic ---
    @classmethod
    def create(cls, base_dir, run_id: str, config: dict) -> "RunState":
        run_dir = Path(base_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        with open(run_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        state = {
            "run_id": run_id,
            "created": _now(),
            "stages": {},
            "current_phase": 1,
        }
        with open(run_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        (run_dir / "decisions.jsonl").touch()

        return cls.load(run_dir)

    @classmethod
    def load(cls, run_dir) -> "RunState":
        run_dir = Path(run_dir)

        config_path = run_dir / "config.json"
        config = {}
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

        with open(run_dir / "state.json", "r", encoding="utf-8") as f:
            state = json.load(f)

        return cls(run_dir, config, state)

    @classmethod
    def list_runs(cls, base_dir) -> list[str]:
        base = Path(base_dir)
        if not base.exists():
            return []
        runs = [
            p.name for p in base.iterdir()
            if p.is_dir() and (p / "state.json").exists()
        ]
        return sorted(runs)

    def phase_dir(self, phase: int) -> Path:
        d = self.run_dir / ("phase_%d" % phase)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def mark_stage(self, phase: int, stage: str, status: str = "done", counts: dict | None = None) -> None:
        key = f"{phase}:{stage}"
        self.state.setdefault("stages", {})[key] = {
            "status": status,
            "ts": _now(),
            "counts": counts or {},
        }
        self.save()

    def stage_status(self, phase: int, stage: str) -> str | None:
        key = f"{phase}:{stage}"
        entry = self.state.get("stages", {}).get(key)
        if entry is None:
            return None
        return entry.get("status")

    def is_stage_done(self, phase: int, stage: str) -> bool:
        return self.stage_status(phase, stage) == "done"

    def record_decision(self, *, record_key: str, decision: str, stage: str, phase: int,
                         reason: str = "", source: str = "", id=None,
                         title: str = "", doi: str = "") -> None:
        entry = {
            "ts": _now(),
            "record_key": record_key,
            "id": id,
            "stage": stage,
            "phase": phase,
            "decision": decision.lower().strip(),
            "reason": reason,
            "source": source,
            "title": title,
            "doi": doi,
        }
        with open(self.run_dir / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def decisions(self) -> list[dict]:
        path = self.run_dir / "decisions.jsonl"
        out: list[dict] = []
        if not path.exists():
            return out
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def decided_keys(self, stage: str | None = None) -> set[str]:
        keys: set[str] = set()
        for d in self.decisions():
            if stage is not None and d.get("stage") != stage:
                continue
            k = d.get("record_key")
            if k:
                keys.add(k)
        return keys

    def decision_for(self, record_key: str, stage: str) -> dict | None:
        result = None
        for d in self.decisions():
            if d.get("record_key") == record_key and d.get("stage") == stage:
                result = d
        return result

    def save(self) -> None:
        with open(self.run_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
