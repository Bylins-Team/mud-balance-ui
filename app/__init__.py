"""Flask factory for the balance simulator web UI.

The factory pattern lets gunicorn / pytest each construct a fresh app
without import-time side effects.
"""

from __future__ import annotations

import os
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

    from . import routes
    app.register_blueprint(routes.bp)
    return app
