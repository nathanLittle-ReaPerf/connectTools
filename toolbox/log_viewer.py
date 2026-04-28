#!/usr/bin/env python3
"""log_viewer.py — Interactive TUI timeline viewer for an Amazon Connect contact.

Contact-first CloudWatch log viewer. Stitches together contact milestones,
flow block executions, Lambda invocations, and Contact Lens turns into a
scrollable, filterable, drill-down timeline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import List, NamedTuple, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# textual — auto-installed by connectToolbox.py
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, Pretty, Static

import ct_config
import ct_snapshot

# ── Constants ─────────────────────────────────────────────────────────────────

RETRY_CONFIG         = Config(retries={"max_attempts": 5, "mode": "adaptive"})
LENS_RETENTION_HOURS = 24
LAMBDA_WINDOW_SECS   = 30

_LAMBDA_MODULE_TYPES = {"InvokeExternalResource", "InvokeLambdaFunction"}

_FLOW_BLOCK_LABELS = {
    "PlayPrompt":              "Play prompt",
    "GetUserInput":            "Get input",
    "CheckAttribute":          "Check attribute",
    "CheckHoursOfOperation":   "Check hours",
    "CheckAgentStatus":        "Check agent status",
    "SetQueue":                "Set queue",
    "SetAttributes":           "Set attributes",
    "UpdateContactAttributes": "Update attributes",
    "Transfer":                "Transfer",
    "Disconnect":              "Disconnect block",
    "Loop":                    "Loop",
    "SetRecordingBehavior":    "Set recording",
    "StartMediaStreaming":     "Start media stream",
    "StopMediaStreaming":      "Stop media stream",
    "SetContactFlow":          "Set contact flow",
    "SetEventHook":            "Set event hook",
    "Wait":                    "Wait",
    "CreateTask":              "Create task",
    "EndFlowExecution":        "End flow",
}

_MAN = """\
NAME
    log_viewer.py — Interactive TUI timeline viewer for an Amazon Connect contact

SYNOPSIS
    python log_viewer.py --instance-id UUID [--contact-id UUID] [OPTIONS]

DESCRIPTION
    Launches a terminal UI showing a chronological, scrollable timeline of
    flow blocks, Lambda invocations, contact milestones, and Contact Lens turns
    for a single contact. Data is fetched in background threads; the UI stays
    responsive throughout.

    If --contact-id is omitted, press [n] inside the TUI to enter one.

OPTIONS
    --instance-id UUID    Amazon Connect instance UUID. Required.
    --contact-id UUID     Contact UUID to load on startup.
    --region REGION       AWS region. Defaults to session/CloudShell region.
    --profile NAME        AWS named profile for local development.
    --log-group NAME      Override the auto-discovered Connect log group.

KEY BINDINGS
    UP/DOWN       Navigate rows
    Enter         Toggle detail panel for the selected row
    /             Open filter bar (live-filters by kind, label, or detail text)
    Escape        Clear filter / dismiss detail / close filter bar
    l             Fetch Lambda execution logs for the selected LAMBDA event
    n             Load a different contact ID without restarting
    e             Export timeline JSON to ~/.connecttools/log_viewer/<cid>_timeline.json
    q             Quit

IAM PERMISSIONS
    connect:DescribeContact, connect:DescribeInstance, connect:DescribeQueue,
    connect:DescribeUser, connect:ListRealtimeContactAnalysisSegments,
    logs:FilterLogEvents (Connect log group + /aws/lambda/* for [l])
"""


# ── Helpers (replicated from contact_timeline.py / lambda_tracer.py) ──────────

class TimelineEvent(NamedTuple):
    ts:       dt.datetime
    offset_s: float
    kind:     str   # CONTACT | FLOW | LAMBDA | LENS
    label:    str
    detail:   str
    raw:      dict


def fmt_offset(seconds: float) -> str:
    s = max(0, int(seconds))
    m, sec = divmod(s, 60)
    h, m   = divmod(m, 60)
    if h:
        return f"T+{h:02d}:{m:02d}:{sec:02d}"
    return f"T+{m:02d}:{sec:02d}"


def _ms(ts: dt.datetime) -> int:
    return int(ts.timestamp() * 1000)


def make_clients(region: Optional[str], profile: Optional[str]):
    session  = boto3.Session(profile_name=profile)
    resolved = region or session.region_name
    if not resolved:
        raise RuntimeError("Could not determine AWS region. Pass --region explicitly.")
    connect = session.client("connect", region_name=resolved, config=RETRY_CONFIG)
    logs    = session.client("logs",    region_name=resolved, config=RETRY_CONFIG)
    return connect, logs


def fetch_contact(connect, instance_id: str, contact_id: str) -> dict:
    try:
        return connect.describe_contact(
            InstanceId=instance_id, ContactId=contact_id
        )["Contact"]
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg  = exc.response["Error"]["Message"]
        raise RuntimeError(f"DescribeContact [{code}]: {msg}") from exc


def fetch_instance_alias(connect, instance_id: str) -> Optional[str]:
    try:
        return connect.describe_instance(
            InstanceId=instance_id
        )["Instance"]["InstanceAlias"]
    except ClientError:
        return None


def fetch_queue_name(connect, instance_id: str, queue_id: str) -> Optional[str]:
    try:
        return connect.describe_queue(
            InstanceId=instance_id, QueueId=queue_id
        )["Queue"]["Name"]
    except ClientError:
        return None


def fetch_agent_name(connect, instance_id: str, agent_id: str) -> Optional[str]:
    try:
        info = connect.describe_user(
            InstanceId=instance_id, UserId=agent_id
        )["User"]["IdentityInfo"]
        return f"{info.get('FirstName', '')} {info.get('LastName', '')}".strip() or None
    except ClientError:
        return None


def filter_log_events(logs_client, log_group: str, filter_pattern: str,
                      start_ms: int, end_ms: int) -> list:
    events: list = []
    kwargs: dict = {"logGroupName": log_group, "startTime": start_ms, "endTime": end_ms}
    if filter_pattern:
        kwargs["filterPattern"] = filter_pattern
    while True:
        try:
            resp = logs_client.filter_log_events(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return []
            return []
        events.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return events


def parse_message(raw: str) -> dict:
    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return {"raw": raw.strip()}


def _event_ts(ev: dict, fallback_ms: int) -> dt.datetime:
    ts_str = ev.get("Timestamp")
    if ts_str:
        try:
            return dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(fallback_ms / 1000, tz=dt.timezone.utc)


def contact_milestones(contact: dict, names: dict) -> List[TimelineEvent]:
    events  = []
    init_ts = contact.get("InitiationTimestamp")
    if not init_ts:
        return events

    method  = contact.get("InitiationMethod", "?")
    channel = contact.get("Channel", "?")
    events.append(TimelineEvent(
        init_ts, 0.0, "CONTACT", "Contact initiated",
        f"{method}  {channel}", {"source": "DescribeContact"},
    ))

    qi = contact.get("QueueInfo") or {}
    if qi.get("EnqueueTimestamp"):
        offset = (qi["EnqueueTimestamp"] - init_ts).total_seconds()
        events.append(TimelineEvent(
            qi["EnqueueTimestamp"], offset, "CONTACT", "Entered queue",
            names.get("queue") or qi.get("Id") or "",
            {"source": "DescribeContact"},
        ))

    ai = contact.get("AgentInfo") or {}
    if ai.get("ConnectedToAgentTimestamp"):
        offset = (ai["ConnectedToAgentTimestamp"] - init_ts).total_seconds()
        events.append(TimelineEvent(
            ai["ConnectedToAgentTimestamp"], offset, "CONTACT", "Agent connected",
            names.get("agent") or ai.get("Id") or "",
            {"source": "DescribeContact"},
        ))

    disc_ts = contact.get("DisconnectTimestamp")
    if disc_ts:
        offset = (disc_ts - init_ts).total_seconds()
        events.append(TimelineEvent(
            disc_ts, offset, "CONTACT", "Contact disconnected",
            contact.get("DisconnectReason") or "",
            {"source": "DescribeContact"},
        ))

    return events


def flow_events(cw_events: list, init_ts: dt.datetime) -> List[TimelineEvent]:
    result = []
    for ev in cw_events:
        msg = parse_message(ev["message"])
        if not isinstance(msg, dict):
            continue
        module_type = msg.get("ContactFlowModuleType")
        if not module_type:
            continue
        ts        = _event_ts(msg, ev["timestamp"])
        offset_s  = (ts - init_ts).total_seconds()
        flow_name = msg.get("ContactFlowName") or ""

        if module_type in _LAMBDA_MODULE_TYPES:
            params = msg.get("Parameters", {})
            arn    = params.get("FunctionArn") or params.get("LambdaFunctionARN") or ""
            fn     = arn.split(":")[-1] if ":" in arn else arn
            ext    = msg.get("ExternalResults") or msg.get("ExternalResult")
            result_str = "Success" if ext else msg.get("Error") or "Unknown"
            detail = f"{result_str}  ·  {flow_name}" if flow_name else result_str
            result.append(TimelineEvent(ts, offset_s, "LAMBDA", fn, detail, msg))
        else:
            label = _FLOW_BLOCK_LABELS.get(module_type, module_type)
            result.append(TimelineEvent(ts, offset_s, "FLOW", label, flow_name, msg))

    return result


def fetch_lens_voice(connect, instance_id: str, contact_id: str):
    segs, token = [], None
    while True:
        kwargs: dict = dict(
            InstanceId=instance_id, ContactId=contact_id,
            OutputType="Raw",
            SegmentTypes=["TRANSCRIPT", "CATEGORIES", "ISSUES", "SENTIMENT"],
        )
        if token:
            kwargs["NextToken"] = token
        try:
            resp = connect.list_realtime_contact_analysis_segments_v2(**kwargs)
        except ClientError as exc:
            return None, str(exc)
        segs.extend(resp.get("Segments", []))
        token = resp.get("NextToken")
        if not token:
            return segs, None


def fetch_lens_chat(connect, instance_id: str, contact_id: str):
    segs, token = [], None
    while True:
        kwargs: dict = dict(
            InstanceId=instance_id, ContactId=contact_id,
            OutputType="Raw",
            SegmentTypes=["TRANSCRIPT", "CATEGORIES", "ISSUES",
                          "EVENT", "ATTACHMENTS", "POST_CONTACT_SUMMARY"],
        )
        if token:
            kwargs["NextToken"] = token
        try:
            resp = connect.list_realtime_contact_analysis_segments_v2(**kwargs)
        except ClientError as exc:
            return None, str(exc)
        segs.extend(resp.get("Segments", []))
        token = resp.get("NextToken")
        if not token:
            return segs, None


def collect_lens(connect, instance_id: str, contact: dict) -> dict:
    channel = contact.get("Channel", "")
    end     = contact.get("DisconnectTimestamp")
    if end is not None:
        age_h = (dt.datetime.now(tz=dt.timezone.utc) - end).total_seconds() / 3600
        if age_h >= LENS_RETENTION_HOURS:
            return {"skipped": "Expired (>24h)"}
    if channel == "VOICE":
        segs, err = fetch_lens_voice(connect, instance_id, contact["Id"])
        return {"error": err} if err else {"segments": segs}
    if channel in ("CHAT", "EMAIL"):
        segs, err = fetch_lens_chat(connect, instance_id, contact["Id"])
        return {"error": err} if err else {"segments": segs}
    return {"skipped": f"Not supported for channel: {channel}"}


def lens_events(lens_data: dict, init_ts: dt.datetime) -> List[TimelineEvent]:
    result = []
    for seg in lens_data.get("segments") or []:
        t = seg.get("Transcript")
        if not t:
            continue
        if "BeginOffsetMillis" in t:
            offset_ms = t["BeginOffsetMillis"]
            ts        = init_ts + dt.timedelta(milliseconds=offset_ms)
            offset_s  = offset_ms / 1000
        elif "AbsoluteTime" in t:
            try:
                ts = dt.datetime.fromisoformat(t["AbsoluteTime"].replace("Z", "+00:00"))
            except ValueError:
                continue
            offset_s = (ts - init_ts).total_seconds()
        else:
            continue
        role    = t.get("ParticipantRole", "?")
        content = t.get("Content", "")
        if len(content) > 80:
            content = content[:79] + "…"
        sent = t.get("Sentiment", "")
        result.append(TimelineEvent(
            ts, offset_s, "LENS",
            f"{role}: {content}",
            f"[{sent}]" if sent else "",
            t,
        ))
    return result


def fetch_lambda_logs(logs_client, function_name: str,
                      invoked_at: dt.datetime) -> list:
    window = dt.timedelta(seconds=LAMBDA_WINDOW_SECS)
    raw    = filter_log_events(
        logs_client, f"/aws/lambda/{function_name}", "",
        _ms(invoked_at - window), _ms(invoked_at + window),
    )
    return [
        {
            "timestamp": dt.datetime.fromtimestamp(
                e["timestamp"] / 1000, tz=dt.timezone.utc
            ).isoformat(),
            "message": e["message"].rstrip(),
        }
        for e in raw
    ]


def resolve_log_group(connect, instance_id: str,
                      arg_log_group: Optional[str]):
    """Returns (log_group, error_or_None). Errors inline so TUI can display them."""
    if arg_log_group:
        cfg = ct_config.load()
        ct_config.set_log_group(cfg, instance_id, arg_log_group)
        return arg_log_group, None
    lg = ct_config.get_log_group(instance_id)
    if lg:
        return lg, None
    alias = fetch_instance_alias(connect, instance_id)
    if alias:
        return f"/aws/connect/{alias}", None
    return None, (
        "Could not auto-discover Connect log group. "
        "Pass --log-group /aws/connect/<alias> explicitly."
    )


def _serial(o: object) -> str:
    return o.isoformat() if hasattr(o, "isoformat") else str(o)  # type: ignore[attr-defined]


def _sanitize(obj: object) -> object:
    """Strip non-JSON-serialisable values (boto3 datetimes) before passing to Pretty."""
    try:
        return json.loads(json.dumps(obj, default=_serial))
    except Exception:
        return str(obj)


def _row_key(ev: TimelineEvent) -> str:
    return f"{ev.ts.isoformat()}:{ev.kind}:{ev.label[:20]}"


# ── App state ─────────────────────────────────────────────────────────────────

class AppState:
    def __init__(self) -> None:
        self.instance_id:   str           = ""
        self.contact_id:    str           = ""
        self.region:        Optional[str] = None
        self.profile:       Optional[str] = None
        self.log_group_arg: Optional[str] = None

        self.contact:     Optional[dict]       = None
        self.names:       dict                 = {}
        self.log_group:   str                  = ""
        self.timeline:    List[TimelineEvent]  = []
        self.filtered:    List[TimelineEvent]  = []
        self.filter_text: str                  = ""
        self.lambda_logs: dict                 = {}  # row_key → list of log dicts


# ── Custom messages ───────────────────────────────────────────────────────────

class ContactLoaded(Message):
    def __init__(self, contact: dict, names: dict, log_group: str) -> None:
        self.contact   = contact
        self.names     = names
        self.log_group = log_group
        super().__init__()


class TimelineReady(Message):
    def __init__(self, events: List[TimelineEvent]) -> None:
        self.events = events
        super().__init__()


class LambdaLogsLoaded(Message):
    def __init__(self, row_key: str, function_name: str,
                 logs: list, error: Optional[str]) -> None:
        self.row_key       = row_key
        self.function_name = function_name
        self.logs          = logs
        self.error         = error
        super().__init__()


class LoadError(Message):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__()


# ── Modal ─────────────────────────────────────────────────────────────────────

class NewContactModal(ModalScreen):
    DEFAULT_CSS = """
    NewContactModal {
        align: center middle;
    }
    #new-contact-dialog {
        width: 64;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 2 4;
    }
    #new-contact-dialog Label {
        margin-bottom: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Enter Contact ID:"),
            Input(
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                id="modal-cid-input",
            ),
            id="new-contact-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#modal-cid-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        cid = event.value.strip()
        self.dismiss(cid if cid else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Main App ──────────────────────────────────────────────────────────────────

_CSS = """
Screen {
    background: $surface;
}

#app-header {
    height: 1;
    background: $primary-darken-2;
    color: $text;
    padding: 0 2;
    content-align: left middle;
}

#status-bar {
    height: 1;
    background: $surface-darken-2;
    color: $text-muted;
    padding: 0 2;
    content-align: left middle;
}

#timeline-table {
    height: 1fr;
}

#detail-panel {
    height: 14;
    border-top: solid $primary;
    background: $surface-darken-1;
    display: none;
    padding: 0 1;
}

#detail-panel.visible {
    display: block;
}

#filter-bar {
    height: 3;
    border-top: solid $accent;
    display: none;
}

#filter-bar.visible {
    display: block;
}
"""


class LogViewerApp(App):
    CSS   = _CSS
    TITLE = "Log Viewer — Amazon Connect"

    BINDINGS = [
        Binding("enter",  "toggle_detail", "Detail",      show=True),
        Binding("slash",  "focus_filter",  "Filter",      show=True),
        Binding("escape", "escape_action", "Clear",       show=True),
        Binding("l",      "fetch_lambda",  "Lambda logs", show=True),
        Binding("n",      "new_contact",   "New contact", show=True),
        Binding("e",      "export_json",   "Export",      show=True),
        Binding("q",      "quit",          "Quit",        show=True),
    ]

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self._state = state

    # ── Layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="app-header")
        yield Static("", id="status-bar")
        yield DataTable(id="timeline-table", zebra_stripes=True)
        yield ScrollableContainer(Pretty({}), id="detail-panel")
        yield Input(
            placeholder="Filter events…  (kind, label, or detail)",
            id="filter-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#timeline-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("OFFSET", "KIND", "EVENT", "DETAIL")
        self._update_header()
        if self._state.contact_id:
            self._set_status("Fetching contact…")
            self._load_contact(self._state.contact_id)
        else:
            self._set_status("Press [n] to enter a Contact ID")

    # ── Header / status ───────────────────────────────────────────────────

    def _update_header(self) -> None:
        s = self._state
        if s.contact:
            start = s.contact.get("InitiationTimestamp")
            end   = s.contact.get("DisconnectTimestamp")
            dur   = ""
            if start and end:
                secs = int((end - start).total_seconds())
                m, sc = divmod(secs, 60)
                dur = f"  {m}m{sc:02d}s"
            channel = s.contact.get("Channel", "")
            header  = (
                f"Log Viewer  │  {s.instance_id}"
                f"  │  {s.contact_id}{dur}  {channel}"
            )
        else:
            header = f"Log Viewer  │  {s.instance_id}"
        self.query_one("#app-header", Static).update(header)

    def _set_status(self, msg: str) -> None:
        self.query_one("#status-bar", Static).update(msg)

    # ── Workers ───────────────────────────────────────────────────────────

    @work(thread=True)
    def _load_contact(self, contact_id: str) -> None:
        s = self._state
        try:
            connect, logs_client = make_clients(s.region, s.profile)
        except RuntimeError as exc:
            self.call_from_thread(self.post_message, LoadError(str(exc)))
            return

        try:
            contact = fetch_contact(connect, s.instance_id, contact_id)
        except RuntimeError as exc:
            self.call_from_thread(self.post_message, LoadError(str(exc)))
            return

        names: dict = {}
        qi = contact.get("QueueInfo") or {}
        if qi.get("Id"):
            names["queue"] = fetch_queue_name(connect, s.instance_id, qi["Id"])
        ai = contact.get("AgentInfo") or {}
        if ai.get("Id"):
            names["agent"] = fetch_agent_name(connect, s.instance_id, ai["Id"])

        log_group, lg_err = resolve_log_group(connect, s.instance_id, s.log_group_arg)
        if lg_err:
            self.call_from_thread(self.post_message, LoadError(lg_err))
            return

        # Header renders while logs are still being fetched
        self.call_from_thread(
            self.post_message, ContactLoaded(contact, names, log_group)
        )
        self.call_from_thread(self._set_status, "Fetching flow logs…")

        start_ts = contact["InitiationTimestamp"]
        end_ts   = contact.get("DisconnectTimestamp")
        now      = dt.datetime.now(dt.timezone.utc)
        start_ms = _ms(start_ts - dt.timedelta(minutes=2))
        end_ms   = _ms(min(end_ts + dt.timedelta(minutes=5), now) if end_ts else now)

        cw_events = filter_log_events(
            logs_client, log_group,
            f'{{ $.ContactId = "{contact_id}" }}',
            start_ms, end_ms,
        )

        self.call_from_thread(self._set_status, "Fetching Contact Lens…")
        lens_data = collect_lens(connect, s.instance_id, contact)

        milestones = contact_milestones(contact, names)
        flow_evs   = flow_events(cw_events, start_ts)
        lens_evs   = lens_events(lens_data, start_ts) if "segments" in lens_data else []
        timeline   = sorted(milestones + flow_evs + lens_evs, key=lambda e: e.ts)

        self.call_from_thread(self.post_message, TimelineReady(timeline))

    @work(thread=True)
    def _fetch_lambda_logs_worker(self, row_key: str, function_name: str,
                                   invoked_at: dt.datetime) -> None:
        s = self._state
        self.call_from_thread(
            self._set_status, f"Fetching Lambda logs: {function_name}…"
        )
        try:
            _, logs_client = make_clients(s.region, s.profile)
            logs = fetch_lambda_logs(logs_client, function_name, invoked_at)
            self.call_from_thread(
                self.post_message,
                LambdaLogsLoaded(row_key, function_name, logs, None),
            )
        except Exception as exc:
            self.call_from_thread(
                self.post_message,
                LambdaLogsLoaded(row_key, function_name, [], str(exc)),
            )

    # ── Message handlers ──────────────────────────────────────────────────

    def on_contact_loaded(self, message: ContactLoaded) -> None:
        s           = self._state
        s.contact   = message.contact
        s.names     = message.names
        s.log_group = message.log_group
        self._update_header()

    def on_timeline_ready(self, message: TimelineReady) -> None:
        s          = self._state
        s.timeline = message.events
        s.filtered = list(message.events)
        self._render_table()
        n_flow   = sum(1 for e in s.timeline if e.kind == "FLOW")
        n_lambda = sum(1 for e in s.timeline if e.kind == "LAMBDA")
        n_lens   = sum(1 for e in s.timeline if e.kind == "LENS")
        lens_str = f", {n_lens} lens" if n_lens else ""
        self._set_status(
            f"{len(s.timeline)} events  ({n_flow} flow, {n_lambda} lambda{lens_str})"
            f"  │  [/] filter  [Enter] detail  [l] λ logs  [e] export  [q] quit"
        )

    def on_lambda_logs_loaded(self, message: LambdaLogsLoaded) -> None:
        s = self._state
        s.lambda_logs[message.row_key] = message.logs
        if message.error:
            self._set_status(f"Lambda log fetch error: {message.error}")
        else:
            self._set_status(
                f"Lambda logs: {message.function_name} ({len(message.logs)} lines)"
            )
        # Refresh detail panel if this row is currently shown
        table = self.query_one("#timeline-table", DataTable)
        panel = self.query_one("#detail-panel")
        row   = table.cursor_row
        if "visible" in panel.classes and 0 <= row < len(s.filtered):
            if _row_key(s.filtered[row]) == message.row_key:
                self._show_detail(s.filtered[row])

    def on_load_error(self, message: LoadError) -> None:
        self._set_status(f"Error: {message.message}")

    # ── Table rendering ───────────────────────────────────────────────────

    def _render_table(self) -> None:
        s     = self._state
        table = self.query_one("#timeline-table", DataTable)
        table.clear()
        for ev in s.filtered:
            offset = fmt_offset(ev.offset_s)
            label  = ev.label[:42] if len(ev.label) > 42 else ev.label
            detail = ev.detail[:58] if len(ev.detail) > 58 else ev.detail
            kind   = ev.kind

            if kind == "CONTACT":
                kind_cell  = f"[bold]{kind}[/bold]"
                label_cell = f"[bold]{label}[/bold]"
            elif kind == "LAMBDA":
                kind_cell  = f"[yellow]{kind}[/yellow]"
                label_cell = f"[yellow]{label}[/yellow]"
            elif kind == "LENS":
                kind_cell  = f"[dim]{kind}[/dim]"
                label_cell = f"[dim]{label}[/dim]"
            else:
                kind_cell  = kind
                label_cell = label

            table.add_row(offset, kind_cell, label_cell, detail,
                          key=_row_key(ev))

    def _apply_filter(self) -> None:
        s = self._state
        q = s.filter_text.lower()
        if q:
            s.filtered = [
                e for e in s.timeline
                if q in e.kind.lower()
                or q in e.label.lower()
                or q in e.detail.lower()
            ]
        else:
            s.filtered = list(s.timeline)
        self._render_table()

    # ── Detail panel ──────────────────────────────────────────────────────

    def _show_detail(self, ev: TimelineEvent) -> None:
        rk    = _row_key(ev)
        s     = self._state
        panel = self.query_one("#detail-panel", ScrollableContainer)

        if ev.kind == "LAMBDA" and rk in s.lambda_logs:
            content = _sanitize({
                "flow_log_entry": ev.raw,
                "lambda_logs":    s.lambda_logs[rk],
            })
        elif ev.kind == "LAMBDA":
            content = _sanitize({
                "flow_log_entry": ev.raw,
                "lambda_logs":    "(not fetched — press [l] to load)",
            })
        else:
            content = _sanitize(ev.raw)

        panel.query_one(Pretty).update(content)

    # ── Action handlers ───────────────────────────────────────────────────

    def action_toggle_detail(self) -> None:
        s     = self._state
        table = self.query_one("#timeline-table", DataTable)
        panel = self.query_one("#detail-panel")
        row   = table.cursor_row

        if "visible" in panel.classes:
            panel.remove_class("visible")
            return

        if not s.filtered or row < 0 or row >= len(s.filtered):
            return
        panel.add_class("visible")
        self._show_detail(s.filtered[row])

    def action_focus_filter(self) -> None:
        fbar = self.query_one("#filter-bar", Input)
        fbar.add_class("visible")
        fbar.focus()

    def action_escape_action(self) -> None:
        s     = self._state
        panel = self.query_one("#detail-panel")
        fbar  = self.query_one("#filter-bar", Input)

        if "visible" in panel.classes:
            panel.remove_class("visible")
        elif s.filter_text:
            fbar.clear()
            s.filter_text = ""
            fbar.remove_class("visible")
            self._apply_filter()
        else:
            fbar.remove_class("visible")

    def action_fetch_lambda(self) -> None:
        s     = self._state
        table = self.query_one("#timeline-table", DataTable)
        row   = table.cursor_row

        if not s.filtered or row < 0 or row >= len(s.filtered):
            return
        ev = s.filtered[row]
        if ev.kind != "LAMBDA":
            self._set_status("Selected row is not a LAMBDA event — navigate to a LAMBDA row first")
            return

        rk = _row_key(ev)
        if rk in s.lambda_logs:
            # Already cached — just open the detail panel
            panel = self.query_one("#detail-panel")
            panel.add_class("visible")
            self._show_detail(ev)
            return

        self._fetch_lambda_logs_worker(rk, ev.label, ev.ts)

    def action_new_contact(self) -> None:
        def handle(cid: Optional[str]) -> None:
            if not cid:
                return
            s = self._state
            s.contact_id  = cid
            s.contact     = None
            s.names       = {}
            s.log_group   = ""
            s.timeline    = []
            s.filtered    = []
            s.filter_text = ""
            s.lambda_logs = {}

            self.query_one("#detail-panel").remove_class("visible")
            fbar = self.query_one("#filter-bar", Input)
            fbar.clear()
            fbar.remove_class("visible")
            self.query_one("#timeline-table", DataTable).clear()
            self._update_header()
            self._set_status("Fetching contact…")
            self._load_contact(cid)

        self.push_screen(NewContactModal(), handle)

    def action_export_json(self) -> None:
        s = self._state
        if not s.timeline:
            self._set_status("Nothing to export — load a contact first")
            return

        doc = {
            "contact_id":  s.contact_id,
            "contact":     _sanitize(s.contact),
            "names":       s.names,
            "log_group":   s.log_group,
            "event_count": len(s.timeline),
            "events": [
                {
                    "offset_s":   round(e.offset_s, 3),
                    "offset_fmt": fmt_offset(e.offset_s),
                    "timestamp":  e.ts.isoformat(),
                    "kind":       e.kind,
                    "label":      e.label,
                    "detail":     e.detail,
                }
                for e in s.timeline
            ],
        }
        dest = ct_snapshot.output_path("log_viewer", f"{s.contact_id}_timeline.json")
        try:
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2)
            self._set_status(f"Exported → {dest}")
        except OSError as exc:
            self._set_status(f"Export failed: {exc}")

    # ── Input / DataTable event routing ──────────────────────────────────

    @on(Input.Changed, "#filter-bar")
    def on_filter_changed(self, event: Input.Changed) -> None:
        self._state.filter_text = event.value
        self._apply_filter()

    @on(DataTable.RowHighlighted, "#timeline-table")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        s     = self._state
        panel = self.query_one("#detail-panel")
        if "visible" not in panel.classes:
            return
        row = getattr(event, "cursor_row", None)
        if row is None:
            row = self.query_one("#timeline-table", DataTable).cursor_row
        if 0 <= row < len(s.filtered):
            self._show_detail(s.filtered[row])


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(
        description="Interactive TUI timeline viewer for an Amazon Connect contact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --instance-id <UUID> --contact-id <UUID> --region us-east-1
  %(prog)s --instance-id <UUID>   # enter contact ID via [n] in the TUI
        """,
    )
    p.add_argument("--instance-id", required=True,  metavar="UUID")
    p.add_argument("--contact-id",  default=None,   metavar="UUID",
                   help="Contact to load on start. Omit to enter via [n] in the TUI.")
    p.add_argument("--region",    default=None)
    p.add_argument("--profile",   default=None)
    p.add_argument("--log-group", default=None, metavar="NAME")
    return p.parse_args()


def main():
    if "--man" in sys.argv:
        print(_MAN)
        sys.exit(0)
    args  = _parse_args()
    state = AppState()
    state.instance_id   = args.instance_id
    state.contact_id    = args.contact_id or ""
    state.region        = args.region
    state.profile       = args.profile
    state.log_group_arg = args.log_group
    LogViewerApp(state).run()


if __name__ == "__main__":
    main()
