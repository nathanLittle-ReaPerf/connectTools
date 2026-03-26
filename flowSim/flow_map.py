#!/usr/bin/env python3
"""flow_map.py — Build a complete attribute/decision map from all flows in an Amazon Connect
instance. Caches flow JSON locally (~/.connecttools/flows/<instance-id>/); refreshes if older
than 60 days. Outputs a JSON map, a scenario template, and an HTML report."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})
CACHE_BASE   = Path.home() / ".connecttools" / "flows"
STALE_DAYS   = 60

_MAN = """\
NAME
    flow_map.py — Attribute and decision map across all flows in a Connect instance

SYNOPSIS
    python flow_map.py --instance-id UUID [OPTIONS]

DESCRIPTION
    Scans every contact flow in an Amazon Connect instance and builds a complete
    map of:
      - Every contact attribute SET, CHECKed, or REFerenced, and in which flows
      - Every Lambda function invoked, and which flows invoke it
      - Every DTMF input block with its valid options
      - Every Hours of Operation and queue staffing check

    Flow JSON is cached locally at ~/.connecttools/flows/<instance-id>/ and
    reused on subsequent runs. The cache is automatically refreshed when older
    than 60 days, or when --force-refresh is passed.

    Outputs:
      - HTML report (browsable attribute/lambda reference)
      - Scenario template JSON (pre-filled for use with flow_sim.py)
      - Full JSON map (--map)

OPTIONS
    --instance-id UUID
        Amazon Connect instance UUID. Required.

    --region REGION
        AWS region. Required if cache is missing or stale.

    --profile NAME
        AWS named profile for local development.

    --force-refresh
        Ignore the local cache and re-fetch all flows from the instance.

    --html FILE
        Path for the HTML report. Default: flow_map_<instance-id>.html

    --scenario FILE
        Path for the scenario template. Default: scenario_<instance-id>.json

    --map FILE
        Save the full JSON map to FILE.

    --no-html
        Skip HTML generation.

    --no-scenario
        Skip scenario template generation.

EXAMPLES
    # First run: fetch all flows and generate outputs
    python flow_map.py --instance-id <UUID> --region us-east-1

    # Subsequent runs: use cache, regenerate outputs
    python flow_map.py --instance-id <UUID>

    # Force re-fetch even if cache is fresh
    python flow_map.py --instance-id <UUID> --region us-east-1 --force-refresh

    # Save JSON map too
    python flow_map.py --instance-id <UUID> --map map.json

IAM PERMISSIONS (only needed when fetching)
    connect:ListContactFlows
    connect:DescribeContactFlow
"""


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_dir(instance_id: str) -> Path:
    return CACHE_BASE / instance_id


def _manifest_path(instance_id: str) -> Path:
    return _cache_dir(instance_id) / "manifest.json"


def _load_manifest(instance_id: str) -> dict | None:
    p = _manifest_path(instance_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _cache_age_days(manifest: dict) -> float:
    fetched = dt.datetime.fromisoformat(manifest["fetched_at"])
    return (dt.datetime.now(dt.timezone.utc) - fetched).total_seconds() / 86400


def _is_stale(manifest: dict) -> bool:
    return _cache_age_days(manifest) >= STALE_DAYS


def _load_cached_flows(instance_id: str) -> list[dict] | None:
    d = _cache_dir(instance_id)
    flows = []
    for p in sorted(d.glob("*.json")):
        if p.name == "manifest.json":
            continue
        try:
            flows.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return flows if flows else None


def _save_cache(instance_id: str, envelopes: list[dict]) -> None:
    d = _cache_dir(instance_id)
    d.mkdir(parents=True, exist_ok=True)
    # Remove stale flow files
    for p in d.glob("*.json"):
        if p.name != "manifest.json":
            p.unlink(missing_ok=True)
    # Write each flow
    for env in envelopes:
        flow_id = env["metadata"]["id"]
        (d / f"{flow_id}.json").write_text(json.dumps(env, indent=2))
    # Write manifest
    manifest = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "flow_count": len(envelopes),
        "instance_id": instance_id,
    }
    _manifest_path(instance_id).write_text(json.dumps(manifest, indent=2))


# ── AWS fetch ─────────────────────────────────────────────────────────────────

def _make_client(region, profile):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        print("Error: could not determine AWS region. Pass --region explicitly.", file=sys.stderr)
        sys.exit(1)
    return session.client("connect", region_name=resolved, config=RETRY_CONFIG)


def _list_flows(client, instance_id: str) -> list[dict]:
    flows, token = [], None
    while True:
        kwargs = {"InstanceId": instance_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = client.list_contact_flows(**kwargs)
        except ClientError as e:
            print(f"Error listing flows: {e.response['Error']['Message']}", file=sys.stderr)
            sys.exit(1)
        flows.extend(resp.get("ContactFlowSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            return flows


def _describe_flow(client, instance_id: str, flow_id: str) -> dict | None:
    try:
        raw     = client.describe_contact_flow(InstanceId=instance_id, ContactFlowId=flow_id)["ContactFlow"]
        content = raw.get("Content") or "{}"
        if isinstance(content, str):
            content = json.loads(content)
        return {
            "metadata": {
                "id":     raw["Id"],
                "arn":    raw.get("Arn", ""),
                "name":   raw.get("Name", ""),
                "type":   raw.get("Type", ""),
                "status": raw.get("Status", ""),
            },
            "content": content,
        }
    except (ClientError, json.JSONDecodeError, KeyError):
        return None


def fetch_and_cache(client, instance_id: str) -> list[dict]:
    print("  Listing flows…", file=sys.stderr)
    summaries = _list_flows(client, instance_id)
    print(f"  Fetching {len(summaries)} flow(s)…", file=sys.stderr)
    envelopes = []
    for i, s in enumerate(summaries, 1):
        print(f"  [{i}/{len(summaries)}] {s['Name']}", file=sys.stderr)
        env = _describe_flow(client, instance_id, s["Id"])
        if env:
            envelopes.append(env)
    _save_cache(instance_id, envelopes)
    print(f"  Cached {len(envelopes)} flows → {_cache_dir(instance_id)}", file=sys.stderr)
    return envelopes


# ── Block scanning ─────────────────────────────────────────────────────────────

_ATTR_REF     = re.compile(r'\$\.Attributes\.([a-zA-Z0-9_]+)')
_EXTERNAL_REF = re.compile(r'\$\.External\.([a-zA-Z0-9_]+)')


def _find_refs(value, pattern: re.Pattern, path: str) -> list[tuple[str, str]]:
    """Recursively find all pattern matches. Returns (path, captured_group_1) pairs."""
    out = []
    if isinstance(value, str):
        for m in pattern.finditer(value):
            out.append((path, m.group(1)))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(_find_refs(v, pattern, f"{path}.{k}"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(_find_refs(v, pattern, f"{path}[{i}]"))
    return out


def _classify_value(val) -> str:
    if not isinstance(val, str):
        return "literal"
    if _ATTR_REF.search(val):
        return "attribute_ref"
    if _EXTERNAL_REF.search(val):
        return "lambda_result"
    if val.startswith("$."):
        return "expression"
    return "literal"


def _block_label(action: dict) -> str:
    ident = action.get("Identifier", "?")
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}", ident.lower()):
        return ident[:8] + "…"
    return ident[:60]


def scan_flow(envelope: dict) -> dict:
    """Scan one flow envelope and return its structured map data."""
    meta    = envelope.get("metadata") or {}
    content = envelope.get("content") or {}

    attrs:           dict[str, dict] = {}
    lambdas:         list[dict]      = []
    dtmf_blocks:     list[dict]      = []
    hours_checks:    list[dict]      = []
    staffing_checks: list[dict]      = []
    flow_transfers:  list[dict]      = []
    external_keys:   set[str]        = set()

    def _attr_entry(key: str) -> dict:
        if key not in attrs:
            attrs[key] = {"set": [], "checked": [], "referenced": []}
        return attrs[key]

    for action in content.get("Actions") or []:
        btype  = action.get("Type", "")
        blabel = _block_label(action)
        params = action.get("Parameters") or {}
        trans  = action.get("Transitions") or {}
        conds  = trans.get("Conditions") or []

        # ── SET ───────────────────────────────────────────────────────────────
        if btype == "UpdateContactAttributes":
            for key, val in (params.get("Attributes") or {}).items():
                vtype = _classify_value(val)
                _attr_entry(key)["set"].append({
                    "block":      blabel,
                    "value":      str(val)[:80] if val is not None else "",
                    "value_type": vtype,
                })
                for _, ext_key in _find_refs(val, _EXTERNAL_REF, ""):
                    external_keys.add(ext_key)
            # Scan values for $.Attributes.* reads
            for _, ref_name in _find_refs(params.get("Attributes") or {}, _ATTR_REF, ""):
                entry = _attr_entry(ref_name)["referenced"]
                rec = {"block": blabel, "context": "used as SET value"}
                if rec not in entry:
                    entry.append(rec)
            continue

        # ── CHECK ─────────────────────────────────────────────────────────────
        if btype == "Compare":
            cmp_val = str(params.get("ComparisonValue") or "")
            m = _ATTR_REF.search(cmp_val)
            if m:
                key = m.group(1)
                operands = [
                    str(op)
                    for c in conds
                    for op in ((c.get("Condition") or {}).get("Operands") or [])
                ]
                _attr_entry(key)["checked"].append({"block": blabel, "comparisons": operands})
            continue

        # ── Lambda ────────────────────────────────────────────────────────────
        if btype == "InvokeExternalResource":
            arn = (
                params.get("FunctionArn")
                or params.get("ResourceId")
                or params.get("LambdaFunctionARN")
                or ""
            )
            lambdas.append({"arn": arn, "block": blabel})
            for _, ref_name in _find_refs(params, _ATTR_REF, ""):
                entry = _attr_entry(ref_name)["referenced"]
                rec = {"block": blabel, "context": "Lambda input"}
                if rec not in entry:
                    entry.append(rec)
            continue

        # ── DTMF / voice input ────────────────────────────────────────────────
        if btype == "GetUserInput":
            options = sorted({
                str(op)
                for c in conds
                for op in ((c.get("Condition") or {}).get("Operands") or [])
                if (c.get("Condition") or {}).get("Operator") == "Equals"
            })
            dtmf_blocks.append({"block": blabel, "options": options})
            continue

        # ── Hours of operation ────────────────────────────────────────────────
        if btype == "CheckHoursOfOperation":
            hoo_id = params.get("HoursOfOperationId") or ""
            hours_checks.append({"block": blabel, "resource_id": hoo_id})
            continue

        # ── Queue staffing ────────────────────────────────────────────────────
        if btype == "CheckStaffing":
            q_id = params.get("QueueId") or ""
            staffing_checks.append({"block": blabel, "queue_id": q_id})
            continue

        # ── Flow transfer ─────────────────────────────────────────────────────
        if btype in ("TransferContactToFlow", "TransferToFlow"):
            target_id = params.get("ContactFlowId") or ""
            flow_transfers.append({"block": blabel, "target_id": target_id})
            continue

        # ── General REF scan ──────────────────────────────────────────────────
        for path, ref_name in _find_refs(params, _ATTR_REF, "Parameters"):
            entry = _attr_entry(ref_name)["referenced"]
            rec = {"block": blabel, "context": path}
            if rec not in entry:
                entry.append(rec)

    return {
        "id":               meta.get("id", ""),
        "name":             meta.get("name", ""),
        "type":             meta.get("type", ""),
        "attributes":       attrs,
        "lambdas":          lambdas,
        "dtmf_blocks":      dtmf_blocks,
        "hours_checks":     hours_checks,
        "staffing_checks":  staffing_checks,
        "flow_transfers":   flow_transfers,
        "external_keys":    sorted(external_keys),
    }


# ── Map building ───────────────────────────────────────────────────────────────

def build_map(instance_id: str, envelopes: list[dict]) -> dict:
    flows_data = [scan_flow(e) for e in envelopes]

    all_attrs:   dict[str, dict] = {}
    all_lambdas: dict[str, list] = {}

    for fd in flows_data:
        fname = fd["name"]

        for key, usage in fd["attributes"].items():
            if key not in all_attrs:
                all_attrs[key] = {"set_in": [], "checked_in": [], "referenced_in": []}
            a = all_attrs[key]
            if usage["set"]:
                a["set_in"].append({"flow": fname, "occurrences": usage["set"]})
            if usage["checked"]:
                a["checked_in"].append({"flow": fname, "occurrences": usage["checked"]})
            if usage["referenced"]:
                a["referenced_in"].append({"flow": fname, "occurrences": usage["referenced"]})

        for lam in fd["lambdas"]:
            arn = lam["arn"]
            if arn:
                if arn not in all_lambdas:
                    all_lambdas[arn] = []
                if fname not in all_lambdas[arn]:
                    all_lambdas[arn].append(fname)

    return {
        "instance_id":  instance_id,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "flow_count":   len(flows_data),
        "flows":        flows_data,
        "attributes":   all_attrs,
        "lambdas":      all_lambdas,
    }


# ── Scenario template ──────────────────────────────────────────────────────────

def build_scenario_template(map_data: dict) -> dict:
    """Generate a scenario template from the map, ready to fill in for flow_sim.py."""
    # Attributes: include anything set or checked; add a hint with known comparison values
    attrs        = {}
    attr_hints   = {}
    for key in sorted(map_data["attributes"].keys()):
        usage = map_data["attributes"][key]
        if not usage["set_in"] and not usage["checked_in"]:
            continue
        attrs[key] = ""
        examples = [
            op
            for entry in usage["checked_in"]
            for occ in entry["occurrences"]
            for op in occ.get("comparisons", [])
        ]
        if examples:
            attr_hints[key] = "valid values seen in flows: " + ", ".join(sorted(set(examples)))

    # Lambda mocks — one entry per unique ARN
    lambda_mocks = {}
    for arn in sorted(map_data["lambdas"].keys()):
        fn_name = arn.split(":")[-1] if ":" in arn else arn
        # Collect external keys referenced after this Lambda across all flows
        ext_keys: set[str] = set()
        for fd in map_data["flows"]:
            if any(lam["arn"] == arn for lam in fd["lambdas"]):
                ext_keys.update(fd.get("external_keys", []))
        lambda_mocks[fn_name] = {
            "_arn":      arn,
            "result":    "Success",
            "attributes": {k: "" for k in sorted(ext_keys)},
        }

    # DTMF inputs — keyed as "Flow / Block"
    dtmf = {}
    for fd in map_data["flows"]:
        for db in fd["dtmf_blocks"]:
            key = f"{fd['name']} / {db['block']}"
            dtmf[key] = {
                "options": db["options"],
                "value":   db["options"][0] if db["options"] else "",
            }

    # Hours mocks — keyed by resource ID (resolve with snapshot if available)
    seen_hoo: set[str] = set()
    hours = {}
    for fd in map_data["flows"]:
        for hc in fd["hours_checks"]:
            rid = hc["resource_id"]
            if rid and rid not in seen_hoo:
                seen_hoo.add(rid)
                hours[rid] = {"in_hours": True, "_block": hc["block"], "_flow": fd["name"]}

    # Staffing mocks
    seen_q: set[str] = set()
    staffing = {}
    for fd in map_data["flows"]:
        for sc in fd["staffing_checks"]:
            qid = sc["queue_id"]
            if qid and qid not in seen_q:
                seen_q.add(qid)
                staffing[qid] = {"staffed": True, "_block": sc["block"], "_flow": fd["name"]}

    return {
        "_note":         "Generated by flow_map.py. Fill in values, then pass to flow_sim.py.",
        "_attr_hints":   attr_hints,
        "call_parameters": {
            "ani":               None,
            "dnis":              None,
            "channel":           "VOICE",
            "initiation_method": "INBOUND",
            "simulated_time":    dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "attributes":    attrs,
        "lambda_mocks":  lambda_mocks,
        "dtmf_inputs":   dtmf,
        "hours_mocks":   hours,
        "staffing_mocks": staffing,
    }


# ── HTML report ────────────────────────────────────────────────────────────────

def _he(s: str) -> str:
    """HTML-escape a string."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_html(map_data: dict) -> str:
    attrs       = map_data["attributes"]
    lambdas     = map_data["lambdas"]
    instance_id = map_data.get("instance_id", "")
    gen_time    = map_data.get("generated_at", "")[:19].replace("T", " ")

    # ── Attribute rows ─────────────────────────────────────────────────────────
    attr_rows = []
    for key in sorted(attrs.keys()):
        usage      = attrs[key]
        set_count  = len(usage["set_in"])
        chk_count  = len(usage["checked_in"])
        ref_count  = len(usage["referenced_in"])

        badges = ""
        if set_count:
            badges += f'<span class="badge bset">SET {set_count}</span>'
        if chk_count:
            badges += f'<span class="badge bchk">CHK {chk_count}</span>'
        if ref_count:
            badges += f'<span class="badge bref">REF {ref_count}</span>'

        # Detail lines
        lines = []
        if usage["set_in"]:
            lines.append('<span class="sec-head">SET in:</span>')
            for entry in usage["set_in"]:
                for occ in entry["occurrences"]:
                    vt = occ.get("value_type", "")
                    v  = occ.get("value", "")
                    lines.append(
                        f'  <span class="flow-name">{_he(entry["flow"])}</span>'
                        f' &rarr; <span class="block-name">{_he(occ["block"])}</span>'
                        f' = <span class="val">{_he(v)}</span>'
                        f' <span class="vtype">({_he(vt)})</span>'
                    )
        if usage["checked_in"]:
            lines.append('<span class="sec-head">CHECKED in:</span>')
            for entry in usage["checked_in"]:
                for occ in entry["occurrences"]:
                    comps = ", ".join(_he(c) for c in occ.get("comparisons", []))
                    lines.append(
                        f'  <span class="flow-name">{_he(entry["flow"])}</span>'
                        f' &rarr; <span class="block-name">{_he(occ["block"])}</span>'
                        f' against: <span class="val">{comps}</span>'
                    )
        if usage["referenced_in"]:
            lines.append('<span class="sec-head">REFERENCED in:</span>')
            for entry in usage["referenced_in"]:
                for occ in entry["occurrences"]:
                    lines.append(
                        f'  <span class="flow-name">{_he(entry["flow"])}</span>'
                        f' &rarr; <span class="block-name">{_he(occ["block"])}</span>'
                        f' ({_he(occ.get("context", ""))})'
                    )

        detail = "<br>".join(lines)
        uid = re.sub(r"[^a-zA-Z0-9]", "_", key)
        attr_rows.append(f"""\
      <tr class="hdr-row" onclick="toggle('{uid}')">
        <td class="attr-key">{_he(key)}</td>
        <td>{badges}</td>
      </tr>
      <tr class="det-row" id="d_{uid}">
        <td colspan="2"><div class="detail">{detail}</div></td>
      </tr>""")

    # ── Lambda rows ────────────────────────────────────────────────────────────
    lambda_rows = []
    for arn in sorted(lambdas.keys()):
        fn_name = arn.split(":")[-1] if ":" in arn else arn
        flows   = ", ".join(_he(f) for f in lambdas[arn])
        lambda_rows.append(f"""\
      <tr>
        <td class="mono">{_he(fn_name)}</td>
        <td class="mono small">{_he(arn)}</td>
        <td>{flows}</td>
      </tr>""")

    attr_body   = "\n".join(attr_rows)   or '<tr><td colspan="2" class="dim">None found.</td></tr>'
    lambda_body = "\n".join(lambda_rows) or '<tr><td colspan="3" class="dim">None found.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Flow Map &mdash; {_he(instance_id)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; background: #111827; color: #d1d5db; }}
  .header {{ background: #1f2937; padding: 18px 28px; border-bottom: 1px solid #374151; }}
  h1 {{ margin: 0; font-size: 1.25em; color: #93c5fd; }}
  .meta {{ color: #6b7280; font-size: 0.82em; margin-top: 4px; }}
  .stats {{ display: flex; gap: 16px; margin-top: 14px; flex-wrap: wrap; }}
  .stat {{ background: #1e3a5f; border-radius: 8px; padding: 10px 20px; min-width: 100px; }}
  .stat-n {{ font-size: 1.6em; font-weight: 700; color: #60a5fa; }}
  .stat-l {{ font-size: 0.78em; color: #9ca3af; }}
  .content {{ padding: 28px; }}
  h2 {{ color: #93c5fd; border-bottom: 1px solid #374151; padding-bottom: 6px; margin-top: 36px; }}
  h2:first-child {{ margin-top: 0; }}
  .hint {{ color: #6b7280; font-size: 0.82em; margin: -8px 0 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88em; }}
  th {{ background: #1e3a5f; color: #93c5fd; padding: 9px 14px; text-align: left; font-weight: 600; }}
  td {{ padding: 8px 14px; border-bottom: 1px solid #1f2937; vertical-align: top; }}
  tr.hdr-row {{ cursor: pointer; transition: background 0.1s; }}
  tr.hdr-row:hover {{ background: #1e2d40; }}
  tr.det-row {{ display: none; }}
  tr.det-row.open {{ display: table-row; }}
  .detail {{ background: #0d1117; padding: 12px 18px; border-radius: 4px; font-size: 0.84em; line-height: 1.8; }}
  .attr-key {{ font-family: monospace; font-weight: 600; color: #f3f4f6; }}
  .badge {{ display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 0.78em; font-weight: 700; margin-right: 4px; }}
  .bset {{ background: #14532d; color: #4ade80; }}
  .bchk {{ background: #451a03; color: #fb923c; }}
  .bref {{ background: #0c2a3a; color: #38bdf8; }}
  .sec-head {{ color: #6b7280; font-size: 0.82em; display: block; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .flow-name {{ color: #a78bfa; }}
  .block-name {{ color: #34d399; font-family: monospace; }}
  .val {{ color: #fbbf24; font-family: monospace; }}
  .vtype {{ color: #6b7280; font-size: 0.85em; }}
  .mono {{ font-family: monospace; }}
  .small {{ font-size: 0.78em; color: #6b7280; word-break: break-all; }}
  .dim {{ color: #4b5563; font-style: italic; }}
</style>
</head>
<body>
<div class="header">
  <h1>Flow Map</h1>
  <div class="meta">Instance: {_he(instance_id)} &nbsp;&bull;&nbsp; Generated: {_he(gen_time)} UTC</div>
  <div class="stats">
    <div class="stat"><div class="stat-n">{map_data.get("flow_count", 0)}</div><div class="stat-l">Flows scanned</div></div>
    <div class="stat"><div class="stat-n">{len(attrs)}</div><div class="stat-l">Attributes</div></div>
    <div class="stat"><div class="stat-n">{len(lambdas)}</div><div class="stat-l">Lambda functions</div></div>
  </div>
</div>
<div class="content">
  <h2>Attributes</h2>
  <p class="hint">Click a row to expand. SET = attribute is assigned. CHK = attribute is evaluated in a condition. REF = attribute value is read elsewhere.</p>
  <table>
    <thead><tr><th>Attribute</th><th>Usage</th></tr></thead>
    <tbody>
{attr_body}
    </tbody>
  </table>

  <h2>Lambda Functions</h2>
  <table>
    <thead><tr><th>Function name</th><th>ARN</th><th>Invoked in</th></tr></thead>
    <tbody>
{lambda_body}
    </tbody>
  </table>
</div>
<script>
function toggle(uid) {{
  const row = document.getElementById('d_' + uid);
  if (row) row.classList.toggle('open');
}}
</script>
</body>
</html>"""


# ── Text summary ───────────────────────────────────────────────────────────────

def print_summary(map_data: dict) -> None:
    attrs   = map_data["attributes"]
    lambdas = map_data["lambdas"]
    print(f"\nFlow Map — {map_data['instance_id']}")
    print(f"  Flows scanned:  {map_data['flow_count']}")
    print(f"  Attributes:     {len(attrs)}")
    print(f"  Lambdas:        {len(lambdas)}")
    print()

    if attrs:
        name_w = max(len(k) for k in attrs)
        print(f"  {'Attribute':<{name_w}}   SET  CHK  REF")
        print(f"  {'-' * name_w}   ---  ---  ---")
        for key in sorted(attrs.keys()):
            u = attrs[key]
            print(f"  {key:<{name_w}}   {len(u['set_in']):>3}  {len(u['checked_in']):>3}  {len(u['referenced_in']):>3}")
        print()

    if lambdas:
        print("  Lambda functions:")
        for arn in sorted(lambdas.keys()):
            fn = arn.split(":")[-1] if ":" in arn else arn
            flows = ", ".join(lambdas[arn])
            print(f"    {fn}  →  {flows}")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)

    p = argparse.ArgumentParser(
        description="Build an attribute/decision map across all flows in a Connect instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID> --force-refresh
  %(prog)s --instance-id <UUID> --map map.json
        """,
    )
    p.add_argument("--instance-id",    required=True, metavar="UUID")
    p.add_argument("--region",         default=None)
    p.add_argument("--profile",        default=None)
    p.add_argument("--force-refresh",  action="store_true", help="Ignore cache and re-fetch all flows")
    p.add_argument("--html",           default=None, metavar="FILE", help="HTML report path")
    p.add_argument("--scenario",       default=None, metavar="FILE", help="Scenario template path")
    p.add_argument("--map",            default=None, metavar="FILE", help="Full JSON map path")
    p.add_argument("--no-html",        action="store_true", help="Skip HTML generation")
    p.add_argument("--no-scenario",    action="store_true", help="Skip scenario template generation")
    args = p.parse_args()

    instance_id = args.instance_id

    # ── Resolve flows (cache or fetch) ────────────────────────────────────────
    manifest   = _load_manifest(instance_id)
    need_fetch = args.force_refresh or manifest is None or _is_stale(manifest)

    if need_fetch:
        if not args.region and not boto3.Session(profile_name=args.profile).region_name:
            print("Error: cache is missing or stale — pass --region to fetch flows.", file=sys.stderr)
            sys.exit(1)
        client    = _make_client(args.region, args.profile)
        envelopes = fetch_and_cache(client, instance_id)
    else:
        age = _cache_age_days(manifest)
        print(f"  Using cached flows ({manifest.get('flow_count', '?')} flows, {age:.0f}d old)", file=sys.stderr)
        envelopes = _load_cached_flows(instance_id) or []
        if not envelopes:
            print("Error: cache appears empty. Run with --force-refresh or pass --region.", file=sys.stderr)
            sys.exit(1)

    # ── Build map ─────────────────────────────────────────────────────────────
    map_data = build_map(instance_id, envelopes)

    # ── Outputs ───────────────────────────────────────────────────────────────
    print_summary(map_data)

    if args.map:
        Path(args.map).write_text(json.dumps(map_data, indent=2))
        print(f"  JSON map saved → {args.map}")

    if not args.no_html:
        html_path = args.html or f"flow_map_{instance_id}.html"
        Path(html_path).write_text(build_html(map_data), encoding="utf-8")
        print(f"  HTML report    → {html_path}")

    if not args.no_scenario:
        tmpl_path = args.scenario or f"scenario_{instance_id}.json"
        Path(tmpl_path).write_text(json.dumps(build_scenario_template(map_data), indent=2))
        print(f"  Scenario tmpl  → {tmpl_path}")

    print()


if __name__ == "__main__":
    main()
