"""Subprocess wrapper around mud-sim.

mud-sim reads its scenario YAML in KOI8-R (engine encoding) and writes
events.jsonl in UTF-8. We accept UTF-8 YAML from the form, convert it
to KOI8-R via iconv before invoking mud-sim, and rewrite the `output:`
field to point at our run directory.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

from .storage import RunHandle


class ScenarioInvalidError(ValueError):
    """The submitted YAML is not a valid scenario the runner can use."""


def _normalize_scenario(yaml_text: str, events_path: Path) -> str:
    """Validate YAML structurally and force the `output:` field to events_path.

    We don't want users to write `output: /tmp/whatever` and have mud-sim
    happily write outside our run directory. So we always rewrite it.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    try:
        data = yaml.load(yaml_text)
    except Exception as e:  # noqa: BLE001 -- ruamel raises a zoo of exceptions
        raise ScenarioInvalidError(f"YAML parse error: {e}") from e

    if not isinstance(data, dict):
        raise ScenarioInvalidError("scenario must be a YAML mapping at the top level")
    if "attacker" not in data:
        raise ScenarioInvalidError("scenario.attacker is required")
    if "victim" not in data:
        raise ScenarioInvalidError("scenario.victim is required")

    data["output"] = str(events_path)

    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue()


def run_scenario(
    yaml_text_utf8: str,
    handle: RunHandle,
    *,
    mud_sim_bin: str,
    world_dir: str,
    timeout_s: int,
) -> tuple[bool, str]:
    """Run mud-sim for one scenario.

    Writes scenario.yaml (KOI8-R) and stderr.log into handle.root, lets
    mud-sim populate events.jsonl. Returns (ok, stderr_text). On failure
    the exception is *not* raised -- the run dir is left for inspection
    and the caller marks status='failed' in meta.json.
    """
    normalized = _normalize_scenario(yaml_text_utf8, handle.events_path)

    # mud-sim wants KOI8-R. Convert in Python so we don't shell out to iconv.
    handle.scenario_path.write_bytes(normalized.encode("koi8-r"))

    # Run mud-sim with cwd=run_dir so its `syslog`, `log/errlog.txt`, etc.
    # land in the per-run directory (writable by the container user) instead
    # of /app (root-owned, container-user can't write there).
    log_dir = handle.root / "log"
    log_dir.mkdir(exist_ok=True)
    try:
        result = subprocess.run(  # noqa: S603 -- args are not user-controlled
            [mud_sim_bin, "--config", str(handle.scenario_path), "-d", world_dir],
            capture_output=True,
            timeout=timeout_s,
            cwd=str(handle.root),
        )
    except subprocess.TimeoutExpired as e:
        msg = f"mud-sim timed out after {timeout_s}s\n"
        if e.stderr:
            msg += e.stderr.decode("utf-8", errors="replace")
        handle.stderr_path.write_text(msg)
        return False, msg

    stderr_text = result.stderr.decode("utf-8", errors="replace")
    handle.stderr_path.write_text(stderr_text)
    return result.returncode == 0, stderr_text
