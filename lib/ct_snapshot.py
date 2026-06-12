"""ct_snapshot — shared snapshot store for connectTools.

Snapshots are stored at ~/.connecttools/snapshot_<instance-id>.json.
Other tools import this module to resolve IDs/ARNs to names without
making live API calls.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

SNAPSHOT_DIR    = Path.home() / ".connecttools"
STALE_THRESHOLD = 24.0   # hours before a warning is shown


def snapshot_path(instance_id: str) -> Path:
    return SNAPSHOT_DIR / f"snapshot_{instance_id}.json"


def load(instance_id: str) -> dict | None:
    """Load the snapshot for instance_id. Returns None if not found or unreadable."""
    path = snapshot_path(instance_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save(instance_id: str, data: dict) -> Path:
    """Write snapshot data to disk. Returns the file path."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(instance_id)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def age_hours(snapshot: dict) -> float:
    """Hours since the snapshot was fetched. Returns inf if timestamp missing."""
    fetched = snapshot.get("fetched_at")
    if not fetched:
        return float("inf")
    try:
        ts = dt.datetime.fromisoformat(fetched)
        return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600
    except ValueError:
        return float("inf")


def warn_if_stale(snapshot: dict, threshold_hours: float = STALE_THRESHOLD) -> None:
    """Print a stderr warning if the snapshot is older than threshold_hours."""
    age = age_hours(snapshot)
    if age >= threshold_hours:
        path = snapshot_path(snapshot.get("instance_id", "?"))
        print(
            f"  \033[33mWarning: instance snapshot is {int(age)}h old "
            f"— run instance_snapshot.py to refresh.\033[0m",
            file=sys.stderr,
        )


def resolve(snapshot: dict, resource_type: str, id_or_arn: str) -> str | None:
    """
    Look up a human-readable name for a resource ID or ARN.

    resource_type must match a top-level key in the snapshot
    (e.g. 'queues', 'flows', 'users', 'routing_profiles').

    For users, returns the username. For all other types, returns name.
    Falls back to None if not found.
    """
    if not id_or_arn or not snapshot:
        return None

    resources = snapshot.get(resource_type) or {}

    # Direct ID lookup
    item = resources.get(id_or_arn)
    if item:
        return item.get("username") or item.get("name")

    # ARN → extract last path segment (Connect ARNs end with /resource-type/id)
    if "/" in id_or_arn:
        extracted = id_or_arn.rstrip("/").split("/")[-1]
        item = resources.get(extracted)
        if item:
            return item.get("username") or item.get("name")

    return None


def search(snapshot: dict, resource_type: str, name_fragment: str) -> list:
    """
    Return all resources of resource_type whose name contains name_fragment
    (case-insensitive). Each result is the stored resource dict.
    """
    resources = snapshot.get(resource_type) or {}
    needle = name_fragment.lower()
    return [
        item for item in resources.values()
        if needle in (item.get("name") or item.get("username") or "").lower()
    ]


def counts(snapshot: dict) -> dict:
    """Return {resource_type: count} for all resource types in the snapshot."""
    skip = {"instance_id", "instance_alias", "fetched_at", "region"}
    return {k: len(v) for k, v in snapshot.items() if k not in skip and isinstance(v, dict)}


# ── Per-tool output directories ────────────────────────────────────────────────

def output_dir(tool_name: str) -> Path:
    """Return (and create if needed) ~/.connecttools/<tool_name>/."""
    d = SNAPSHOT_DIR / tool_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_path(tool_name: str, filename: str) -> Path:
    """Resolve an output file path.

    If filename has no directory component (just a bare name), places it in
    ~/.connecttools/<tool_name>/. Otherwise expands ~ and returns as-is,
    so explicit paths like ~/my-dir/file.csv or /tmp/file.csv are honoured.
    """
    p = Path(filename).expanduser()
    if p.parent == Path("."):
        return output_dir(tool_name) / p.name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
