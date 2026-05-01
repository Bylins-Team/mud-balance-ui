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
import time as _time
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from . import jobqueue, meta as meta_mod
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


# PC class options shown in the form. Order is from pc_classes.xml in the
# engine. Both attacker and victim selectors render this -- single source
# of truth so the two selects can't drift.
PC_CLASSES = [
    ("bogatyr",        "богатырь"),
    ("naemnik",        "наёмник"),
    ("kudesnik",       "кудесник"),
    ("koldun",         "колдун"),
    ("lekar",          "лекарь"),
    ("ohotnik",        "охотник"),
    ("volkhv",         "волхв"),
    ("druzhinnik",     "дружинник"),
    ("kupets",         "купец"),
    ("chernoknizhnik", "чернокнижник"),
    ("vityaz",         "витязь"),
    ("tat",            "тать"),
    ("kuznets",        "кузнец"),
    ("volshebnik",     "волшебник"),
]


@bp.route("/runs/new", methods=["GET"])
def runs_new():
    # Prefill the form from an existing run if `?from=<run-id>` is present.
    # Falls back to localStorage on the client if no `from` and no value
    # is found there either, the form generates its own default.
    prefill_yaml = ""
    src_id = request.args.get("from")
    if src_id:
        src = storage.get_run(current_app.config["RUNS_DIR"], src_id)
        if src is not None:
            prefill_yaml = storage.load_meta(src).get("scenario_yaml") or ""
    return render_template("runs_new.html",
                           pc_classes=PC_CLASSES,
                           prefill_yaml=prefill_yaml)


@bp.route("/runs", methods=["POST"])
def runs_create():
    yaml_text = request.form.get("scenario", "").strip()
    if not yaml_text:
        return "scenario YAML is required", 400

    # Reject malformed YAML up-front so the run dir is never created --
    # otherwise we'd queue a guaranteed-failed job. Validation lives inside
    # runner._normalize_scenario; replicate the structural checks here.
    try:
        runner._normalize_scenario(yaml_text, Path("/tmp/x.jsonl"))  # noqa: SLF001
    except runner.ScenarioInvalidError as e:
        return f"scenario invalid: {e}", 400

    handle = storage.new_run(current_app.config["RUNS_DIR"])
    jobqueue.submit(
        handle, yaml_text,
        mud_sim_bin=current_app.config["MUD_SIM_BIN"],
        world_dir=current_app.config["MUD_SIM_WORLD_DIR"],
        timeout_s=current_app.config["MUD_SIM_TIMEOUT_S"],
    )
    # Redirect immediately. Viewer polls meta.json status until 'ok' /
    # 'failed' / 'timeout' and re-renders.
    return redirect(url_for("ui.run_view", run_id=handle.run_id))


@bp.route("/runs/<run_id>")
def run_view(run_id: str):
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None:
        abort(404)
    meta = storage.load_meta(handle)
    status = meta.get("status", "unknown")
    if status in ("queued", "running"):
        # Render a waiting page that polls /runs/<id>/status; once the job
        # finishes the page reloads into the full viewer.
        return render_template("run_pending.html", handle=handle, meta=meta)
    return render_template(
        "run_view.html",
        handle=handle,
        meta=meta,
        initial_round=request.args.get("round", "-1"),
        initial_role=request.args.get("role", "attacker"),
    )


@bp.route("/runs/<run_id>/status")
def run_status(run_id: str):
    """Tiny JSON endpoint the pending page polls every second.

    Progress: count `round` events in events.jsonl on the fly (mud-sim
    appends one per pulse_violence). Cheap -- one byte-grep per poll.
    """
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None:
        abort(404)
    meta = storage.load_meta(handle)
    target = int(meta.get("target_rounds") or 0)
    done = 0
    if handle.events_path.is_file():
        with handle.events_path.open("rb") as fh:
            for line in fh:
                # Quick substring check; the field is fixed-position
                # near the start of every event, so no JSON parse needed.
                if b'"name":"round"' in line:
                    done += 1
    started = meta.get("started_at")
    elapsed = max(0, int(_time.time() - started)) if isinstance(started, (int, float)) else 0
    return jsonify({
        "status": meta.get("status", "unknown"),
        "queued_at": meta.get("queued_at"),
        "started_at": started,
        "finished_at": meta.get("finished_at"),
        "elapsed_s": elapsed,
        "rounds_done": done,
        "rounds_target": target,
    })


@bp.route("/runs/<run_id>/api/analytics")
def api_analytics(run_id: str):
    """Per-role per-round aggregates for the analytics panel charts.

    Симулятор эмитит damage и miss события с attacker_name/victim_name.
    Чтобы сопоставить имя с «ролью» (attacker / victim / attacker_pet_N /
    victim_pet_N) сначала проходимся по char_state'ам -- они эмитятся в
    самом начале и содержат связку target_name → role.

    Возвращаем один объект `roles[role]` с массивами длины rounds:
      dealt_melee[], dealt_spell[]   — исходящий урон (real_dam) этой роли
      pets_dealt[]                   — исходящий урон петов этой роли (для
                                       мастера); у самого пета остаётся в
                                       dealt_melee/dealt_spell
      taken[]                        — входящий урон (real_dam, что прошло)
      absorbed[]                     — поглощённый щитом/уменьшениями
                                       (dam - real_dam)
      hits_dealt[], misses_dealt[]   — # попаданий / # промахов (отдал)
      hits_taken[], misses_taken[]   — # попаданий / # промахов (принял)
      crits_dealt[]                  — # критов (отдал)
    А также total -- агрегаты на весь бой.
    """
    handle = storage.get_run(current_app.config["RUNS_DIR"], run_id)
    if handle is None:
        abort(404)
    meta = storage.load_meta(handle)
    round_ts = meta.get("round_ts") or []
    rounds = len(round_ts)
    if not rounds:
        return jsonify({"labels": [], "rounds": 0, "roles": {}})

    def round_for_ts(ts: int) -> int:
        for i, r_ts in enumerate(round_ts):
            if ts <= r_ts:
                return i
        return rounds - 1

    name_to_role: dict[str, str] = {}
    role_owner: dict[str, str] = {}  # pet role -> master role
    for ev in _iter_events(handle.events_path):
        if ev.get("name") != "char_state":
            continue
        n = ev.get("target_name") or ""
        r = ev.get("role") or ""
        if n and r and n not in name_to_role:
            name_to_role[n] = r
        if r.startswith("attacker_pet"):
            role_owner[r] = "attacker"
        elif r.startswith("victim_pet"):
            role_owner[r] = "victim"

    if not name_to_role:
        return jsonify({"labels": [], "rounds": 0, "roles": {}})

    def zeros() -> list[int]:
        return [0] * rounds

    metric_keys = (
        "dealt_melee", "dealt_spell", "pets_dealt",
        "taken", "absorbed",
        "hits_dealt", "misses_dealt",
        "hits_taken", "misses_taken",
        "crits_dealt",
    )
    roles = sorted(set(name_to_role.values()))
    data: dict[str, dict[str, list[int]]] = {
        role: {k: zeros() for k in metric_keys} for role in roles
    }

    for ev in _iter_events(handle.events_path):
        name = ev.get("name")
        ts = int(ev.get("ts", 0))
        if name == "damage":
            att = ev.get("attacker_name") or ""
            vict = ev.get("victim_name") or ""
            att_role = name_to_role.get(att)
            vict_role = name_to_role.get(vict)
            r = round_for_ts(ts)
            if not 0 <= r < rounds:
                continue
            real_dam = int(ev.get("real_dam", 0))
            dam = int(ev.get("dam", 0))
            absorbed_n = max(0, dam - real_dam)
            is_spell = int(ev.get("spell_id", 0)) != 0
            is_charmie = bool(ev.get("attacker_is_charmie"))
            if att_role and att_role in data:
                row = data[att_role]
                row["hits_dealt"][r] += 1
                if ev.get("crit"):
                    row["crits_dealt"][r] += 1
                if is_spell:
                    row["dealt_spell"][r] += real_dam
                else:
                    row["dealt_melee"][r] += real_dam
                # Урон пета поднимаем в строку мастера в pets_dealt --
                # чтобы график «исходящий» у роли attacker показывал и
                # его собственные удары, и удары его свиты.
                if is_charmie:
                    owner = role_owner.get(att_role)
                    if owner and owner in data:
                        data[owner]["pets_dealt"][r] += real_dam
            if vict_role and vict_role in data:
                row = data[vict_role]
                row["taken"][r] += real_dam
                row["absorbed"][r] += absorbed_n
                row["hits_taken"][r] += 1
        elif name == "miss":
            att = ev.get("attacker_name") or ""
            vict = ev.get("victim_name") or ""
            att_role = name_to_role.get(att)
            vict_role = name_to_role.get(vict)
            r = round_for_ts(ts)
            if not 0 <= r < rounds:
                continue
            if att_role and att_role in data:
                data[att_role]["misses_dealt"][r] += 1
            if vict_role and vict_role in data:
                data[vict_role]["misses_taken"][r] += 1

    # Totals + cumulative для удобства фронта.
    out_roles: dict[str, dict] = {}
    for role, m in data.items():
        totals = {k: sum(v) for k, v in m.items()}
        out_roles[role] = {
            **m,
            "totals": totals,
        }

    return jsonify({
        "labels": [f"R{i}" for i in range(rounds)],
        "rounds": rounds,
        "roles": out_roles,
    })


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
        {
            "vnum": o.vnum,
            "name": o.name,
            "type": o.obj_type,
            "wear_flags": list(o.wear_flags),
            "applies": [
                {"location": ap.location, "name": ap.location_name, "modifier": ap.modifier}
                for ap in o.applies
            ],
            "affect_flags": [
                {"key": f, "label": world.AFFECT_FLAG_LABELS.get(f, f)}
                for f in o.affect_flags
            ],
            "summary": world.obj_summary(o),
            "min_level": o.min_level,
            "min_remort": o.min_remort,
        }
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


