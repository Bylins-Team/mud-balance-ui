"""Aggregate meta.json from a run's events.jsonl.

Computed once after the run finishes; the list page reads meta.json
without touching the (potentially large) events.jsonl. Viewer reads
events.jsonl directly via the API.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def summarize(events_path: Path, scenario_yaml_utf8: str) -> dict:
    """Walk events.jsonl once, produce a small summary dict for the card."""
    total = 0
    damage_sum = 0
    damage_count = 0
    miss_count = 0
    rounds = 0
    attacker_label: str | None = None
    victim_label: str | None = None
    first_ts: int | None = None
    last_ts: int | None = None

    if events_path.is_file():
        with events_path.open() as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                ts = ev.get("ts")
                if isinstance(ts, int):
                    first_ts = ts if first_ts is None else first_ts
                    last_ts = ts
                name = ev.get("name")
                if name == "damage":
                    damage_sum += int(ev.get("dam", 0))
                    damage_count += 1
                elif name == "miss":
                    miss_count += 1
                elif name == "round":
                    rounds += 1
                    if attacker_label is None:
                        attacker_label = ev.get("attacker_class") or (
                            f"mob#{ev.get('attacker_vnum')}"
                            if ev.get("attacker_vnum") is not None
                            else None
                        )
                        victim_label = ev.get("victim_class") or (
                            f"mob#{ev.get('victim_vnum')}"
                            if ev.get("victim_vnum") is not None
                            else None
                        )

    dpr = damage_sum / rounds if rounds else 0.0
    swings = damage_count + miss_count
    hit_rate = 100.0 * damage_count / swings if swings else 0.0

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scenario_yaml": scenario_yaml_utf8,
        "events_total": total,
        "rounds": rounds,
        "dpr": round(dpr, 2),
        "hit_rate_pct": round(hit_rate, 1),
        "attacker": attacker_label or "?",
        "victim": victim_label or "?",
        "first_ts_ms": first_ts,
        "last_ts_ms": last_ts,
    }
