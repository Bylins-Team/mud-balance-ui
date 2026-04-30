"""In-process job queue for mud-sim runs.

Single ThreadPoolExecutor with N workers (default 2). Each submitted
job updates status in meta.json so the viewer can poll: queued ->
running -> ok/failed/timeout.

A separate process queue (RQ/Celery) is overkill until we have multiple
workers / restart resilience requirements. Restart drops in-flight jobs.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import meta as meta_mod
from . import runner, storage

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_max_workers = int(os.environ.get("MUD_SIM_QUEUE_WORKERS", "2"))


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=_max_workers, thread_name_prefix="mud-sim")
    return _executor


def submit(
    handle: storage.RunHandle,
    yaml_text_utf8: str,
    *,
    mud_sim_bin: str,
    world_dir: str,
    timeout_s: int,
) -> None:
    """Enqueue a run. Returns immediately. Status flips to 'queued', then
    'running' when a worker picks it up, then 'ok'/'failed'/'timeout'."""
    # Stash the scenario `rounds:` so /status can report progress as
    # "N/total" without re-parsing YAML on every poll.
    target_rounds = _extract_target_rounds(yaml_text_utf8)
    initial = {
        "status": "queued",
        "scenario_yaml": yaml_text_utf8,
        "queued_at": time.time(),
        "target_rounds": target_rounds,
    }
    storage.write_meta(handle, initial)

    _get_executor().submit(
        _execute_job,
        handle, yaml_text_utf8,
        mud_sim_bin, world_dir, timeout_s,
    )


def _extract_target_rounds(yaml_text: str) -> int:
    """Cheap regex pull of `rounds:` so we can report progress without
    a real YAML parse on every status poll. Default to 100 if absent
    (matches mud-sim's own default)."""
    import re
    m = re.search(r"^\s*rounds\s*:\s*(\d+)", yaml_text, re.MULTILINE)
    return int(m.group(1)) if m else 100


def _execute_job(
    handle: storage.RunHandle,
    yaml_text_utf8: str,
    mud_sim_bin: str,
    world_dir: str,
    timeout_s: int,
) -> None:
    """Worker body. Runs mud-sim, summarises events.jsonl, writes final
    meta.json. All exceptions are caught -- the queue thread must not die
    or subsequent submits silently drop on the floor."""
    started_at = time.time()
    storage.write_meta(handle, {
        "status": "running",
        "scenario_yaml": yaml_text_utf8,
        "started_at": started_at,
        "target_rounds": _extract_target_rounds(yaml_text_utf8),
    })

    try:
        ok, _stderr = runner.run_scenario(
            yaml_text_utf8, handle,
            mud_sim_bin=mud_sim_bin,
            world_dir=world_dir,
            timeout_s=timeout_s,
        )
    except Exception:  # noqa: BLE001
        log.exception("run %s crashed", handle.run_id)
        storage.write_meta(handle, {
            "status": "failed",
            "scenario_yaml": yaml_text_utf8,
            "error": traceback.format_exc(),
            "started_at": started_at,
            "finished_at": time.time(),
        })
        return

    summary = meta_mod.summarize(handle.events_path, yaml_text_utf8)
    summary["status"] = "ok" if ok else "failed"
    summary["started_at"] = started_at
    summary["finished_at"] = time.time()
    storage.write_meta(handle, summary)


def queue_depth() -> int:
    """Approximate number of jobs that haven't completed yet."""
    if _executor is None:
        return 0
    return _executor._work_queue.qsize()  # noqa: SLF001 -- best-effort
