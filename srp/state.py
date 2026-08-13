"""
state.py -- RunState: one run workspace on disk (config.json, state.json,
decisions.jsonl) giving per-phase/stage checkpoints for resume and an
append-only decisions log that doubles as a cross-phase "already judged" cache.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from srp.normalize import normalize_doi, normalize_title, record_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload) -> None:
    """Write JSON so an interrupted save can never destroy the previous file.

    state.json is rewritten on every mark_stage, i.e. many times across a
    multi-hour run. The old code opened it "w" (truncating immediately) and then
    streamed json.dump into it, so a Ctrl-C or crash in that window left
    truncated JSON -- and RunState.load has no error handling, so the next resume
    died with a JSONDecodeError and the run's entire checkpoint history was gone
    with no recovery path.

    Writing to a temp file in the same directory and then os.replace() makes the
    swap atomic on both NTFS and POSIX: readers see either the old file or the new
    one, never a half-written one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt here must still clean
        # up the temp file rather than litter the run directory.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# normalize_doi / normalize_title / record_key now live in srp/normalize.py, which
# scripts/dedup.py imports too -- see that module for why sharing them matters.
# Re-exported here so `from srp.state import record_key` keeps working.
__all__ = ["normalize_doi", "normalize_title", "record_key", "RunState"]


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

        _write_json_atomic(run_dir / "config.json", config)

        state = {
            "run_id": run_id,
            "created": _now(),
            "stages": {},
            "current_phase": 1,
        }
        _write_json_atomic(run_dir / "state.json", state)

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

    def save_config(self, config: dict) -> None:
        """Persist an updated config.json -- config.json is otherwise written
        once at create() and never touched again, so anything that lets a
        user edit run settings after the fact (e.g. re-running a phase's
        search with corrected settings) needs this to make the edit durable
        across a resume, not just live in memory for the current process."""
        self.config = config
        _write_json_atomic(self.run_dir / "config.json", config)

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
        _write_json_atomic(self.run_dir / "state.json", self.state)
