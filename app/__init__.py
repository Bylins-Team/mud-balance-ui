"""Flask factory for the balance simulator web UI.

The factory pattern lets gunicorn / pytest each construct a fresh app
without import-time side effects.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    app.config.update(
        MUD_SIM_BIN=os.environ.get("MUD_SIM_BIN", "/opt/mud-sim"),
        MUD_SIM_WORLD_DIR=os.environ.get("MUD_SIM_WORLD_DIR", "/opt/small"),
        RUNS_DIR=Path(os.environ.get("RUNS_DIR", "/data/runs")),
        MUD_SIM_TIMEOUT_S=int(os.environ.get("MUD_SIM_TIMEOUT_S", "120")),
    )
    app.config["RUNS_DIR"].mkdir(parents=True, exist_ok=True)

    from . import routes, world
    app.register_blueprint(routes.bp)

    # Jinja helper used by partials/state_panel.html to look up an item's
    # applies summary by vnum -- avoids having mud-sim emit the full
    # apply list per char_state event (it only writes 'slot:vnum:name').
    @app.template_global()
    def obj_tooltip(vnum: int) -> str:
        try:
            v = int(vnum)
        except (TypeError, ValueError):
            return ""
        obj = world.get_object(Path(app.config["MUD_SIM_WORLD_DIR"]), v)
        return world.obj_summary(obj) if obj else ""

    # Recover orphaned runs: anything that says queued/running on boot
    # was interrupted (gunicorn restart, container recreate). Mark them
    # failed so the viewer doesn't poll forever.
    _recover_orphaned_runs(app.config["RUNS_DIR"])

    # Warm the spell/mob/object caches in a background thread so the first
    # /api/* request doesn't block 60s on YAML parsing of a big world.
    _warm_caches_async(Path(app.config["MUD_SIM_WORLD_DIR"]))

    return app


def _recover_orphaned_runs(runs_dir: Path) -> None:
    """Mark queued/running runs as 'failed' on app startup.

    The in-process job queue dies with gunicorn; surviving meta.json files
    that say running/queued are zombies. Without this fixup the viewer
    polls /status forever and the runs list shows a permanent ⏳ badge.
    """
    from . import storage

    log = logging.getLogger(__name__)
    for handle in storage.list_runs(runs_dir):
        meta = storage.load_meta(handle)
        if meta.get("status") in ("queued", "running"):
            meta["status"] = "failed"
            meta["error"] = "interrupted: container restarted before mud-sim finished"
            storage.write_meta(handle, meta)
            log.warning("recovered orphaned run %s", handle.run_id)


def _warm_caches_async(world_dir: Path) -> None:
    """Kick off spell/mob/object cache population on app start.

    Each of the three loaders runs in its own daemon thread (they touch
    independent files, no contention), and within load_mobs/load_objects
    YAML parsing itself parallelises across files via a ThreadPoolExecutor.
    Subsequent /api/* requests hit the warm in-memory cache.
    """
    from . import world

    log = logging.getLogger(__name__)

    def _warm(name, fn):
        try:
            log.info("warmup: %s", name)
            fn(world_dir)
            log.info("warmup: %s done", name)
        except Exception:  # noqa: BLE001
            log.exception("warmup %s failed", name)

    for name, fn in (("spells", world.load_spells),
                     ("mobs", world.load_mobs),
                     ("objects", world.load_objects)):
        threading.Thread(target=_warm, args=(name, fn),
                         name=f"warmup-{name}", daemon=True).start()
