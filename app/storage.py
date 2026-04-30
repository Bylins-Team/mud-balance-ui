"""Filesystem layout for run history.

One run = one directory under RUNS_DIR/<ulid>/. The ULID is sortable, so
listing newest-first is just a reverse sort on directory name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import ulid


@dataclass(frozen=True)
class RunHandle:
    """Filesystem paths for one run. Cheap to construct, no I/O."""

    run_id: str
    root: Path

    @property
    def scenario_path(self) -> Path:
        return self.root / "scenario.yaml"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def meta_path(self) -> Path:
        return self.root / "meta.json"

    @property
    def stderr_path(self) -> Path:
        return self.root / "stderr.log"


def new_run(runs_dir: Path) -> RunHandle:
    """Allocate a fresh run directory with a ULID id."""
    run_id = str(ulid.new())
    root = runs_dir / run_id
    root.mkdir(parents=True, exist_ok=False)
    return RunHandle(run_id=run_id, root=root)


def get_run(runs_dir: Path, run_id: str) -> RunHandle | None:
    """Return a handle for an existing run, or None if not found."""
    root = runs_dir / run_id
    if not root.is_dir():
        return None
    return RunHandle(run_id=run_id, root=root)


def list_runs(runs_dir: Path) -> Iterator[RunHandle]:
    """Iterate all runs newest-first (ULIDs sort lexicographically by time)."""
    if not runs_dir.is_dir():
        return iter(())
    return (
        RunHandle(run_id=p.name, root=p)
        for p in sorted(runs_dir.iterdir(), reverse=True)
        if p.is_dir()
    )


def load_meta(handle: RunHandle) -> dict:
    """Read meta.json. Empty dict if missing/corrupt -- don't crash the list page."""
    try:
        return json.loads(handle.meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_meta(handle: RunHandle, meta: dict) -> None:
    handle.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
