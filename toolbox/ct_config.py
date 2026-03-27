#!/usr/bin/env python3
"""ct_config — shared configuration store for connectTools."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_FILE  = Path.home() / ".connecttools" / "config.json"
# Root of the connectTools repo (parent of the toolbox/ directory)
TOOLBOX_ROOT = Path(__file__).parent.parent


def output_dir(tool_name: str) -> Path:
    """Return (and create) the output directory for a tool.

    Converts tool_name to PascalCase folder under TOOLBOX_ROOT.
    e.g. 'contact_logs' → <connectTools>/ContactLogs/
    """
    folder = "".join(w.capitalize() for w in tool_name.replace(".py", "").split("_"))
    d = TOOLBOX_ROOT / folder
    d.mkdir(parents=True, exist_ok=True)
    return d

# Ordered list of (key, display_label) for all configurable fields
FIELDS = [
    ("instance_id", "Instance ID"),
    ("region",      "Region"),
    ("profile",     "AWS Profile"),
    ("account_id",  "Account ID"),
]


def get_log_group(instance_id: str) -> str:
    """Return the saved log group for this instance, or empty string."""
    return load().get("log_groups", {}).get(instance_id, "")


def set_log_group(data: dict, instance_id: str, log_group: str) -> None:
    """Write log_group into data dict under log_groups[instance_id] and save."""
    data.setdefault("log_groups", {})[instance_id] = log_group
    save(data)


def load() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save(data: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
