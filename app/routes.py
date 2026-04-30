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
import shutil
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from . import meta as meta_mod
from . import runner, storage, world
from .encoding import colour_to_html
from flask import jsonify

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
    # The form generates its own YAML via JS from structured fields; no
    # server-side default needed.
    return render_template("runs_new.html")


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
        # Roll back the run dir so it does not show up in the list.
        shutil.rmtree(handle.root, ignore_errors=True)
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
    # Start at round=-1 so the viewer opens on the pre-fight snapshot
    # (slider left edge). Older `?t=` query strings are not migrated --
    # the viewer was the only consumer of them.
    return render_template(
        "run_view.html",
        handle=handle,
        meta=storage.load_meta(handle),
        initial_round=request.args.get("round", "-1"),
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
    """Latest char_state snapshot for a role at round <= round_no."""
    handle, round_no = _resolve_handle_and_round(run_id)
    role = request.args.get("role", "attacker")
    snapshot: dict | None = None
    for ev in _iter_events(handle.events_path):
        if ev.get("name") != "char_state":
            continue
        if ev.get("role") != role:
            continue
        ev_round = int(ev.get("round", -1))
        if ev_round > round_no:
            break
        snapshot = ev
    return render_template("partials/state_panel.html", snapshot=snapshot, role=role)


@bp.route("/runs/<run_id>/api/log")
def api_log(run_id: str):
    """All damage/miss/affect_* events up to and including round_no.

    These engine-side events don't carry a `round` attribute, so we use
    the wall-clock cutoff from meta.round_ts (built at run-summary time).
    """
    handle, round_no = _resolve_handle_and_round(run_id)
    cutoff_ms = _round_ts_cutoff(storage.load_meta(handle), round_no)
    entries = []
    # round=-1 -> pre-fight, no events yet. cutoff_ms==0 means "before any
    # `round` event was emitted", so we filter everything out.
    if cutoff_ms == 0:
        return render_template("partials/log_panel.html", entries=entries)
    for ev in _iter_events(handle.events_path):
        if int(ev.get("ts", 0)) > cutoff_ms:
            break
        name = ev.get("name")
        if name == "damage":
            verb = "крит" if ev.get("crit") else "удар"
            charmie = " (питомец)" if ev.get("attacker_is_charmie") else ""
            entries.append({
                "ts": ev["ts"],
                "kind": "damage",
                "text": f"{ev.get('attacker_name', '?')}{charmie} → "
                        f"{ev.get('victim_name', '?')}: "
                        f"{ev.get('dam', 0)} ({verb})",
            })
        elif name == "miss":
            entries.append({
                "ts": ev["ts"],
                "kind": "miss",
                "text": f"{ev.get('attacker_name', '?')} промах "
                        f"({ev.get('reason', '?')})",
            })
        elif name == "affect_added":
            entries.append({
                "ts": ev["ts"],
                "kind": "affect_added",
                "text": f"+ аффект spell_id={ev.get('spell_id')} на "
                        f"{ev.get('target_name', '?')}",
            })
        elif name == "affect_removed":
            entries.append({
                "ts": ev["ts"],
                "kind": "affect_removed",
                "text": f"− аффект spell_id={ev.get('spell_id')} с "
                        f"{ev.get('target_name', '?')}",
            })
    return render_template("partials/log_panel.html", entries=entries)


@bp.route("/runs/<run_id>/api/screen")
def api_screen(run_id: str):
    """All screen_output events for a role with round <= round_no."""
    handle, round_no = _resolve_handle_and_round(run_id)
    role = request.args.get("role", "attacker")
    chunks = []
    for ev in _iter_events(handle.events_path):
        if ev.get("name") != "screen_output":
            continue
        if ev.get("role") != role:
            continue
        if int(ev.get("round", -1)) > round_no:
            break
        chunks.append(colour_to_html(ev.get("text", "")))
    return render_template("partials/screen_panel.html", chunks=chunks, role=role)


@bp.route("/api/spells")
def api_spells():
    """Autocomplete for `action.spell`. ?q= filters by substring (rus/eng)."""
    q = request.args.get("q", "").strip()
    world_dir = Path(current_app.config["MUD_SIM_WORLD_DIR"])
    items = world.search_spells(world_dir, q)
    return jsonify([{"rus": s.rus, "eng": s.eng} for s in items])


@bp.route("/api/mobs")
def api_mobs():
    """Autocomplete for mob participants. ?q= filters by name or vnum."""
    q = request.args.get("q", "").strip()
    world_dir = Path(current_app.config["MUD_SIM_WORLD_DIR"])
    items = world.search_mobs(world_dir, q)
    return jsonify([{"vnum": m.vnum, "name": m.name} for m in items])


@bp.route("/api/objects")
def api_objects():
    """Autocomplete for inventory. ?slot=wield|body|... narrows by wear-flag,
    ?q= filters by name/vnum substring."""
    q = request.args.get("q", "").strip()
    slot = request.args.get("slot", "").strip()
    world_dir = Path(current_app.config["MUD_SIM_WORLD_DIR"])
    items = world.search_objects(world_dir, slot, q)
    return jsonify([
        {"vnum": o.vnum, "name": o.name, "type": o.obj_type, "wear_flags": list(o.wear_flags)}
        for o in items
    ])


@bp.route("/runs/<run_id>/delete", methods=["POST"])
def run_delete(run_id: str):
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None:
        abort(404)
    # mud-sim drops `log/` subdir inside the run dir for syslog/errlog,
    # so we need a recursive remove rather than per-file unlink.
    shutil.rmtree(handle.root)
    return redirect(url_for("ui.runs_list"))


# -----------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------

def _resolve_handle_and_round(run_id: str) -> tuple[storage.RunHandle, int]:
    """Resolve a run by id and parse `?round=` from the request.

    Round-based timeline is sturdier than wall-clock seconds: a 5-round
    duel finishes in 0.4s of real time, so a seconds-slider is useless.
    Round numbering follows the JSONL: -1 = pre-fight snapshot, 0..N-1
    are the per-pulse_violence rounds.
    """
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None:
        abort(404)
    try:
        round_no = int(request.args.get("round", "0"))
    except ValueError:
        round_no = 0
    return handle, round_no


def _round_ts_cutoff(meta: dict, round_no: int) -> int:
    """Find the latest ts_ms whose 'round' event has round <= round_no.

    Used to filter timeline-less events (damage, miss, affect_*) by
    "everything that happened up to and including round N". meta.round_ts
    is built by app.meta.summarize and indexed by round (-1, 0, 1, ...).
    """
    table = meta.get("round_ts") or []
    if not table:
        return 0
    if round_no < 0:
        return 0
    if round_no >= len(table):
        return int(table[-1] or 0)
    return int(table[round_no] or 0)


def _iter_events(events_path: Path):
    if not events_path.is_file():
        return
    with events_path.open() as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


