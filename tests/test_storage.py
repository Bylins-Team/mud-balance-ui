"""Smoke tests for app/storage.py.

We don't run mud-sim from tests (that would require a full build). The
tests cover only the on-disk layout: that new_run / get_run / list_runs
agree on a stable directory structure.
"""

from __future__ import annotations

from pathlib import Path

from app.storage import get_run, list_runs, load_meta, new_run, write_meta


def test_new_run_creates_dir(tmp_path: Path) -> None:
    h = new_run(tmp_path)
    assert h.root.is_dir()
    assert h.run_id
    assert h.scenario_path == h.root / "scenario.yaml"


def test_list_runs_newest_first(tmp_path: Path) -> None:
    h1 = new_run(tmp_path)
    h2 = new_run(tmp_path)  # ULID -> later one sorts after
    ids = [h.run_id for h in list_runs(tmp_path)]
    assert ids == sorted([h1.run_id, h2.run_id], reverse=True)


def test_get_run_round_trip(tmp_path: Path) -> None:
    h = new_run(tmp_path)
    again = get_run(tmp_path, h.run_id)
    assert again is not None
    assert again.root == h.root


def test_get_run_missing_returns_none(tmp_path: Path) -> None:
    assert get_run(tmp_path, "nonexistent-id") is None


def test_meta_round_trip(tmp_path: Path) -> None:
    h = new_run(tmp_path)
    write_meta(h, {"dpr": 42, "attacker": "bogatyr"})
    assert load_meta(h) == {"dpr": 42, "attacker": "bogatyr"}


def test_load_meta_missing_returns_empty(tmp_path: Path) -> None:
    h = new_run(tmp_path)
    assert load_meta(h) == {}
