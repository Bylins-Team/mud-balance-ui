"""HTTP endpoints for the balance simulator UI.

Routes:
  GET    /                         redirect to /runs
  GET    /runs                     HTML list of runs
  GET    /runs/new                 HTML form (YAML textarea)
  POST   /runs                     create a run (spawns mud-sim, blocks until done)
  GET    /runs/<id>                viewer page (timeline + 3 panels)
  GET    /runs/<id>/events.jsonl   raw events for download or external tools
  GET    /runs/<id>/api/state      HTMX fragment: state panel for a role at time t
  GET    /runs/<id>/api/log        HTMX fragment: combat log up to time t
  GET    /runs/<id>/api/screen     HTMX fragment: telnet output for a role up to time t
  POST   /runs/<id>/delete         delete a run (HTMX form)
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from . import meta as meta_mod
from . import runner, storage
from .encoding import colour_to_html

bp = Blueprint("ui", __name__)


@bp.route("/")
def index():
    return redirect(url_for("ui.runs_list"))


@bp.route("/runs")
def runs_list():
    runs = []
    for h in storage.list_runs(current_app.config["RUNS_DIR"]):
        runs.append({"handle": h, "meta": storage.load_meta(h)})
    return render_template("runs_list.html", runs=runs)


@bp.route("/runs/new", methods=["GET"])
def runs_new():
    return render_template("runs_new.html", default_yaml=_DEFAULT_SCENARIO)


@bp.route("/runs", methods=["POST"])
def runs_create():
    yaml_text = request.form.get("scenario", "").strip()
    if not yaml_text:
        return "scenario YAML is required", 400

    handle = storage.new_run(current_app.config["RUNS_DIR"])
    try:
        ok, _stderr = runner.run_scenario(
            yaml_text,
            handle,
            mud_sim_bin=current_app.config["MUD_SIM_BIN"],
            world_dir=current_app.config["MUD_SIM_WORLD_DIR"],
            timeout_s=current_app.config["MUD_SIM_TIMEOUT_S"],
        )
    except runner.ScenarioInvalidError as e:
        # Roll back the empty run dir so it does not show up in the list.
        for p in handle.root.glob("*"):
            p.unlink()
        handle.root.rmdir()
        return f"scenario invalid: {e}", 400

    summary = meta_mod.summarize(handle.events_path, yaml_text)
    summary["status"] = "ok" if ok else "failed"
    storage.write_meta(handle, summary)
    return redirect(url_for("ui.run_view", run_id=handle.run_id))


@bp.route("/runs/<run_id>")
def run_view(run_id: str):
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None:
        abort(404)
    return render_template(
        "run_view.html",
        handle=handle,
        meta=storage.load_meta(handle),
        initial_t=request.args.get("t", "0"),
        initial_role=request.args.get("role", "attacker"),
    )


@bp.route("/runs/<run_id>/events.jsonl")
def run_events(run_id: str):
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None or not handle.events_path.is_file():
        abort(404)
    return handle.events_path.read_bytes(), 200, {"Content-Type": "application/x-ndjson"}


@bp.route("/runs/<run_id>/api/state")
def api_state(run_id: str):
    """Latest char_state snapshot for a role with ts <= t."""
    handle, t_ms = _resolve_handle_and_t(run_id)
    role = request.args.get("role", "attacker")
    snapshot: dict | None = None
    for ev in _iter_events(handle.events_path):
        if ev.get("name") != "char_state":
            continue
        if ev.get("role") != role:
            continue
        if int(ev.get("ts", 0)) > t_ms:
            break
        snapshot = ev
    return render_template("partials/state_panel.html", snapshot=snapshot, role=role)


@bp.route("/runs/<run_id>/api/log")
def api_log(run_id: str):
    """All damage/miss/affect_* events with ts <= t, formatted human-readably."""
    handle, t_ms = _resolve_handle_and_t(run_id)
    entries = []
    for ev in _iter_events(handle.events_path):
        if int(ev.get("ts", 0)) > t_ms:
            break
        name = ev.get("name")
        if name == "damage":
            entries.append({
                "ts": ev["ts"],
                "kind": "damage",
                "text": f"{ev.get('attacker_name', '?')} -> {ev.get('victim_name', '?')}: "
                        f"{ev.get('dam', 0)} ({'crit' if ev.get('crit') else 'hit'})",
            })
        elif name == "miss":
            entries.append({
                "ts": ev["ts"],
                "kind": "miss",
                "text": f"{ev.get('attacker_name', '?')} miss "
                        f"({ev.get('reason', '?')})",
            })
        elif name in ("affect_added", "affect_removed"):
            entries.append({
                "ts": ev["ts"],
                "kind": name,
                "text": f"{name}: spell={ev.get('spell_id')} on "
                        f"{ev.get('target_name', '?')}",
            })
    return render_template("partials/log_panel.html", entries=entries)


@bp.route("/runs/<run_id>/api/screen")
def api_screen(run_id: str):
    """All screen_output events for a role with ts <= t, &-codes -> HTML spans."""
    handle, t_ms = _resolve_handle_and_t(run_id)
    role = request.args.get("role", "attacker")
    chunks = []
    for ev in _iter_events(handle.events_path):
        if ev.get("name") != "screen_output":
            continue
        if ev.get("role") != role:
            continue
        if int(ev.get("ts", 0)) > t_ms:
            break
        chunks.append(colour_to_html(ev.get("text", "")))
    return render_template("partials/screen_panel.html", chunks=chunks, role=role)


@bp.route("/runs/<run_id>/delete", methods=["POST"])
def run_delete(run_id: str):
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None:
        abort(404)
    for p in handle.root.glob("*"):
        p.unlink()
    handle.root.rmdir()
    return redirect(url_for("ui.runs_list"))


# -----------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------

def _resolve_handle_and_t(run_id: str):
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None:
        abort(404)
    try:
        t_seconds = float(request.args.get("t", "0"))
    except ValueError:
        t_seconds = 0.0
    # `t` is seconds-from-first-event; we resolve to absolute ms via meta.first_ts_ms.
    meta = storage.load_meta(handle)
    base = int(meta.get("first_ts_ms", 0))
    t_ms = base + int(t_seconds * 1000)
    return handle, t_ms


def _iter_events(events_path: Path):
    if not events_path.is_file():
        return
    with events_path.open() as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


_DEFAULT_SCENARIO = """\
seed: 42
rounds: 50
attacker: { type: player, class: bogatyr, level: 30 }
victim:   { type: mob, vnum: 102 }
"""
