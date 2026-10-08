"""Operations plugin: backend routes, mounted at /api/plugins/operations/.

GET /overview?hours=N returns everything the Overview page shows: vitals for
the window, today's hourly strip, what needs attention, hosts with their model
pool, and recent activity. Opens the metrics collector's
<hermes root>/metrics/metrics.db and the kanban board with mode=ro. The only
writes are the actions below: kanban cards, a GitLab merge request for a rule,
the plugin's own files in plugin-data/operations/ (roles.json,
summaries.json, fixes.json), and in business mode the standard's org skill. Each section is built independently, so one failing source leaves the
others intact and its error is reported under "errors". A database that does
not exist (no metrics collector, no kanban board yet) is replaced by an empty
one, so its numbers read as empty and errors["metrics"] / errors["kanban"] say why.

GET /changes?hours=N returns what changed in the window: MRs merged on
GitLab (read with the token glab stores, or GITLAB_TOKEN) and model loads and
unloads in the pool. It is a separate route so a slow GitLab never holds up
/overview.

GET /mrs?hours=N returns open merge requests and those merged in the window,
each with its reviewers and its newest pipeline's stages (passed, failed,
running or skipped). Read only.

GET /flow?hours=N returns the window's requests as links from entry point
(platform) to profile to model to what served them (the pool device, or the
cloud provider when no device did): each distinct path with its count, each
node's count, errors and p95, and requests per hour split by what served them.

GET /activity?hours=N returns the window's activity log, grouped per hour:
errors, gateway state changes, kanban task events and host check status
changes, each group with its count, its first raw lines and a key. Merged MRs and
model loads come from /changes; the page merges the two.

POST /activity/summaries {"hours"} returns a readable sentence per Activity
entry, written by the model set for the "operations_summary" auxiliary task,
each with the model's name and how many events it read. Off unless the
activity_summaries setting is on; summaries are cached in
plugin-data/operations/summaries.json.

GET /handoffs?hours=N returns who handed work to whom in the window: kanban
cards (created_by to assignee, with every run and its outcome) and subagent
delegations (subagent_stop records grouped by parent session, with each
child's model and request count), plus totals per (from, to) pair.

GET /assignees lists the profiles a card can be handed to. POST
/cards/{id}/assign {"profile"} hands an open card to one and, when the card is
ready, starts that profile's worker at once (one dispatcher tick, with the
kanban limits from config.yaml that the gateway applies); with "start_at"
(epoch seconds) it parks the card in kanban's "scheduled" status instead, and
the plugin's dispatch-tick hook (../__init__.py) releases it at that time. POST
/cards/{id}/unblock {"comment"} answers a blocked card (the comment goes on the
card as "dashboard") and starts its profile again the same way. And POST /cards/{id}/close {"outcome":
"done"|"archived", "result"} closes it (done needs a result). They go through Hermes' own kanban functions,
the same ones the Kanban page and `hermes kanban` use.

GET /conformance returns the fleet standard's repo checks: the
conformance-report.json artifact the standard repo's CI job "standard"
publishes, with each rule's title, severity and fix from conformance.yaml, and
the latest pipeline on main, and the host checks: each host's newest
conformance_results in metrics.db (written by the metrics forwarder on that host,
see hermes-metrics-dash host_checks.py) next to the hosts and host rules the
standard declares. The project is the plugin setting "conformance_project": a GitLab
group/repo, or a path to a local conformance.yaml (no CI report or pipeline then).
Unset, it is ~/.hermes/conformance/conformance.yaml when that exists (POST
/conformance/local starts one); without either the route reports it is not set up.

POST /conformance/hosts/{host}/fix {"profile"} files a kanban card for that
profile to fix the host's failing checks; with no profile it records that a
person is fixing them by hand and returns the standard's remediate commands.
The dashboard never runs anything on the host. Either way the request is saved
in plugin-data/operations/fixes.json, GET /conformance returns each host's
newest one with its state from the host's next report (waiting, fixed, still
failing), and /activity lists it. POST /conformance/projects/fix {"root",
"profile"} files a card for a project's findings, POST /attention/fix {"key",
"title", "body", "profile"} files one for an alert from the Notifications tab,
and POST /conformance/rules {"kind", "rule"} proposes a rule as a merge request on the standard (into
the file itself for a local standard); with "to": "org" in business mode (an
organisation with Hermes skill sync on, see org_mode, returned by GET
/conformance as "org") it goes to the org skill
named after the standard through Hermes' sync client instead, and the sync
server decides whether it publishes or waits for an admin.

GET /roles returns the roles the page can be viewed as (job description,
responsibilities, skills, the Activity kinds watched, the Topology layout):
this install's saved ones from plugin-data/operations/roles.json, else the
built-in ones. PUT /roles {"roles"} saves the whole list; DELETE /roles puts
back the built-in ones.

GET /conformance/projects?root=<folder>... checks each folder (the page passes the projects
Hermes Desktop lists) over its git-tracked files against the built-in DEFAULT_REPO_RULES plus,
when there is a fleet standard (see GET /conformance), that standard's "absent" repo rules. Read only.

GET /setup returns what a first run still needs, for the Overview page's setup checklist: each
profile with operations on, off (listed under plugins.disabled) or missing from plugins.enabled,
whether metrics.db and the kanban board exist, and the conformance_project, alert_target,
drift_action and drift_profile settings, and from_default: which of those this profile takes from
the default profile's settings. Also the requesting profile and settings_page: whether
Hermes Desktop has a settings form for operations there, which it only builds for a profile with
its own copy under <profile>/plugins (a root-only install has none in named profiles). Read only.

Settings live in config.yaml under plugins.entries.operations.settings and are
edited from Capabilities > Plugins in Hermes Desktop (see plugin.yaml).
A named profile that leaves one unset uses the default profile's value.

Runs inside the Hermes dashboard: stdlib, FastAPI and Hermes' own modules only.
"""

from __future__ import annotations

import base64
import bisect
import contextlib
import contextvars
import fnmatch
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.secret_scope import get_secret
from fastapi import APIRouter, Body, HTTPException, Query
from hermes_cli import kanban_db
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.config import load_config
from hermes_cli.kanban_db import kanban_db_path
from hermes_cli.kanban_db_connect import connect_closing
from hermes_constants import get_default_hermes_root, get_hermes_home
from hermes_yaml import safe_load
from ruamel.yaml import YAMLError

router = APIRouter()

ID = "operations"
_METRICS_DB = get_default_hermes_root() / "metrics" / "metrics.db"
# The standard used while the Conformance project setting is empty, once POST /conformance/local makes it.
_LOCAL_STANDARD = get_default_hermes_root() / "conformance" / "conformance.yaml"
_KANBAN_DB = kanban_db_path()
# Host name -> pool device name, kept by hand for Ops Timeline; read here, never written.
_DEVICE_ALIASES = get_default_hermes_root() / "plugin-data" / "gitlab-mr" / "device-aliases.json"
# host_stats arrive every 30 s; a host silent for longer than this is stale.
STALE_S = 120
# The forwarder's label for a platform in a running gateway's state file that an earlier gateway process
# wrote: the running gateway does not run it (a bot since removed, or never set up here). Hermes keeps the
# entry, so a gateway whose newest state is this is left out rather than shown as an alert.
STALE_GATEWAY = "stale"
# A device is in the pool if it reported within this long; its models are the ones
# reported within this long of its newest row (older LM Link ids drop out).
POOL_SNAPSHOT_S = 120
ACTIVITY_LIMIT = 25
# Raw rows read before grouping; bounds the 7-day window on a noisy day.
ACTIVITY_SCAN = 2000
KANBAN_ACTIVITY_KINDS = ("created", "completed", "crashed", "blocked", "timed_out", "gave_up")
SEV_RANK = {"crit": 0, "warn": 1, "info": 2}
# glab's config: GLAB_CONFIG_DIR, else its macOS or XDG location, whichever exists.
_GLAB_CONFIG = next((d / "config.yml" for d in (
    Path(os.environ["GLAB_CONFIG_DIR"]) if os.environ.get("GLAB_CONFIG_DIR") else None,
    Path.home() / "Library" / "Application Support" / "glab-cli",
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "glab-cli",
) if d is not None and (d / "config.yml").exists()), Path.home() / ".config" / "glab-cli" / "config.yml")
CHANGES_LIMIT = 40
MRS_LIMIT = 40
# Pool snapshots arrive about once a minute; a longer silence is the device being away,
# not its models unloading, so the comparison restarts after it.
POOL_GAP_MIN = 5
# Hosts run their checks every 5 minutes; five missed reports and a host's results are stale.
HOST_STALE_S = 5 * 300
FLOW_COLUMNS = ("entry", "profile", "model", "served")
LOG_LIMIT = 300
HANDOFF_LIMIT = 200
TRACE_SESSIONS = 100
TRACE_CALLS = 500
# A call this many times its usual duration (same model, same server, the week before) counts as slow.
SLOW_FACTOR = 2
SLOW_MIN_S = 5
TIMELINE_CHANGES = 200
# A gateway that stops and is back in some state within this long was restarted: one change, not two.
RESTART_S = 180
USUAL_MIN_CALLS = 5  # fewer similar calls than this and a call has no typical time
# Run errors are excerpts; the kanban board has the full text.
RUN_ERROR_CHARS = 200
LOG_RAW = 5
# Kanban events worth a line; heartbeat, respawn_guarded and spawned (the twin of claimed) are noise.
LOG_TASK_KINDS = ("created", "assigned", "claimed", "commented", "completed", "blocked", "crashed",
                  "timed_out", "gave_up", "rate_limited", "reclaimed")


# The columns this plugin reads, for the empty stand-in of a database that is missing.
_EMPTY_METRICS = """
CREATE TABLE records (ts REAL, host TEXT, event TEXT, kind TEXT, platform TEXT, model TEXT, duration_s REAL,
  error TEXT, label TEXT, device TEXT, cpu_load REAL, mem_used_gb REAL, mem_total_gb REAL, session_id TEXT,
  parent_session_id TEXT);
CREATE TABLE health_findings (ts REAL, rule TEXT, sev TEXT, text TEXT, action TEXT);
"""
_EMPTY_KANBAN = """
CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT, created_at INTEGER,
  consecutive_failures INTEGER, last_failure_error TEXT, created_by TEXT, completed_at INTEGER);
CREATE TABLE task_runs (task_id TEXT, profile TEXT, started_at INTEGER, ended_at INTEGER, outcome TEXT, error TEXT);
CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
"""


def _ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _ro_or_empty(db: Path, schema: str, errors: Dict[str, str], name: str) -> sqlite3.Connection:
    """The database read-only, or an empty in-memory stand-in with the reason under errors[name]."""
    try:
        return _ro(db)
    except sqlite3.Error as exc:
        errors[name] = f"{db} not found" if not db.exists() else str(exc)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    return conn


def _local_midnight(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _p95(values: List[float]):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def vitals(m: sqlite3.Connection, k: sqlite3.Connection, since: float, now: float) -> Dict[str, Any]:
    durations = [r[0] for r in m.execute(
        "SELECT duration_s FROM records WHERE kind='api' AND ts>=? AND duration_s IS NOT NULL", (since,))]
    requests = m.execute("SELECT COUNT(*) FROM records WHERE kind='api' AND ts>=?", (since,)).fetchone()[0]
    errors = m.execute("SELECT COUNT(*) FROM records WHERE kind='error' AND ts>=?", (since,)).fetchone()[0]
    last_seen = [r[0] for r in m.execute(
        "SELECT MAX(ts) FROM records WHERE kind='host_stats' AND ts>=? GROUP BY host", (now - 86400,))]
    open_tasks = k.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done', 'archived')").fetchone()[0]
    return {
        "requests": requests,
        "errors": errors,
        "p95_s": _p95(durations),
        "hosts_total": len(last_seen),
        "hosts_reporting": sum(1 for ts in last_seen if now - ts <= STALE_S),
        "open_tasks": open_tasks,
    }


def today(m: sqlite3.Connection, k: sqlite3.Connection, now: float) -> List[Dict[str, Any]]:
    start = _local_midnight(now)
    hours = [{"ts": start + h * 3600, "requests": 0, "errors": 0, "tasks_done": 0, "tasks_failed": 0}
             for h in range(int((now - start) // 3600) + 1)]
    for kind, field in (("api", "requests"), ("error", "errors")):
        for bucket, n in m.execute(
                "SELECT CAST((ts-?)/3600 AS INTEGER), COUNT(*) FROM records WHERE kind=? AND ts>=? GROUP BY 1",
                (start, kind, start)):
            if 0 <= bucket < len(hours):
                hours[bucket][field] = n
    for bucket, kind, n in k.execute(
            "SELECT CAST((created_at-?)/3600 AS INTEGER), kind, COUNT(*) FROM task_events"
            " WHERE created_at>=? AND kind IN ('completed', 'crashed', 'timed_out', 'gave_up') GROUP BY 1, 2",
            (start, start)):
        if 0 <= bucket < len(hours):
            hours[bucket]["tasks_done" if kind == "completed" else "tasks_failed"] += n
    return hours


def attention(m: sqlite3.Connection, k: sqlite3.Connection, now: float) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    snap = m.execute("SELECT MAX(ts) FROM health_findings").fetchone()[0]
    if snap is not None:
        for r in m.execute(
                "SELECT rule, sev, text, action FROM health_findings WHERE ts=? AND sev IN ('crit', 'warn')", (snap,)):
            items.append({"key": f"health:{r['rule']}", "sev": r["sev"], "source": f"health {r['rule']}", "text": r["text"],
                          "action": r["action"], "ts": snap})
    for r in m.execute(
            "SELECT host, platform, label, error, MAX(ts) AS ts FROM records"
            " WHERE kind='gateway_status' AND ts>=? GROUP BY platform", (now - 86400,)):
        if r["label"] not in ("connected", STALE_GATEWAY):
            items.append({"key": f"gateway:{r['platform']}:{r['label']}",
                          "sev": "crit" if r["label"] == "fatal" else "warn", "source": "gateway",
                          "text": f"{r['platform']} is {r['label']}", "action": r["error"], "ts": r["ts"], "host": r["host"]})
    for r in m.execute(
            "SELECT host, MAX(ts) AS ts FROM records WHERE kind='host_stats' AND ts>=? GROUP BY host", (now - 86400,)):
        if now - r["ts"] > STALE_S:
            items.append({"key": f"host:{r['host']}", "sev": "warn", "source": "host", "text": f"{r['host']} has sent no host stats for "
                          f"{int((now - r['ts']) // 60)} min", "action": "check its metrics forwarder", "ts": r["ts"]})
    # Kanban's two human queues: "blocked" (a card waiting on another card sits in todo instead)
    # and "triage" (new cards to sort, and cards that kept re-blocking for the same cause).
    for r in k.execute(
            "SELECT t.id, t.title, t.assignee, t.status, t.last_failure_error, t.created_at,"
            " MAX(e.created_at) AS ts FROM tasks t LEFT JOIN task_events e ON e.task_id=t.id"
            " AND e.kind IN ('blocked', 'block_loop_detected') WHERE t.status IN ('blocked', 'triage') GROUP BY t.id"):
        blocked = r["status"] == "blocked"
        items.append({"key": f"kanban:{r['id']}", "sev": "warn", "source": "kanban", "card": r["id"],
                      "text": f"{r['title']} {'is waiting on you' if blocked else 'needs triage'}"
                              + (f" ({r['assignee']})" if r["assignee"] else ""),
                      "action": _reason(k, r["id"], "blocked" if blocked else "block_loop_detected")
                                or r["last_failure_error"]
                                or ("reply on the card to unblock it" if blocked else "sort it on the Kanban page"),
                      "ts": r["ts"] or r["created_at"]})
    for r in k.execute(
            "SELECT id, title, assignee, consecutive_failures, last_failure_error, created_at FROM tasks"
            " WHERE status NOT IN ('done', 'archived', 'blocked', 'triage') AND consecutive_failures > 0"):
        items.append({"key": f"failed:{r['id']}", "sev": "warn", "source": "kanban",
                      "text": f"{r['title']} failed {r['consecutive_failures']} times in a row"
                              + (f" ({r['assignee']})" if r["assignee"] else ""),
                      "action": r["last_failure_error"], "ts": r["created_at"]})
    items.sort(key=lambda i: (SEV_RANK.get(i["sev"], 3), -(i["ts"] or 0)))
    return items


def _device_aliases() -> Dict[str, str]:
    try:
        data = json.loads(_DEVICE_ALIASES.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def hosts(m: sqlite3.Connection, now: float) -> Dict[str, Any]:
    out = []
    for r in m.execute(
            "SELECT host, cpu_load, mem_used_gb, mem_total_gb, MAX(ts) AS ts FROM records"
            " WHERE kind='host_stats' AND ts>=? GROUP BY host ORDER BY host", (now - 86400,)):
        out.append({"host": r["host"], "cpu_load": r["cpu_load"], "mem_used_gb": r["mem_used_gb"],
                    "mem_total_gb": r["mem_total_gb"], "last_seen": r["ts"], "stale": now - r["ts"] > STALE_S})
    # LM Studio names extra loaded copies of a model "<model>:2", ":3", ...; count them as one.
    pool: Dict[str, Dict[str, int]] = {}
    for r in m.execute(
            "SELECT p.device, p.model FROM records p JOIN (SELECT device, MAX(ts) AS last FROM records"
            " WHERE kind='pool_status' AND ts>=? GROUP BY device) d ON d.device=p.device"
            " WHERE p.kind='pool_status' AND p.ts>=d.last-? GROUP BY p.device, p.model ORDER BY p.device, p.model",
            (now - POOL_SNAPSHOT_S, POOL_SNAPSHOT_S)):
        models = pool.setdefault(r["device"], {})
        name = re.sub(r":\d+$", "", r["model"] or "")
        models[name] = models.get(name, 0) + 1
    aliases = _device_aliases()
    for h in out:
        h["device"] = aliases.get(h["host"])
    with_stats = {aliases.get(h["host"], h["host"]) for h in out}
    return {"hosts": out,
            "pool": [{"device": d, "models": [{"name": n, "count": c} for n, c in ms.items()]}
                     for d, ms in pool.items()],
            # serving models but sending no host stats, e.g. machines without a metrics forwarder
            "pool_only": [d for d in pool if d not in with_stats]}


def activity(m: sqlite3.Connection, k: sqlite3.Connection, since: float) -> List[Dict[str, Any]]:
    """Errors and task events, identical (kind, text, where) entries grouped into one with a count."""
    raw = [(r["ts"], "error", r["error"] or f"{r['event']} failed ({r['model'] or 'no model'})", r["device"] or r["host"])
           for r in m.execute("SELECT ts, host, device, event, model, error FROM records WHERE kind='error' AND ts>=?"
                              " ORDER BY ts DESC LIMIT ?", (since, ACTIVITY_SCAN))]
    marks = ",".join("?" * len(KANBAN_ACTIVITY_KINDS))
    raw += [(r["created_at"], f"task {r['kind'].replace('_', ' ')}", r["title"], r["assignee"])
            for r in k.execute(f"SELECT e.created_at, e.kind, t.title, t.assignee FROM task_events e"
                               f" JOIN tasks t ON t.id=e.task_id WHERE e.created_at>=? AND e.kind IN ({marks})"
                               f" ORDER BY e.created_at DESC LIMIT ?",
                               (since, *KANBAN_ACTIVITY_KINDS, ACTIVITY_SCAN))]
    groups: Dict[tuple, Dict[str, Any]] = {}
    for ts, kind, text, where in raw:
        g = groups.setdefault((kind, text, where), {"ts": ts, "first_ts": ts, "count": 0,
                                                    "kind": kind, "text": text, "where": where})
        g["ts"], g["first_ts"], g["count"] = max(g["ts"], ts), min(g["first_ts"], ts), g["count"] + 1
    return sorted(groups.values(), key=lambda g: -g["ts"])[:ACTIVITY_LIMIT]


@router.get("/overview")
def overview(hours: int = Query(24, ge=1, le=168)) -> Dict[str, Any]:
    now = time.time()
    since = now - hours * 3600
    out: Dict[str, Any] = {"generated_at": now, "hours": hours, "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    k = _ro_or_empty(_KANBAN_DB, _EMPTY_KANBAN, out["errors"], "kanban")
    sections = {
        "vitals": lambda: vitals(m, k, since, now),
        "today": lambda: today(m, k, now),
        "attention": lambda: attention(m, k, now),
        "hosts": lambda: hosts(m, now),
        "activity": lambda: activity(m, k, since),
    }
    try:
        for name, build in sections.items():
            try:
                out[name] = build()
            except sqlite3.Error as exc:
                out[name] = None
                out["errors"][name] = str(exc)
    finally:
        m.close()
        k.close()
    return out


@router.get("/attention")
def attention_route() -> Dict[str, Any]:
    """Just the attention list, for Desktop's background alert poll."""
    out: Dict[str, Any] = {"items": [], "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    k = _ro_or_empty(_KANBAN_DB, _EMPTY_KANBAN, out["errors"], "kanban")
    try:
        out["items"] = attention(m, k, time.time())
    except sqlite3.Error as exc:
        out["errors"]["attention"] = str(exc)
    finally:
        m.close()
        k.close()
    return out


def _settings_in(config: Any) -> Dict[str, Any]:
    """plugins.entries.operations.settings in one parsed config.yaml, without unset ("" or null) values."""
    data = ((((config or {}).get("plugins") or {}).get("entries") or {}).get(ID) or {}).get("settings")
    return {k: v for k, v in data.items() if v not in ("", None)} if isinstance(data, dict) else {}


def _default_settings() -> Dict[str, Any]:
    """The default profile's settings, which a named profile falls back to; {} in default itself."""
    if get_hermes_home() == get_default_hermes_root():
        return {}
    try:
        return _settings_in(safe_load(_profile_config("default").read_text(encoding="utf-8")))
    except (OSError, YAMLError):
        return {}


def _settings() -> Dict[str, Any]:
    """The requesting profile's settings over the default profile's (see plugin.yaml): a value set
    once in default applies to every profile that leaves it unset. Only this dashboard reads these;
    each gateway's hooks (alerts, automatic fix cards) read their own profile's, so nothing is
    posted or filed twice."""
    return {**_default_settings(), **_settings_in(load_config())}


def _glab_lines() -> List[str]:
    try:
        return _GLAB_CONFIG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _gitlab_host() -> str:
    """GITLAB_HOST, else the gitlab_host setting, else glab's default "host:"."""
    host = os.environ.get("GITLAB_HOST") or _settings().get("gitlab_host")
    if host:
        return host
    for line in _glab_lines():
        if line.startswith("host:"):
            return line[len("host:"):].strip().strip("\"'")
    return ""


def _gitlab_token() -> str:
    """The gitlab_token setting, else GITLAB_TOKEN, else glab's token for the host."""
    tok = get_secret("OPERATIONS_GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN", "")
    if tok:
        return tok
    host = _gitlab_host()
    if not host:
        return ""
    lines = _glab_lines()
    # No PyYAML in the dashboard's bundled Python: find "<host>:" under "hosts:", then the
    # first "token:" indented deeper than it, stopping where the host's block ends.
    host_indent = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if host_indent is None:
            if indent > 0 and stripped == f"{host}:":
                host_indent = indent
        elif indent <= host_indent:
            break
        elif stripped.startswith("token:"):
            return stripped[len("token:"):].strip().strip("\"'")
    return ""


def _gitlab(path: str, body: Optional[Dict[str, Any]] = None) -> str:
    """GET, or POST with a JSON body (the rule proposal's only writes)."""
    host, tok = _gitlab_host(), _gitlab_token()
    if not host or not tok:
        raise RuntimeError("no GitLab login: run `glab auth login`, or set GITLAB_HOST and GITLAB_TOKEN")
    headers = {"PRIVATE-TOKEN": tok}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://{host}/api/v4/{path}", headers=headers,
                                 data=None if body is None else json.dumps(body).encode("utf-8"))
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def merged_mrs(since: float) -> List[Dict[str, Any]]:
    qs = urllib.parse.urlencode({
        "scope": "all", "state": "merged", "order_by": "updated_at", "sort": "desc", "per_page": "100",
        "updated_after": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))})
    mrs = json.loads(_gitlab(f"merge_requests?{qs}"))
    out = []
    for mr in mrs:
        ts = datetime.fromisoformat((mr.get("merged_at") or "").replace("Z", "+00:00")).timestamp() \
            if mr.get("merged_at") else None
        if ts is None or ts < since:
            continue
        out.append({"ts": ts, "kind": "merged", "text": mr["title"], "where": mr["references"]["full"],
                    "url": mr["web_url"], "by": (mr.get("merged_by") or mr.get("author") or {}).get("username")})
    return out


def model_changes(m: sqlite3.Connection, since: float) -> List[Dict[str, Any]]:
    """Loads and unloads from consecutive pool snapshots, one entry per model per device."""
    snaps: Dict[str, Dict[int, set]] = {}
    for minute, device, model in m.execute(
            "SELECT CAST(ts/60 AS INTEGER), device, model FROM records WHERE kind='pool_status' AND ts>=?",
            (since - POOL_GAP_MIN * 60,)):
        snaps.setdefault(device, {}).setdefault(minute, set()).add(re.sub(r":\d+$", "", model or ""))
    # (device, model) -> [(loaded_at or None if loaded before the window, unloaded_at or None if still loaded)]
    spans: Dict[tuple, List[tuple]] = {}
    for device, by_minute in snaps.items():
        loaded: Dict[str, float] = {}
        prev, prev_minute = None, None
        for minute in sorted(by_minute):
            cur = by_minute[minute]
            if prev is not None and minute - prev_minute <= POOL_GAP_MIN:
                for name in cur - prev:
                    loaded[name] = minute * 60
                for name in prev - cur:
                    spans.setdefault((device, name), []).append((loaded.pop(name, None), minute * 60))
            elif prev is not None:
                loaded.clear()
            prev, prev_minute = cur, minute
        for name, start in loaded.items():
            spans.setdefault((device, name), []).append((start, None))
    out = []
    for (device, name), ss in spans.items():
        ss = [(a, b) for a, b in ss if (a or b) >= since]
        if not ss:
            continue
        loads = [(a, b) for a, b in ss if a is not None]
        last_a, last_b = ss[-1]
        if len(loads) > 1:
            text = f"{name} loaded {len(loads)} times" + (", loaded now" if last_b is None else "")
        elif loads:
            a, b = loads[0]
            text = f"{name} loaded" + (f" for {_dur(b - a)}" if b is not None else "")
        else:
            text = f"{name} unloaded"
        out.append({"ts": last_a or last_b, "kind": "model", "text": text, "where": device, "count": len(ss)})
    return out


def _dur(s: float) -> str:
    if s < 59.95:
        return f"{s:.1f}s"
    m = int(s // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


@router.get("/changes")
def changes(hours: int = Query(24, ge=1, le=168)) -> Dict[str, Any]:
    now = time.time()
    since = now - hours * 3600
    items: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    try:
        items += merged_mrs(since)
    except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        errors["gitlab"] = str(exc)
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, errors, "models")
    try:
        items += model_changes(m, since)
    except sqlite3.Error as exc:
        errors["models"] = str(exc)
    finally:
        m.close()
    items.sort(key=lambda i: -i["ts"])
    return {"generated_at": now, "hours": hours, "items": items[:CHANGES_LIMIT], "errors": errors}



def _iso(ts: Optional[str]) -> Optional[float]:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else None


# A stage is failed if any job in it failed (allow_failure aside), running while any job has not
# finished, passed when every job that ran passed, and skipped when none ran.
_JOB_STATE = {"success": "ok", "failed": "fail", "running": "run", "pending": "run", "created": "run",
              "preparing": "run", "waiting_for_resource": "run", "scheduled": "run"}


def pipeline_stages(jobs: List[Dict[str, Any]]) -> List[List[str]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for j in sorted(jobs, key=lambda j: j["id"]):
        latest[j["name"]] = j  # a retried job replaces its earlier run
    stages: Dict[str, List[str]] = {}
    for j in sorted(latest.values(), key=lambda j: j["id"]):
        st = _JOB_STATE.get(j["status"], "skip")
        stages.setdefault(j["stage"], []).append("ok" if st == "fail" and j.get("allow_failure") else st)
    return [[name, next((s for s in ("fail", "run", "ok") if s in sts), "skip")] for name, sts in stages.items()]


def _mr_pipeline(mr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pid, iid = mr["project_id"], mr["iid"]
    pipes = json.loads(_gitlab(f"projects/{pid}/merge_requests/{iid}/pipelines?per_page=1"))
    if not pipes:
        return None
    p = pipes[0]
    jobs = json.loads(_gitlab(f"projects/{pid}/pipelines/{p['id']}/jobs?per_page=100&include_retried=true"))
    return {"status": p["status"], "url": p.get("web_url"), "stages": pipeline_stages(jobs)}


@router.get("/mrs")
def merge_requests(hours: int = Query(24, ge=1, le=168)) -> Dict[str, Any]:
    """Open merge requests and those merged in the window, each with its newest pipeline's stages."""
    now = time.time()
    since = now - hours * 3600
    errors: Dict[str, str] = {}
    items: List[Dict[str, Any]] = []
    try:
        base = {"scope": "all", "order_by": "updated_at", "sort": "desc", "per_page": str(MRS_LIMIT)}
        opened = json.loads(_gitlab("merge_requests?" + urllib.parse.urlencode({**base, "state": "opened"})))
        merged = json.loads(_gitlab("merge_requests?" + urllib.parse.urlencode({
            **base, "state": "merged", "updated_after": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))})))
    except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        return {"generated_at": now, "hours": hours, "items": [], "errors": {"gitlab": str(exc)}}
    mrs = opened + [mr for mr in merged if (_iso(mr.get("merged_at")) or 0) >= since]
    # one GitLab round trip per MR for its pipeline and one for its jobs; run them side by side
    with ThreadPoolExecutor(max_workers=8) as pool:
        # get_secret reads the profile from a context variable, which worker threads do not inherit
        futures = [pool.submit(contextvars.copy_context().run, _mr_pipeline, mr) for mr in mrs]
    failed = 0
    for mr, fut in zip(mrs, futures):
        lost = False
        try:
            pipe = fut.result()
        except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            pipe, failed, lost = None, failed + 1, True
            errors["pipelines"] = str(exc)
        ref = mr["references"]["full"]
        items.append({
            "id": ref, "iid": mr["iid"], "repo": ref.rsplit("!", 1)[0], "title": mr["title"], "url": mr["web_url"],
            "state": "merged" if mr["state"] == "merged" else "draft" if mr.get("draft") else "open",
            "author": (mr.get("author") or {}).get("username"),
            "reviewers": [r["username"] for r in mr.get("reviewers") or []],
            "merged_by": (mr.get("merged_by") or {}).get("username"),
            "created": _iso(mr.get("created_at")), "updated": _iso(mr.get("updated_at")),
            "merged": _iso(mr.get("merged_at")), "conflicts": bool(mr.get("has_conflicts")),
            "pipeline": pipe, "pipeline_unavailable": lost})
    if failed:
        errors["pipelines"] = f"{failed} of {len(mrs)} pipelines unavailable: {errors['pipelines']}"
    return {"generated_at": now, "hours": hours, "items": items, "errors": errors}


def request_flow(m: sqlite3.Connection, since: float, until: float) -> Dict[str, Any]:
    # profile and provider came with later collectors; an older metrics.db reads them as unknown.
    have = {r[1] for r in m.execute("PRAGMA table_info(records)")}
    profile, provider = (c if c in have else "NULL" for c in ("profile", "provider"))
    # A failed call is recorded as kind='error'; those from a client (with a platform) count as
    # requests that failed. The router's own errors have no platform and stay out of Flow.
    rows = m.execute(f"SELECT ts, platform, {profile}, model, device, {provider}, duration_s,"
                     " kind='error' OR COALESCE(error, '')!=''"
                     " FROM records WHERE (kind='api' OR (kind='error' AND COALESCE(platform, '')!='')) AND ts>=? AND ts<?",
                     (since, until)).fetchall()
    now = min(until, time.time())
    start = since - since % 3600
    # a window that ends on the hour (one hour from the Today strip) has no bucket after it
    hourly = [{"ts": start + h * 3600, "by": {}} for h in range(max(1, -int((start - now) // 3600)))]
    hour_paths: List[Dict[tuple, int]] = [{} for _ in hourly]
    nodes: Dict[str, Dict[str, Any]] = {}
    durations: Dict[str, List[float]] = {}
    paths: Dict[tuple, int] = {}
    for ts, platform, prof, model, device, prov, dur, err in rows:
        labels = (platform or "unknown", prof or "no profile", model or "no model", device or prov or "unknown")
        ids = [f"{c}:{label}" for c, label in zip(FLOW_COLUMNS, labels)]
        for col, label, nid in zip(FLOW_COLUMNS, labels, ids):
            n = nodes.setdefault(nid, {"id": nid, "col": col, "label": label, "requests": 0, "errors": 0})
            n["requests"] += 1
            n["errors"] += bool(err)
            if dur is not None:
                durations.setdefault(nid, []).append(dur)
        nodes[ids[-1]]["pool_device"] = bool(device)
        paths[tuple(ids)] = paths.get(tuple(ids), 0) + 1
        h = min(len(hourly) - 1, int((ts - start) // 3600))
        by = hourly[h]["by"]
        by[ids[-1]] = by.get(ids[-1], 0) + 1
        hour_paths[h][tuple(ids)] = hour_paths[h].get(tuple(ids), 0) + 1
    for nid, n in nodes.items():
        n["p95_s"] = _p95(durations.get(nid, []))
    ordered = sorted(paths.items(), key=lambda p: -p[1])
    # each hour's requests by path (its index in "paths"), so the page can chart any selection over time
    index = {ids: i for i, (ids, _) in enumerate(ordered)}
    for hour, counts in zip(hourly, hour_paths):
        hour["paths"] = {index[ids]: n for ids, n in counts.items()}
    return {"total": len(rows),
            "nodes": sorted(nodes.values(), key=lambda n: (FLOW_COLUMNS.index(n["col"]), -n["requests"], n["label"])),
            "paths": [{"ids": list(ids), "requests": n} for ids, n in ordered],
            "hourly": hourly}


def _window(hours: int, start: Optional[float]) -> tuple:
    """The last `hours` (open-ended, so a host whose clock runs ahead still counts), or the `hours` from `start`."""
    return (time.time() - hours * 3600, math.inf) if start is None else (start, start + hours * 3600)


@router.get("/flow")
def flow(hours: int = Query(24, ge=1, le=168), start: Optional[float] = None) -> Dict[str, Any]:
    since, until = _window(hours, start)
    out: Dict[str, Any] = {"generated_at": time.time(), "hours": hours, "start": start, "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    try:
        out.update(request_flow(m, since, until))
    except sqlite3.Error as exc:
        out["errors"]["flow"] = str(exc)
    finally:
        m.close()
    return out


def error_groups(m: sqlite3.Connection, since: float, until: float) -> List[Dict[str, Any]]:
    """The window's failed calls grouped by what failed and where, most first, newest session first."""
    groups: Dict[tuple, Dict[str, Any]] = {}
    for r in m.execute(f"SELECT ts, {_cols(m, 'host', 'device', 'provider', 'platform', 'profile', 'model', 'event', 'error', 'session_id')}"
                       " FROM records WHERE kind='error' AND ts>=? AND ts<? ORDER BY ts DESC", (since, until)):
        served = r["device"] or r["provider"]
        key = (r["host"], r["platform"], r["profile"], r["model"], served, r["event"], r["error"])
        g = groups.setdefault(key, {"host": r["host"], "platform": r["platform"], "profile": r["profile"], "model": r["model"],
                                    "served": served, "event": r["event"], "error": r["error"], "count": 0,
                                    "first": r["ts"], "last": r["ts"], "sessions": []})
        g["count"] += 1
        g["first"] = r["ts"]
        if r["session_id"] and r["session_id"] not in g["sessions"]:
            g["sessions"].append(r["session_id"])
    return sorted(groups.values(), key=lambda g: -g["count"])


@router.get("/errors")
def errors_route(hours: int = Query(1, ge=1, le=168), start: Optional[float] = None) -> Dict[str, Any]:
    since, until = _window(hours, start)
    out: Dict[str, Any] = {"generated_at": time.time(), "hours": hours, "start": start, "groups": [], "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    try:
        out["groups"] = error_groups(m, since, until)
    except sqlite3.Error as exc:
        out["errors"]["errors"] = str(exc)
    finally:
        m.close()
    return out


def _cols(m: sqlite3.Connection, *cols: str) -> str:
    """The columns as a select list; ones an older metrics.db lacks read as NULL."""
    have = {r[1] for r in m.execute("PRAGMA table_info(records)")}
    return ", ".join(c if c in have else f"NULL AS {c}" for c in cols)


def sessions(m: sqlite3.Connection, since: float, until: float) -> Dict[str, Any]:
    """The window's sessions that made model calls, newest first, each described whole (a session that began
    before the window counts all its calls, as opening it shows them). A call's ts is when it ended."""
    cols = _cols(m, "session_id", "kind", "ts", "duration_s", "platform", "profile", "model", "error",
                 "input_tokens", "output_tokens")
    rows = [dict(r) for r in m.execute(
        f"SELECT session_id, MIN(ts - COALESCE(duration_s, 0)) AS start, MAX(ts) AS end, COUNT(*) AS calls,"
        f" SUM(kind='error' OR COALESCE(error, '')!='') AS errors,"
        f" SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, GROUP_CONCAT(DISTINCT model) AS models"
        f" FROM (SELECT {cols} FROM records WHERE kind IN ('api', 'error') AND session_id IN (SELECT session_id"
        f" FROM records WHERE kind IN ('api', 'error') AND ts>=? AND ts<? AND COALESCE(session_id, '')!=''))"
        f" GROUP BY session_id ORDER BY end DESC LIMIT ?", (since, until, TRACE_SESSIONS + 1))]
    if rows:
        ids = [r["session_id"] for r in rows]
        marks = ",".join("?" * len(ids))
        parents = dict(m.execute(f"SELECT session_id, parent_session_id FROM records WHERE kind='subagent_stop'"
                                 f" AND session_id IN ({marks})", ids).fetchall())
        kids = dict(m.execute(f"SELECT parent_session_id, COUNT(*) FROM records WHERE kind='subagent_stop'"
                              f" AND parent_session_id IN ({marks}) GROUP BY 1", ids).fetchall())
        seen: Dict[str, list] = {}
        for x in m.execute(f"SELECT session_id, {_cols(m, 'platform', 'profile')} FROM records WHERE kind IN ('api', 'error')"
                           f" AND session_id IN ({marks}) ORDER BY ts", ids):
            seen.setdefault(x["session_id"], []).append(x)
        for r in rows:
            r.update(_where(seen.get(r["session_id"], [])))
            r["models"] = sorted(set((r["models"] or "").split(",")) - {""})
            r["parent"], r["subagents"] = parents.get(r["session_id"]), kids.get(r["session_id"], 0)
    return {"sessions": rows[:TRACE_SESSIONS], "truncated": len(rows) > TRACE_SESSIONS}


def _where(rows) -> Dict[str, Any]:
    """A session's platforms and profiles in order of first use: one opened in Desktop after cron began it ran on both."""
    return {k: " then ".join(dict.fromkeys(r[k] for r in rows if r[k])) or None for k in ("platform", "profile")}


def session_trace(m: sqlite3.Connection, sid: str) -> Dict[str, Any]:
    """One session's calls in order (failed ones included), its subagents, and its parent."""
    cols = _cols(m, "kind", "ts", "duration_s", "host", "turn_id", "platform", "profile", "model", "device", "provider",
                 "ttft_s", "tps", "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens",
                 "finish_reason", "error")
    rows = m.execute(f"SELECT {cols} FROM records WHERE session_id=? AND kind IN ('api', 'error') ORDER BY ts LIMIT ?",
                     (sid, TRACE_CALLS + 1)).fetchall()
    turns: Dict[str, int] = {}
    calls = [{"start": r["ts"] - (r["duration_s"] or 0), "end": r["ts"], "duration_s": r["duration_s"],
              "turn": turns.setdefault(r["turn_id"] or "", len(turns) + 1), "model": r["model"],
              "served": r["device"] or r["provider"] or "unknown", "pool_device": bool(r["device"]), "host": r["host"],
              "ttft_s": r["ttft_s"], "tps": r["tps"], "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
              "cache_read_tokens": r["cache_read_tokens"], "reasoning_tokens": r["reasoning_tokens"],
              "finish_reason": r["finish_reason"], "error": r["error"] or (None if r["kind"] == "api" else "failed")}
             for r in rows[:TRACE_CALLS]]
    pcol = _cols(m, "profile")
    parent = m.execute("SELECT parent_session_id FROM records WHERE kind='subagent_stop' AND session_id=? LIMIT 1",
                       (sid,)).fetchone()
    subagents = [{"session_id": r["session_id"], "profile": r["profile"], "label": r["label"],
                  "start": r["ts"] - (r["duration_s"] or 0), "end": r["ts"], "duration_s": r["duration_s"],
                  "calls": m.execute("SELECT COUNT(*) FROM records WHERE kind='api' AND session_id=?",
                                     (r["session_id"],)).fetchone()[0]}
                 for r in m.execute(f"SELECT session_id, ts, duration_s, label, {pcol} FROM records"
                                    " WHERE kind='subagent_stop' AND parent_session_id=? ORDER BY ts", (sid,))]
    return {"session_id": sid, **_where(rows),
            "parent": parent[0] if parent else None, "calls": calls, "truncated": len(rows) > TRACE_CALLS,
            "subagents": subagents, "why": _why(m, sid, calls)}


def _why(m: sqlite3.Connection, sid: str, calls: List[Dict[str, Any]]) -> List[str]:
    """Where a session's time went: model calls against the gaps between them, and calls slower than usual.

    A call's usual time is the median of calls like it as of when it ran: the same model on the same server,
    with half to twice its output tokens (output length is most of a call's time), that ended in the week
    before it started, in other sessions (a slow session does not set its own baseline)."""
    if not calls:
        return []
    models = sorted({c["model"] for c in calls if c["model"]})
    history: Dict[tuple, List[tuple]] = {}  # (model, served) -> [(ts, output_tokens, duration_s)] by ts
    if models:
        marks = ",".join("?" * len(models))
        for model, served, ts, out_t, dur in m.execute(
                f"SELECT model, COALESCE(device, provider, 'unknown'), ts, output_tokens, duration_s FROM (SELECT"
                f" {_cols(m, 'kind', 'ts', 'session_id', 'model', 'device', 'provider', 'output_tokens', 'duration_s')}"
                f" FROM records WHERE kind='api' AND ts>=? AND ts<? AND model IN ({marks})"
                f" AND COALESCE(session_id, '')!=?) WHERE duration_s IS NOT NULL AND output_tokens IS NOT NULL ORDER BY ts",
                (min(c["start"] for c in calls) - 7 * 86400, max(c["start"] for c in calls), *models, sid)):
            history.setdefault((model, served), []).append((ts, out_t, dur))
    for c in calls:
        rows, o = history.get((c["model"], c["served"]), []), c["output_tokens"]
        times = [r[0] for r in rows]
        durs = [] if o is None else [d for _, t, d in rows[bisect.bisect_left(times, c["start"] - 7 * 86400):
                                                            bisect.bisect_left(times, c["start"])]
                                     if o / 2 <= t <= 2 * max(o, 1)]
        c["usual_n"] = len(durs) if o is not None else None
        c["usual_s"] = statistics.median(durs) if len(durs) >= USUAL_MIN_CALLS else None
    out = []
    span = max(c["end"] for c in calls) - min(c["start"] for c in calls)
    busy = sum(c["duration_s"] or 0 for c in calls)
    if span > 0:
        out.append(f"Model calls took {_dur(busy)} of the session's {_dur(span)} ({round(100 * min(busy, span) / span)}%);"
                   " the rest was between calls: tools, a person, or waiting to be picked up.")
    gaps = [(b["start"] - a["end"], i + 2) for i, (a, b) in enumerate(zip(calls, calls[1:]))]
    gap, at = max(gaps, default=(0, 0))
    if gap >= SLOW_MIN_S:
        c = calls[at - 1]
        out.append(f"The longest wait was {_dur(gap)}, before call {c['turn']}.{at}.")
    slow = []
    for i, c in enumerate(calls, 1):
        med = c["usual_s"]
        if med and c["duration_s"] and c["duration_s"] >= SLOW_MIN_S and c["duration_s"] >= SLOW_FACTOR * med:
            slow.append((c["duration_s"] / med, i, c, med))
    for ratio, i, c, med in sorted(slow, key=lambda x: -x[0])[:3]:
        out.append(f"Call {c['turn']}.{i} took {_dur(c['duration_s'])}, {ratio:.1f}x the usual {_dur(med)}"
                   f" for {c['model']} on {c['served']} at that output length"
                   + (f"; its first token came after {_dur(c['ttft_s'])}." if c["ttft_s"] is not None else "."))
    failed = sum(bool(c["error"]) for c in calls)
    if failed:
        out.append(f"{failed} call{'s' if failed != 1 else ''} failed.")
    return out


@router.get("/trace")
def trace(hours: int = Query(24, ge=1, le=168), start: Optional[float] = None) -> Dict[str, Any]:
    since, until = _window(hours, start)
    out: Dict[str, Any] = {"generated_at": time.time(), "hours": hours, "start": start, "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    try:
        out.update(sessions(m, since, until))
    except sqlite3.Error as exc:
        out["errors"]["trace"] = str(exc)
    finally:
        m.close()
    return out


@router.get("/trace/{session_id}")
def trace_session(session_id: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    try:
        out.update(session_trace(m, session_id))
    except sqlite3.Error as exc:
        out["errors"]["trace"] = str(exc)
    finally:
        m.close()
    return out


def find_session(m: sqlite3.Connection, prefix: str, before: float) -> Optional[Dict[str, Any]]:
    """The session a health finding names by the start of its id (findings cut ids short, and a cron job's runs
    share a start): of those begun by the finding, the one active last. A range on the id keeps to its index."""
    r = m.execute("SELECT session_id, MIN(ts) AS start, MAX(ts) AS end FROM records WHERE kind IN ('api', 'error')"
                  " AND session_id >= ? AND session_id < ? GROUP BY session_id HAVING start <= ? ORDER BY end DESC LIMIT 1",
                  (prefix, prefix + "\uffff", before)).fetchone()
    return dict(r) if r else None


@router.get("/find-session")
def find_session_route(prefix: str = Query(..., min_length=8), before: float = Query(...)) -> Dict[str, Any]:
    out: Dict[str, Any] = {"session": None, "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    try:
        out["session"] = find_session(m, prefix, before)
    except sqlite3.Error as exc:
        out["errors"]["trace"] = str(exc)
    finally:
        m.close()
    return out


def topology(m: sqlite3.Connection, since: float, now: float) -> Dict[str, Any]:
    """Hosts with their gateways, and what each host's requests were served by over the window."""
    h = hosts(m, now)
    aliases = _device_aliases()
    gateways = [{"host": r["host"], "platform": r["platform"], "state": r["label"], "error": r["error"], "ts": r["ts"]}
                for r in m.execute("SELECT host, platform, label, error, MAX(ts) AS ts FROM records"
                                   " WHERE kind='gateway_status' AND ts>=? GROUP BY host, platform ORDER BY host, platform",
                                   (now - 86400,)) if r["label"] != STALE_GATEWAY]
    by: Dict[tuple, Dict[str, Any]] = {}
    for host, served, pool_device, model, n, failed in m.execute(
            f"SELECT host, COALESCE(device, provider, 'unknown'), device IS NOT NULL, COALESCE(model, 'no model'),"
            f" COUNT(*), SUM(COALESCE(error, '')!='') FROM (SELECT {_cols(m, 'host', 'device', 'provider', 'model', 'error')}"
            f" FROM records WHERE kind='api' AND ts>=?) GROUP BY 1, 2, 3, 4", (since,)):
        e = by.setdefault((host, served), {"host": host, "served": served, "pool_device": False, "requests": 0,
                                           "errors": 0, "models": {}})
        e["pool_device"] |= bool(pool_device)
        e["requests"], e["errors"] = e["requests"] + n, e["errors"] + failed
        e["models"][model] = e["models"].get(model, 0) + n
    edges = sorted(by.values(), key=lambda e: -e["requests"])
    # A host whose newest conformance report fails a check has drifted from its standard.
    drift: Dict[str, List[str]] = {}
    try:
        for r in host_results(m, {}, now):
            failing = [rule for rule, res in r["results"].items() if res["status"] == "fail"]
            if failing:
                drift[r["host"]] = sorted(failing)
    except sqlite3.Error:
        pass  # no conformance_results table: this install does not run host checks
    stats = {x["host"]: x for x in h["hosts"]}
    names = sorted((set(stats) | {g["host"] for g in gateways} | {e["host"] for e in edges}) - {None})
    pool = {p["device"]: p["models"] for p in h["pool"]}
    served: Dict[str, Dict[str, Any]] = {d: {"id": d, "pool_device": True, "live": True, "models": ms} for d, ms in pool.items()}
    for e in edges:
        served.setdefault(e["served"], {"id": e["served"], "pool_device": e["pool_device"], "live": False, "models": []})
    return {"hosts": [{**stats.get(n, {"host": n, "last_seen": None, "stale": True}), "stats": n in stats,
                       "device": aliases.get(n), "drift": drift.get(n, [])} for n in names],
            "gateways": gateways, "edges": edges,
            "served": sorted(served.values(), key=lambda s: (not s["pool_device"], s["id"]))}


def _presence(times: List[float], gap: float, now: float) -> List[list]:
    """[[ts, up]] for a node that reports every so often: up at its first report, down `gap` after
    a report with no next one, up again at the next."""
    out: List[list] = []
    for prev, ts in zip([None] + times, times):
        if prev is None or ts - prev > gap:
            if prev is not None:
                out.append([prev + gap, False])
            out.append([ts, True])
    if times and now - times[-1] > gap:
        out.append([times[-1] + gap, False])
    return out


def topology_timeline(m: sqlite3.Connection, since: float, now: float) -> Dict[str, Any]:
    """What the page needs to draw the map as of any time in the window: requests per bucket, each
    node's states with the one in force at the window's start, and the changes inside the window."""
    step = 600 if now - since <= 86400 else 3600
    buckets = [list(r) for r in m.execute(
        f"SELECT CAST(ts/{step} AS INTEGER)*{step}, host, COALESCE(device, provider, 'unknown'), device IS NOT NULL,"
        f" COALESCE(model, 'no model'), COUNT(*), SUM(COALESCE(error, '')!='') FROM (SELECT ts,"
        f" {_cols(m, 'host', 'device', 'provider', 'model', 'error')} FROM records WHERE kind='api' AND ts>=?)"
        f" GROUP BY 1, 2, 3, 4, 5 ORDER BY 1", (since,))]
    # The same requests by entry point and profile, for the layouts that draw entry point to profile to role
    # alias to what served it: a request whose model is a router role went through the router. As in Flow,
    # a client's failed calls (kind='error' with a platform) count as requests that failed.
    flow = [list(r) for r in m.execute(
        f"SELECT CAST(ts/{step} AS INTEGER)*{step}, host, COALESCE(platform, 'unknown'), COALESCE(profile, 'default'),"
        f" COALESCE(model, 'no model'), COALESCE(device, provider, 'unknown'), device IS NOT NULL, COUNT(*),"
        f" SUM(kind='error' OR COALESCE(error, '')!=''), SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) FROM (SELECT ts, kind,"
        f" {_cols(m, 'host', 'platform', 'profile', 'device', 'provider', 'model', 'error', 'input_tokens', 'output_tokens')}"
        f" FROM records WHERE (kind='api' OR (kind='error' AND COALESCE(platform, '')!='')) AND ts>=?) GROUP BY 1, 2, 3, 4, 5, 6, 7 ORDER BY 1", (since,))]
    harness = [list(r) for r in m.execute(
        f"SELECT CAST(ts/{step} AS INTEGER)*{step}, host, COALESCE(model, 'no model'), COUNT(*) FROM records"
        f" WHERE kind='harness_sample' AND ts>=? GROUP BY 1, 2, 3 ORDER BY 1", (since,))]
    states: Dict[str, List[list]] = {}
    changes: List[Dict[str, Any]] = []

    def track(node: str, series: List[list], text, tone):
        """Keep the state in force at `since` and every change after it."""
        kept = [s for s in series if s[0] < since][-1:] + [s for s in series if s[0] >= since]
        if kept:
            states[node] = kept
        for (_, old), (ts, new) in zip(kept, kept[1:]):
            changes.append({"ts": ts, "node": node, "text": text(new, old), "tone": tone(new), "state": new})

    rows: Dict[tuple, List[list]] = {}
    for r in m.execute("SELECT ts, host, platform, label FROM records WHERE kind='gateway_status' AND ts>=?"
                       " ORDER BY ts", (since - 86400,)):
        s = rows.setdefault((r["host"], r["platform"]), [])
        if not s or s[-1][1] != r["label"]:
            s.append([r["ts"], r["label"]])
    verb = lambda new: new if new in ("connected", "stopped") else f"went {new}"
    for (host, platform), s in rows.items():
        if s[-1][1] == STALE_GATEWAY:
            continue
        track(f"g:{host}:{platform}", s, lambda new, old, h=host, p=platform: f"{p} on {h} {verb(new)}",
              lambda new: "good" if new == "connected" else "bad")
    # Only gateway changes so far, each node's in time order: fold a stop and what followed into a restart.
    gateway_changes, changes[:] = list(changes), []
    for c in gateway_changes:
        last = changes[-1] if changes else None
        if last and last["node"] == c["node"] and last["state"] == "stopped" and c["ts"] - last["ts"] <= RESTART_S:
            where = c["text"][:-len(verb(c["state"]))]
            changes[-1] = {**c, "text": f"{where}restarted" if c["state"] == "connected" else f"{where}restarted and {verb(c['state'])}",
                           "tone": "info" if c["state"] == "connected" else c["tone"]}
        else:
            changes.append(c)
    seen: Dict[str, List[float]] = {}
    for host, ts in m.execute("SELECT host, ts FROM records WHERE kind='host_stats' AND ts>=? ORDER BY ts",
                              (since - STALE_S,)):
        seen.setdefault(host, []).append(ts)
    for host, times in seen.items():
        track(f"h:{host}", _presence(times, STALE_S, now),
              lambda new, old, h=host: f"{h} {'is reporting again' if new else 'stopped reporting'}",
              lambda new: "good" if new else "warn")
    seen = {}
    for device, minute in m.execute("SELECT device, CAST(ts/60 AS INTEGER) FROM records WHERE kind='pool_status'"
                                    " AND ts>=? GROUP BY 1, 2 ORDER BY 2", (since - POOL_GAP_MIN * 60,)):
        seen.setdefault(device, []).append(minute * 60)
    for device, times in seen.items():
        # Pool members report every minute. A lone snapshot with no report a minute either side is a
        # one-off writer using another name for the device, not a device joining and leaving.
        times = [t for i, t in enumerate(times)
                 if (i and t - times[i - 1] <= 60) or (i + 1 < len(times) and times[i + 1] - t <= 60)]
        if not times:
            continue
        track(f"s:{device}", _presence(times, POOL_GAP_MIN * 60, now),
              lambda new, old, d=device: f"{d} {'joined' if new else 'left'} the pool",
              lambda new: "good" if new else "warn")
    if m.execute("SELECT 1 FROM sqlite_master WHERE name='conformance_results'").fetchone():
        reports: Dict[str, Dict[float, List[str]]] = {}
        for r in m.execute("SELECT host, ts, rule, status FROM conformance_results WHERE ts>=? ORDER BY ts",
                           (since - 86400,)):
            failing = reports.setdefault(r["host"], {}).setdefault(r["ts"], [])
            if r["status"] == "fail":
                failing.append(r["rule"])
        for host, by_ts in reports.items():
            s: List[list] = []
            for ts, failing in by_ts.items():
                if not s or s[-1][1] != sorted(failing):
                    s.append([ts, sorted(failing)])
            track(f"d:{host}", s,
                  lambda new, old, h=host: f"{h} drifted: {', '.join(new)} failing" if new else f"{h} conforms again",
                  lambda new: "drift" if new else "good")
    changes.sort(key=lambda c: -c["ts"])
    return {"step": step, "since": since, "buckets": buckets, "flow": flow, "harness": harness, "states": states,
            "changes": changes[:TIMELINE_CHANGES], "more_changes": max(0, len(changes) - TIMELINE_CHANGES)}


def router_roles() -> Dict[str, Any]:
    """The model router's role aliases: each role's candidates in order, which are live, and the one it
    resolves to now. Found the way the router's own plugin finds it: MODEL_ROUTER_URL (the router's
    default address otherwise), and MODEL_ROUTER_TOKEN / ROUTER_TOKEN or <hermes root>/model-router/router.token.
    An install without the router gets the error and the page keeps to observed traffic.
    """
    url = os.environ.get("MODEL_ROUTER_URL", "http://127.0.0.1:8867").rstrip("/")
    tok = os.environ.get("MODEL_ROUTER_TOKEN") or os.environ.get("ROUTER_TOKEN") or ""
    if not tok:
        try:
            tok = (get_default_hermes_root() / "model-router" / "router.token").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    req = urllib.request.Request(f"{url}/router/status", headers={"Authorization": f"Bearer {tok}"} if tok else {})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            snap = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"url": url, "error": str(exc), "roles": []}
    return {"url": url, "roles": [
        {"name": name, "strict": bool(r.get("strict")), "resolved": r.get("resolved"),
         "candidates": [{"model": c["id"], "live": bool(c.get("live")), "fit": c.get("fit") is not False}
                        for c in r.get("candidates_detail") or []]}
        for name, r in sorted((snap.get("roles") or {}).items())]}


@router.get("/topology")
def topology_route(hours: int = Query(24, ge=1, le=168)) -> Dict[str, Any]:
    now = time.time()
    out: Dict[str, Any] = {"generated_at": now, "hours": hours, "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    try:
        out.update(topology(m, now - hours * 3600, now))
    except sqlite3.Error as exc:
        out["errors"]["topology"] = str(exc)
    try:
        out["timeline"] = topology_timeline(m, now - hours * 3600, now)
    except sqlite3.Error as exc:
        out["errors"]["replay"] = str(exc)
    finally:
        m.close()
    out["router"] = router_roles()
    return out


def _transitions(rows, since: float):
    """(ts, key, old, new, row) for each row whose value differs from the previous one for its key.

    rows are ordered by key, then ts, and may start before since: those set the baseline only."""
    last: Dict[Any, Any] = {}
    for ts, key, value, row in rows:
        if key in last and last[key] != value and ts >= since:
            yield ts, key, last[key], value, row
        elif key not in last and ts >= since:
            yield ts, key, None, value, row
        last[key] = value


def activity_log(m: sqlite3.Connection, k: sqlite3.Connection, since: float, errors: Dict[str, str]) -> Dict[str, Any]:
    raw: List[tuple] = []  # (ts, category, kind, text, where, raw line)
    try:
        raw += [(r["ts"], "incident", "error", r["error"] or f"{r['event']} failed", r["device"] or r["host"],
                 f"{r['event']} · {r['model'] or 'no model'} · {r['error'] or 'no error text'}")
                for r in m.execute("SELECT ts, host, device, event, model, error FROM records WHERE kind='error'"
                                   " AND ts>=? ORDER BY ts DESC LIMIT ?", (since, ACTIVITY_SCAN))]
        gone = {r[0] for r in m.execute("SELECT platform, label, MAX(ts) FROM records WHERE kind='gateway_status'"
                                        " AND ts>=? GROUP BY platform", (since - 86400,)) if r[1] == STALE_GATEWAY}
        gw = m.execute("SELECT ts, platform, label, error FROM records WHERE kind='gateway_status' AND ts>=?"
                       " ORDER BY platform, ts", (since - 86400,))
        for ts, platform, old, new, r in _transitions(((r["ts"], r["platform"], r["label"], r) for r in gw
                                                       if r["platform"] not in gone), since):
            if old is None and new == "connected":
                continue
            raw.append((ts, "incident", "gateway", f"{platform} {'was' if old is None else 'went'} {new}", None,
                        f"{old or 'no earlier state'} -> {new}{': ' + r['error'] if r['error'] else ''}"))
    except sqlite3.Error as exc:
        errors["incidents"] = str(exc)
    try:
        if m.execute("SELECT 1 FROM sqlite_master WHERE name='conformance_results'").fetchone():
            cr = m.execute("SELECT ts, host, rule, status, value, verified FROM conformance_results WHERE ts>=?"
                           " ORDER BY host, rule, ts", (since - 86400,))
            for ts, (h, rule), old, new, r in _transitions(
                    ((r["ts"], (r["host"], r["rule"]), r["status"], r) for r in cr), since):
                if old is None and new in ("pass", "na"):
                    continue
                raw.append((ts, "conformance", "host check", f"{rule} {'is' if old is None else f'{old} ->'} {new}", h,
                            f"{r['value'] or new}{'' if r['verified'] else ' (unverified sender)'}"))
    except sqlite3.Error as exc:
        errors["conformance"] = str(exc)
    for h, f in _read_fixes().items():
        if f["ts"] >= since:
            how = "to run by hand" if f["via"] == "hand" else f"as card {f['card']} for {f['via']}"
            raw.append((f["ts"], "conformance", "fix requested", f"Fix asked for on {h}, {how}", h, ", ".join(f["rules"])))
    marks = ",".join("?" * len(LOG_TASK_KINDS))
    try:
        raw += [(r["created_at"], "task", f"task {r['kind'].replace('_', ' ')}", r["title"], r["assignee"],
                 f"{r['task_id']} · {r['kind']}")
                for r in k.execute(f"SELECT e.created_at, e.task_id, e.kind, t.title, t.assignee FROM task_events e"
                                   f" JOIN tasks t ON t.id=e.task_id WHERE e.created_at>=? AND e.kind IN ({marks})"
                                   f" ORDER BY e.created_at DESC LIMIT ?", (since, *LOG_TASK_KINDS, ACTIVITY_SCAN))]
    except sqlite3.Error as exc:
        errors["tasks"] = str(exc)
    groups: Dict[tuple, Dict[str, Any]] = {}
    for ts, category, kind, text, where, line in sorted(raw, key=lambda x: -x[0]):
        g = groups.setdefault((int(ts // 3600), category, kind, text, where), {
            "ts": ts, "first_ts": ts, "count": 0, "category": category, "kind": kind, "text": text,
            "where": where, "raw": []})
        g["first_ts"], g["count"] = ts, g["count"] + 1
        if len(g["raw"]) < LOG_RAW:
            g["raw"].append([ts, line])
    items = sorted(groups.values(), key=lambda g: -g["ts"])
    for g in items:
        # what a summary is cached under: a group that gains an event gets a new one
        g["key"] = hashlib.sha1(json.dumps([g["category"], g["kind"], g["text"], g["where"], g["count"], g["raw"]],
                                           default=str).encode("utf-8")).hexdigest()[:16]
    counts: Dict[str, int] = {}
    for g in items:
        counts[g["category"]] = counts.get(g["category"], 0) + 1
    return {"items": items[:LOG_LIMIT], "counts": counts, "truncated": len(items) > LOG_LIMIT}


@router.get("/activity")
def activity_route(hours: int = Query(24, ge=1, le=168)) -> Dict[str, Any]:
    now = time.time()
    out: Dict[str, Any] = {"generated_at": now, "hours": hours, "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    k = _ro_or_empty(_KANBAN_DB, _EMPTY_KANBAN, out["errors"], "kanban")
    try:
        out.update(activity_log(m, k, now - hours * 3600, out["errors"]))
    finally:
        m.close()
        k.close()
    out["summaries"] = _settings().get("activity_summaries") is True
    return out


# Readable summaries of Activity entries, written by the model the user picks for the
# "operations_summary" auxiliary task (registered in ../__init__.py) and kept with the name of that
# model and how many events each one read. Opt-in per install with the activity_summaries setting.
_SUMMARIES = get_default_hermes_root() / "plugin-data" / "operations" / "summaries.json"
SUMMARY_LINES = 40     # raw lines sent per model call
SUMMARY_KEEP = 500     # cached summaries kept, newest first
_SUMMARY_PROMPT = (
    "You turn operations log entries into short readable lines for an on-call engineer. "
    "Each entry has an id, its kind, where it happened, how many times, and sample raw lines. "
    "Write one plain sentence per entry, at most 25 words, using only facts in the entry. "
    "Do not guess causes or suggest fixes. Answer with only a JSON object mapping each id to its sentence.")


@contextlib.contextmanager
def _locked(path: Path):
    """Read-modify-write of a plugin-data file: the dashboard and the gateway's tick both write them."""
    import fcntl  # POSIX; the hosts this runs on
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _read_summaries() -> Dict[str, Any]:
    try:
        data = json.loads(_SUMMARIES.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def summarize(groups: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """One model call over groups that fit in SUMMARY_LINES raw lines; returns {key: summary}."""
    from agent.auxiliary_client import call_llm

    batch, lines = [], 0
    for g in groups:
        n = max(1, len(g["raw"]))
        if batch and lines + n > SUMMARY_LINES:
            break
        batch.append(g)
        lines += n
    prompt = "\n\n".join(
        f"id: e{i}\nkind: {g['kind']}\nwhere: {g['where'] or '-'}\ntimes: {g['count']}\ntext: {g['text']}\n"
        + "\n".join(f"raw: {line}" for _, line in g["raw"][:SUMMARY_LINES])
        for i, g in enumerate(batch))
    route: Dict[str, str] = {}
    # Headless, like kanban's dashboard calls: some relays reject a call with no affinity scope.
    from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
    token = None if get_affinity_scope() else set_affinity_scope("operations:summaries")
    try:
        resp = call_llm(task="operations_summary", temperature=0.0, max_tokens=60 * len(batch) + 100, timeout=60,
                        messages=[{"role": "system", "content": _SUMMARY_PROMPT}, {"role": "user", "content": prompt}],
                        route_info=route)
    finally:
        if token is not None:
            reset_affinity_scope(token)
    raw = (resp.choices[0].message.content or "").strip()
    found = re.search(r"\{.*\}", raw, re.DOTALL)
    parsed = json.loads(found.group(0) if found else raw)
    if not isinstance(parsed, dict):
        raise ValueError("the model did not answer with a JSON object")
    model = getattr(resp, "model", None) or route.get("model") or "unknown model"
    now = time.time()
    return {g["key"]: {"text": str(parsed[f"e{i}"]).strip(), "model": model, "provider": route.get("provider"),
                       "events": g["count"], "ts": now}
            for i, g in enumerate(batch) if str(parsed.get(f"e{i}") or "").strip()}


@router.post("/activity/summaries")
def activity_summaries(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Summaries for the window's entries: cached ones at once, plus one model call for the next
    entries that have none. "pending" counts entries still without one, so the page asks again."""
    if _settings().get("activity_summaries") is not True:
        raise HTTPException(403, "Activity summaries are off: turn on activity_summaries in the plugin's settings.")
    try:
        hours = int(body.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 0
    if not 1 <= hours <= 168:
        raise HTTPException(400, "hours must be 1 to 168")
    items = activity_route(hours)["items"]
    cache = _read_summaries()
    todo = [g for g in items if g["key"] not in cache]
    errors: Dict[str, str] = {}
    if todo:
        try:
            new = summarize(todo)
            with _locked(_SUMMARIES):
                # re-read: another request may have written while the model answered
                cache = {**_read_summaries(), **new}
                keep = sorted(cache.items(), key=lambda kv: -kv[1].get("ts", 0))[:SUMMARY_KEEP]
                tmp = _SUMMARIES.with_suffix(".tmp")
                tmp.write_text(json.dumps(dict(keep)), encoding="utf-8")
                tmp.replace(_SUMMARIES)
            cache = dict(keep)
        except Exception as exc:  # the model or its provider: a failure is reported, the page keeps raw text
            errors["model"] = f"{type(exc).__name__}: {exc}"[:300]
    out = {g["key"]: cache[g["key"]] for g in items if g["key"] in cache}
    pending = 0 if errors else sum(1 for g in items if g["key"] not in out)
    return {"summaries": out, "pending": pending, "errors": errors}


def handoffs(m: sqlite3.Connection, k: sqlite3.Connection, since: float, until: float,
             errors: Dict[str, str]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    try:
        cards = {t["id"]: {"id": t["id"], "kind": "card", "from": t["created_by"] or "unknown",
                           "to": t["assignee"] or "unassigned", "title": t["title"], "where": None,
                           "start": t["created_at"], "end": t["completed_at"], "status": t["status"],
                           "open": t["status"] not in ("done", "archived"), "runs": []}
                 for t in k.execute("SELECT id, title, created_by, assignee, status, created_at, completed_at FROM tasks"
                                    " WHERE created_at<? AND (created_at>=? OR completed_at>=? OR status NOT IN ('done', 'archived'))",
                                    (until, since, since))}
        if cards:
            marks = ",".join("?" * len(cards))
            for r in k.execute(f"SELECT task_id, profile, started_at, ended_at, outcome, error FROM task_runs"
                               f" WHERE task_id IN ({marks}) ORDER BY started_at", tuple(cards)):
                cards[r["task_id"]]["runs"].append({"profile": r["profile"], "start": r["started_at"], "end": r["ended_at"],
                                                    "outcome": r["outcome"], "error": (r["error"] or "")[:RUN_ERROR_CHARS] or None})
        for c in cards.values():
            if c["status"] == "scheduled":
                c["starts_at"] = _start_at(k, c["id"])
            if c["status"] == "blocked":
                c["blocked_reason"] = _reason(k, c["id"], "blocked")
        items += cards.values()
    except sqlite3.Error as exc:
        errors["kanban"] = str(exc)
    try:
        # profile came with later collectors; an older metrics.db reads it as unknown.
        profile = "profile" if any(r[1] == "profile" for r in m.execute("PRAGMA table_info(records)")) else "NULL"
        stops = m.execute(f"SELECT ts, host, {profile}, session_id, parent_session_id, duration_s FROM records"
                          " WHERE kind='subagent_stop' AND ts>=? AND ts<? ORDER BY ts", (since, until)).fetchall()
        children: Dict[str, Dict[str, Any]] = {}
        if stops:
            ids = [s_[3] for s_ in stops if s_[3]]
            marks = ",".join("?" * len(ids))
            for r in m.execute(f"SELECT session_id, {profile}, model, COUNT(*) FROM records WHERE kind='api'"
                               f" AND session_id IN ({marks}) GROUP BY 1, 2, 3 ORDER BY 4", ids):
                c = children.setdefault(r[0], {"profile": None, "model": None, "requests": 0})
                c["profile"], c["model"], c["requests"] = c["profile"] or r[1], r[2], c["requests"] + r[3]
        groups: Dict[str, Dict[str, Any]] = {}
        for ts, host, prof, sid, parent, dur in stops:
            child = children.get(sid, {})
            g = groups.setdefault(parent or sid or str(ts), {
                "id": f"subagents:{parent or sid or ts}", "kind": "subagent", "from": prof or child.get("profile") or f"{host} (no profile)",
                "to": "subagent", "title": None, "where": host, "start": ts - (dur or 0), "end": ts,
                "status": "done", "open": False, "children": []})
            g["start"], g["end"] = min(g["start"], ts - (dur or 0)), max(g["end"], ts)
            g["children"].append({"start": ts - (dur or 0), "end": ts, "duration_s": dur,
                                  "model": child.get("model"), "requests": child.get("requests", 0)})
        for g in groups.values():
            n, calls = len(g["children"]), sum(c["requests"] for c in g["children"])
            g["title"] = f"{n} subagent{'s' if n != 1 else ''}, {calls} request{'s' if calls != 1 else ''}"
        items += groups.values()
    except sqlite3.Error as exc:
        errors["subagents"] = str(exc)
    items.sort(key=lambda i: -(i["start"] or 0))
    pairs: Dict[tuple, Dict[str, Any]] = {}
    for i in items:
        p = pairs.setdefault((i["from"], i["to"], i["kind"]), {"from": i["from"], "to": i["to"], "kind": i["kind"],
                                                               "count": 0, "open": 0})
        p["count"], p["open"] = p["count"] + 1, p["open"] + i["open"]
    return {"items": items[:HANDOFF_LIMIT], "truncated": len(items) > HANDOFF_LIMIT,
            "pairs": sorted(pairs.values(), key=lambda p: (-p["open"], -p["count"]))}


@router.get("/handoffs")
def handoffs_route(hours: int = Query(168, ge=1, le=168), start: Optional[float] = None) -> Dict[str, Any]:
    since, until = _window(hours, start)
    out: Dict[str, Any] = {"generated_at": time.time(), "hours": hours, "start": start, "errors": {}}
    m = _ro_or_empty(_METRICS_DB, _EMPTY_METRICS, out["errors"], "metrics")
    k = _ro_or_empty(_KANBAN_DB, _EMPTY_KANBAN, out["errors"], "kanban")
    try:
        out.update(handoffs(m, k, since, until, out["errors"]))
    finally:
        m.close()
        k.close()
    return out


# Shared with ../__init__.py, whose dispatch-tick hook reads it back to release the card.
_START_AT = re.compile(r"\(operations:(\d+)\)")


def _reason(k: sqlite3.Connection, task_id: str, kind: str) -> str:
    """The reason on the card's latest ``kind`` event (scheduled, blocked)."""
    row = k.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?"
                    " ORDER BY id DESC LIMIT 1", (task_id, kind)).fetchone()
    return ((json.loads(row[0]) or {}).get("reason") or "") if row and row[0] else ""


def _start_at(k: sqlite3.Connection, task_id: str):
    m = _START_AT.search(_reason(k, task_id, "scheduled"))
    return int(m.group(1)) if m else None


@router.get("/assignees")
def assignees() -> Dict[str, Any]:
    return {"assignees": kanban_db.list_profiles_on_disk()}


def _card_write(task_id: str, fn) -> Dict[str, Any]:
    """Run one kanban write on an existing card; 409 when Hermes refuses it."""
    if not _KANBAN_DB.exists():
        raise HTTPException(404, "no kanban board")
    with connect_closing(_KANBAN_DB) as k:
        if kanban_db.get_task(k, task_id) is None:
            raise HTTPException(404, f"card {task_id} not found")
        try:
            ok = fn(k)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc))
        if not ok:
            raise HTTPException(409, f"Hermes refused the change to {task_id}; check the card on the Kanban page")
        return {"ok": True, "status": kanban_db.get_task(k, task_id).status}


@router.post("/cards/{task_id}/assign")
def card_assign(task_id: str, profile: str = Body(..., embed=True),
                start_at: Optional[int] = Body(None, embed=True)) -> Dict[str, Any]:
    if profile not in kanban_db.list_profiles_on_disk():
        raise HTTPException(400, f"no profile named {profile!r}")
    later = start_at is not None and start_at > time.time() + 60

    def write(k):
        # A card already scheduled is released first: to start now, or to be rescheduled.
        if not kanban_db.assign_task(k, task_id, profile):
            return False
        released = kanban_db.get_task(k, task_id).status == "scheduled"
        if released and not kanban_db.unblock_task(k, task_id):
            return False
        if not later:
            return True
        # Kanban has no reschedule, so a scheduled card is released above and parked again here; one a
        # dispatcher claimed in between is left running rather than parked under its worker.
        when = datetime.fromtimestamp(start_at).strftime("%Y-%m-%d %H:%M")
        if (released and kanban_db.get_task(k, task_id).status not in ("todo", "ready")) or not kanban_db.schedule_task(
                k, task_id, reason=f"Start at {when} (operations:{start_at})"):
            raise RuntimeError(f"{task_id} is {kanban_db.get_task(k, task_id).status}, not scheduled for {when}; "
                               "check it on the Kanban page")
        return True

    out = _card_write(task_id, write)
    out["started"], out["warning"] = False, ""
    if later:
        out["starts_at"] = start_at
    return _start_now(out, task_id, profile)


@router.post("/cards/{task_id}/unblock")
def card_unblock(task_id: str, comment: str = Body("", embed=True)) -> Dict[str, Any]:
    """Answer a blocked card: the comment reaches the worker with the card's history."""
    def write(k):
        # Unblock first, so a refused unblock leaves no stray comment; the worker reads it either way.
        if not kanban_db.unblock_task(k, task_id):
            return False
        if comment.strip():
            kanban_db.add_comment(k, task_id, author="dashboard", body=comment.strip())
        return True

    out = _card_write(task_id, write)
    out["started"], out["warning"] = False, ""
    with connect_closing(_KANBAN_DB) as k:
        profile = kanban_db.get_task(k, task_id).assignee
    return _start_now(out, task_id, profile) if profile else out


def _start_now(out: Dict[str, Any], task_id: str, profile: str) -> Dict[str, Any]:
    if out["status"] != "ready":
        return out
    # One dispatcher tick now instead of waiting up to a minute for the gateway's; same
    # config-driven limits as the gateway and `hermes kanban dispatch`.
    cfg = (load_config() or {}).get("kanban") or {}
    with connect_closing(_KANBAN_DB) as k:
        res = kbd.dispatch_once(
            k,
            max_spawn=kbd._positive_int(cfg.get("max_spawn"), None),
            max_in_progress=kbd.resolve_max_in_progress(kbd._positive_int(cfg.get("max_in_progress"), None)),
            default_assignee=(cfg.get("default_assignee") or "").strip() or None,
            max_in_progress_per_profile=kbd._positive_int(cfg.get("max_in_progress_per_profile"), None))
    out["started"] = any(t == task_id for t, _, _ in res.spawned)
    busy = [n for t, _, n in res.skipped_per_profile_capped if t == task_id]
    if out["started"] or res.skipped_locked:
        pass  # a locked tick means another dispatcher is mid-tick and claims it now
    elif busy:
        out["warning"] = f"{profile} is busy with {busy[0]} card(s); it starts this one when one finishes."
    else:
        out["warning"] = f"{profile} did not start it yet; the Kanban page shows why."
        # Same probe the Kanban page uses; it fails open, so a warning is always real.
        try:
            from hermes_cli.kanban import _check_dispatcher_presence
            running, message = _check_dispatcher_presence(hermes_home=get_hermes_home())
            if not running:
                out["warning"] = message
        except Exception:
            pass
    return out


@router.post("/cards/{task_id}/close")
def card_close(task_id: str, outcome: str = Body(..., embed=True), result: str = Body("", embed=True)) -> Dict[str, Any]:
    if outcome == "done":
        # Hermes refuses a completion with no result, so the page asks for one.
        return _card_write(task_id, lambda k: kanban_db.complete_task(k, task_id, result=result.strip() or None))
    if outcome == "archived":
        return _card_write(task_id, lambda k: kanban_db.archive_task(k, task_id))
    raise HTTPException(400, "outcome must be done or archived")


def host_results(m: sqlite3.Connection, declared: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    """Every declared host plus any host that reported, with the results of its newest report.

    The forwarder on each host writes conformance_results through the collector; a host's
    rows from one report share a ts. A declared host with no rows has never reported (or
    not within the collector's 14 days) and stays stale.
    """
    hosts = {name: {"host": name, "declared": True, "os": h.get("os"), "roles": h.get("roles") or [],
                    "ts": None, "stale": True, "verified": True, "standard_rev": None, "results": {}}
             for name, h in declared.items()}
    rows = m.execute(
        "SELECT c.host, c.ts, c.rule, c.status, c.value, c.evidence, c.standard_rev, c.verified"
        " FROM conformance_results c JOIN (SELECT host, MAX(ts) AS ts FROM conformance_results GROUP BY host) n"
        " ON c.host = n.host AND c.ts = n.ts").fetchall()
    for r in rows:
        h = hosts.setdefault(r["host"], {"host": r["host"], "declared": False, "os": None, "roles": [],
                                         "verified": True, "results": {}})
        h.update(ts=r["ts"], stale=now - r["ts"] > HOST_STALE_S, standard_rev=r["standard_rev"],
                 verified=h["verified"] and bool(r["verified"]))
        h["results"][r["rule"]] = {"status": r["status"], "value": r["value"],
                                   "evidence": json.loads(r["evidence"] or "[]")}
    return sorted(hosts.values(), key=lambda h: h["host"])


def _standard_project() -> str:
    """The Conformance project setting, or the local standard while it is unset and the file exists."""
    project = (_settings().get("conformance_project") or "").strip()
    return project or (str(_LOCAL_STANDARD) if _LOCAL_STANDARD.is_file() else "")


def _local_file(project: str) -> Optional[Path]:
    """A project that is a path is a local conformance.yaml, or the folder holding one, read without GitLab."""
    path = Path(project).expanduser()
    if not path.is_absolute():
        return None
    return path / "conformance.yaml" if path.is_dir() else path


def _standard_text(project: str) -> str:
    local = _local_file(project)
    if local:
        return local.read_text(encoding="utf-8")
    return _gitlab(f"projects/{urllib.parse.quote(project, safe='')}/repository/files/conformance.yaml/raw?ref=main")


@router.get("/conformance")
def conformance() -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    project = _standard_project() or None
    out: Dict[str, Any] = {"generated_at": time.time(), "project": project, "standard": None, "repo": None,
                           "host_rules": [], "hosts": None, "pipeline": None, "errors": errors,
                           "org": org_mode(), "local": False}
    # Optional feature: without a project there is nothing to check, which is not an error.
    if not project:
        return out
    # A local standard has no CI: its repo rules are checked per project (/conformance/projects) instead.
    out["local"] = bool(_local_file(project))
    pid = urllib.parse.quote(project, safe="")
    fetch_errors = (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError)
    if not out["local"]:
        out["repo_url"] = f"https://{_gitlab_host()}/{project}"
        try:
            report = json.loads(_gitlab(f"projects/{pid}/jobs/artifacts/main/raw/"
                                        f"conformance-report.json?job=standard"))
            out["standard"] = report["standard"]
            out["repo"] = [dict(r, findings=r.get("findings") or []) for r in report["results"]]
            if report.get("errors"):
                errors["checker"] = "; ".join(str(e) for e in report["errors"])
        except fetch_errors as exc:
            errors["report"] = str(exc)
    std: Dict[str, Any] = {}
    try:
        std = safe_load(_standard_text(project)) or {}
    except fetch_errors + (YAMLError,) as exc:
        errors["rules"] = str(exc)
    if out["local"] and isinstance(std, dict):
        out["standard"] = std.get("standard")
    rule_text = {r["id"]: {k: str(r[k]).strip() for k in ("title", "severity", "fix", "remediate") if r.get(k)}
                 for r in (std.get("repo_rules") or []) + (std.get("host_rules") or [])}
    for r in out["repo"] or []:
        r.update(rule_text.get(r["rule"], {}))
    out["host_rules"] = [dict(rule_text[r["id"]], rule=r["id"], applies_to=r.get("applies_to") or [])
                         for r in std.get("host_rules") or []]
    # The rule editor offers the check kinds and severities the standard already uses.
    used = [r for r in (std.get("host_rules") or []) + (std.get("repo_rules") or []) if isinstance(r.get("check"), dict)]
    out["check_examples"] = {r["check"].get("kind"): _flow(r["check"]) for r in reversed(used)}
    out["severities"] = sorted({str(r["severity"]) for r in used if r.get("severity")})
    try:
        m = _ro(_METRICS_DB)
        try:
            out["hosts"] = host_results(m, std.get("hosts") or {}, out["generated_at"])
        finally:
            m.close()
    except sqlite3.Error as exc:
        errors["hosts"] = f"{_METRICS_DB} not found" if not _METRICS_DB.exists() else str(exc)
    out["fixes"] = fix_states(_read_fixes(), out["hosts"] or [])
    if out["local"]:
        return out
    try:
        pipes = json.loads(_gitlab(f"projects/{pid}/pipelines?ref=main&per_page=1"))
        if pipes:
            p = pipes[0]
            out["pipeline"] = {"status": p["status"], "sha": p["sha"][:8], "url": p["web_url"],
                               "ts": datetime.fromisoformat(p["updated_at"].replace("Z", "+00:00")).timestamp()}
    except fetch_errors as exc:
        errors["pipeline"] = str(exc)
    return out


# The built-in repo standard: what every project is checked against, with or without a Conformance
# project. Each is an `absent` rule, the same kind (and the same matching) as the standard's own
# checker in CI, so the plugin can run it itself over a checkout's tracked files.
DEFAULT_REPO_RULES = [
    {"id": "no-private-keys", "title": "No private keys in the repo", "severity": "blocks",
     "check": {"kind": "absent", "pattern": r"-----BEGIN [A-Z ]*PRIVATE KEY-----(\s*$|\\n)", "paths": ["*"]},
     "fix": "Remove the key, rotate it, and load it from a secret store or an ignored file."},
    {"id": "no-api-tokens", "title": "No API tokens in the repo", "severity": "blocks",
     "check": {"kind": "absent", "paths": ["*"],
               "pattern": r"\b(AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|glpat-[A-Za-z0-9_-]{20}"
                          r"|xox[baprs]-[A-Za-z0-9-]{10,}|sk-ant-[A-Za-z0-9_-]{20,})"},
     "fix": "Revoke the token, issue a new one, and read it from the environment or a secret store."},
    {"id": "no-conflict-markers", "title": "No merge conflict markers", "severity": "blocks",
     "check": {"kind": "absent", "pattern": r"^(<<<<<<<|>>>>>>>) ", "paths": ["*"]},
     "fix": "Finish the merge: keep the right side and delete the marker lines."},
    {"id": "no-home-paths", "title": "No hard-coded home directories in code", "severity": "warns",
     "check": {"kind": "absent", "pattern": r"(/Users|/home)/[A-Za-z0-9._-]+/",
               "paths": ["*.sh", "*.py", "*.js", "*.mjs", "*.cjs", "*.ts", "*.tsx", "*.go", "*.rs", "*.rb",
                         "*.ps1", "*.plist", "*.json", "*.yaml", "*.yml", "*.toml"]},
     "fix": "Use $HOME, ~, or a path from config, so it runs for someone else."},
]
_SCAN_MAX_BYTES = 1_000_000
_SCAN_MAX_FINDINGS = 50
_scan_cache: Dict[str, tuple] = {}  # root -> ((rules, repo state), results); a large checkout takes seconds


def repo_state(root: str) -> tuple:
    """What the checked files depend on, cheaply: HEAD plus each changed tracked file's size and mtime,
    so an edit, commit or checkout changes it and an untouched repo is not read again."""
    git = dict(capture_output=True, timeout=20, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})  # no index refresh write
    # a repo's own config cannot have status run its fsmonitor program
    status = subprocess.run(["git", "-C", root, "-c", "core.fsmonitor=false", "status", "--porcelain", "-z",
                             "--untracked-files=no"],
                            check=True, **git).stdout.decode("utf-8", "replace").split("\0")
    head = subprocess.run(["git", "-C", root, "rev-parse", "-q", "--verify", "HEAD"], **git).stdout.strip()
    changed, skip = [], False
    for entry in filter(None, status):
        if skip:  # a rename's old path
            skip = False
            continue
        skip = entry[0] in "RC"
        try:
            st = os.stat(os.path.join(root, entry[3:]))
            changed.append((entry, st.st_size, st.st_mtime_ns))
        except OSError:
            changed.append((entry, None, None))
    return head, tuple(changed)


def scan_project(root: str, rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One checkout against `absent` rules: `git ls-files`, then each rule's pattern over the files its
    paths match, as the standard's own checker does. Large and binary files are skipped."""
    files = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, timeout=20,
                           check=True).stdout.decode("utf-8", "replace").split("\0")
    checks = [(r, re.compile(r["check"]["pattern"]), {ex.get("path") for ex in r.get("exceptions") or []})
              for r in rules]
    found: Dict[str, List[Dict[str, Any]]] = {r["id"]: [] for r in rules}
    counts = dict.fromkeys(found, 0)
    for path in filter(None, files):
        wanted = [(r, p) for r, p, ex in checks
                  if path not in ex and any(fnmatch.fnmatch(path, g) for g in r["check"]["paths"])]
        if not wanted:
            continue
        try:
            full = os.path.join(root, path)
            if os.path.getsize(full) > _SCAN_MAX_BYTES:
                continue
            with open(full, "rb") as f:
                raw = f.read()
        except OSError:  # tracked but deleted in the working tree
            continue
        if b"\0" in raw[:4096]:
            continue
        for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            for r, p in wanted:
                if p.search(line):
                    counts[r["id"]] += 1
                    if len(found[r["id"]]) < _SCAN_MAX_FINDINGS:
                        found[r["id"]].append({"file": path, "line": n, "text": line.strip()[:200]})
    return {r["id"]: {"status": ("fail" if r["severity"] == "blocks" else "warn") if counts[r["id"]] else "pass",
                      "count": counts[r["id"]], "findings": found[r["id"]]} for r in rules}


def absent_rules(std: Any, source: str) -> tuple:
    """A conformance.yaml's `absent` repo rules, the kind checked here (the rest need a tool and stay in that
    repo's CI), and why any were left out. The file is someone's repo content, so each rule is checked."""
    rules, problems = [], []
    for r in (std.get("repo_rules") if isinstance(std, dict) else None) or []:
        check = r.get("check") if isinstance(r, dict) else None
        if not (isinstance(check, dict) and check.get("kind") == "absent"):
            continue
        paths = [check["paths"]] if isinstance(check.get("paths"), str) else check.get("paths")
        if not (isinstance(r.get("id"), str) and isinstance(check.get("pattern"), str) and isinstance(paths, list)
                and paths and all(isinstance(g, str) for g in paths)):
            problems.append(f"{r.get('id', 'a rule')}: needs an id, a pattern and paths")
            continue
        try:
            re.compile(check["pattern"])
        except re.error as exc:
            problems.append(f"{r['id']}: pattern does not compile ({exc})")
            continue
        exceptions = [ex for ex in r.get("exceptions") or [] if isinstance(ex, dict)]
        rules.append({"id": r["id"], "title": r.get("title"), "fix": r.get("fix"), "source": source,
                      "severity": r.get("severity") if r.get("severity") in ("blocks", "warns") else "warns",
                      "check": {"kind": "absent", "pattern": check["pattern"], "paths": paths},
                      "exceptions": exceptions})
    return rules, problems


@router.get("/conformance/projects")
def conformance_projects(root: List[str] = Query(default=[])) -> Dict[str, Any]:
    """The projects the page lists (Hermes' own projects and repos, which it reads from Desktop) against
    the built-in repo standard, the Conformance project's `absent` repo rules, and a project's own
    conformance.yaml rules for that project only (they add checks; a rule id already defined above
    stays as it is there, so a repo cannot loosen the fleet's rules). Read only: the files git tracks
    in each checkout, nothing else."""
    errors: Dict[str, str] = {}
    rules = {r["id"]: dict(r, source="built-in") for r in DEFAULT_REPO_RULES}
    project = _standard_project()
    if project:
        try:
            std = safe_load(_standard_text(project))
            fleet, problems = absent_rules(std, project)
            rules.update((r["id"], r) for r in fleet if r["id"] not in rules)
            if problems:
                errors["rules"] = "; ".join(problems)
        except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError, YAMLError) as exc:
            errors["rules"] = str(exc)
    shared = list(rules.values())
    columns = dict(rules)
    out = []
    now = time.time()
    for path in list(dict.fromkeys(root))[:30]:
        row: Dict[str, Any] = {"root": path, "results": {}, "error": None, "rules_error": None}
        if not os.path.isdir(path):
            row["error"] = "folder not found"
            out.append(row)
            continue
        own: List[Dict[str, Any]] = []
        local = os.path.join(path, "conformance.yaml")
        if os.path.isfile(local):
            try:
                with open(local, encoding="utf-8") as f:
                    own, problems = absent_rules(safe_load(f), "conformance.yaml")
                own = [r for r in own if r["id"] not in rules]
                row["rules_error"] = "; ".join(problems) or None
            except (OSError, UnicodeDecodeError, YAMLError) as exc:
                row["rules_error"] = f"conformance.yaml: {exc}"
        checked = shared + own
        for r in own:
            columns.setdefault(r["id"], r)
        sig = json.dumps(checked, sort_keys=True, default=str)
        try:
            state = repo_state(path)
            hit = _scan_cache.get(path)
            row["results"] = hit[1] if hit and hit[0] == (sig, state) else scan_project(path, checked)
            row["scanned_at"] = now
            _scan_cache[path] = ((sig, state), row["results"])
        except subprocess.CalledProcessError:
            row["error"] = "not a git repository"
        except (OSError, subprocess.SubprocessError) as exc:
            row["error"] = str(exc)
        out.append(row)
    return {"rules": [{k: r.get(k) for k in ("id", "title", "severity", "fix", "source")} for r in columns.values()],
            "projects": out, "errors": errors}


def _project() -> str:
    project = _standard_project()
    if not project:
        raise HTTPException(400, "set Conformance project in the Operations settings, or create a local standard, first")
    return project


_STARTER = """\
# Fleet standard for the Operations plugin's Conformance tab, used while its Conformance project
# setting is empty. "Propose a rule" on that tab adds rules here; edit it by hand too.
standard: local

# Each host under the label its metrics forwarder reports, with its os and roles. A host rule
# applies to a host when its applies_to names that os or one of those roles.
hosts:
  # my-laptop: { os: macos, roles: [workstation] }

# Checked on each host by the metrics forwarder (hermes-metrics-dash host_checks.py, with
# CONFORMANCE_STANDARD set to this file's path). Check kinds: revision, job, port, keys.
host_rules:

# Checked by the plugin over the files git tracks in each project. Check kind: absent.
repo_rules:
"""


@router.post("/conformance/local")
def create_local_standard() -> Dict[str, Any]:
    """Start a local standard, so the fleet checks work without a GitLab repo or CI."""
    if _LOCAL_STANDARD.exists():
        raise HTTPException(409, f"{_LOCAL_STANDARD} already exists")
    _LOCAL_STANDARD.parent.mkdir(parents=True, exist_ok=True)
    _LOCAL_STANDARD.write_text(_STARTER, encoding="utf-8")
    return {"ok": True, "path": str(_LOCAL_STANDARD)}


def _gitlab_error(exc: Exception) -> HTTPException:
    if isinstance(exc, urllib.error.HTTPError):
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            detail += " (the GitLab token needs the api scope to propose rules)"
        return HTTPException(502, f"GitLab answered {exc.code}: {detail}")
    return HTTPException(502, str(exc))


# A fix asked for from the Conformance tab, newest per host. The dashboard never reaches into a
# host: whoever runs the fix (a profile's worker from a card, or a person) does it on the host, and
# the host's own next conformance report says whether it worked (fix_states).
_FIXES = get_default_hermes_root() / "plugin-data" / "operations" / "fixes.json"


def _read_fixes() -> Dict[str, Any]:
    try:
        data = json.loads(_FIXES.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_fix(host: str, fix: Dict[str, Any]) -> None:
    with _locked(_FIXES):
        fixes = _read_fixes()
        fixes[host] = fix
        tmp = _FIXES.with_suffix(".tmp")
        tmp.write_text(json.dumps(fixes, indent=1), encoding="utf-8")
        tmp.replace(_FIXES)


def fix_states(fixes: Dict[str, Any], hosts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Each host's newest fix with its state: waiting until the host reports after it, then fixed
    when every check it was asked to fix passes in that report, else still failing."""
    by_name = {h["host"]: h for h in hosts}
    out = {}
    for name, f in fixes.items():
        h = by_name.get(name) or {}
        if not h.get("ts") or h["ts"] <= f["ts"]:
            state = "waiting"
        else:
            bad = [r for r in f["rules"] if (h["results"].get(r) or {}).get("status") in ("fail", "warn")]
            state, f = ("still failing", dict(f, failing=bad)) if bad else ("fixed", f)
        out[name] = dict(f, state=state, report_ts=h.get("ts"))
    return out


@router.post("/conformance/hosts/{host}/fix")
def fix_drift(host: str, profile: Optional[str] = Body(None, embed=True)) -> Dict[str, Any]:
    out = drift_card(host, profile)
    return _start_now(out, out["card"], profile) if out["card"] and not out["existing"] else out


def drift_card(host: str, profile: Optional[str]) -> Dict[str, Any]:
    """Ask for a host's failing checks to be fixed: by a profile, as a kanban card that starts now,
    or (no profile) by the person asking, who runs the standard's fix on the host themselves.

    The dashboard does not reach into other hosts itself; the card carries the failing checks with
    their evidence, fixes and any `remediate` command the standard gives, and whoever does the fix
    does it on the host. One open card per host: asking again while it is open returns that card.
    """
    project = _project()
    if profile and profile not in kanban_db.list_profiles_on_disk():
        raise HTTPException(400, f"no profile named {profile!r}")
    if profile and not _KANBAN_DB.exists():
        raise HTTPException(404, "no kanban board")
    if not _METRICS_DB.exists():
        raise HTTPException(404, "no metrics.db: host checks come from the metrics collector")
    try:
        std = safe_load(_standard_text(project)) or {}
    except (RuntimeError, urllib.error.URLError, OSError, ValueError, YAMLError) as exc:
        raise _gitlab_error(exc)
    m = _ro(_METRICS_DB)
    try:
        h = next((x for x in host_results(m, std.get("hosts") or {}, time.time()) if x["host"] == host), None)
    finally:
        m.close()
    rules = {r["id"]: r for r in std.get("host_rules") or []}
    failing = [(rules[rid], res) for rid, res in (h or {}).get("results", {}).items()
               if rid in rules and res["status"] in ("fail", "warn")]
    if not failing:
        raise HTTPException(409, f"{host} has no failing checks in its newest report")
    fix = {"ts": time.time(), "via": profile or "hand", "card": None, "rules": [r["id"] for r, _ in failing],
           "commands": [str(r["remediate"]).strip() for r, _ in failing if r.get("remediate")]}
    if not profile:
        _record_fix(host, fix)
        return {"ok": True, "card": None, "existing": False, "status": "by hand", "started": False, "warning": "",
                "commands": fix["commands"]}
    key = f"operations:drift:{project}:{host}:"
    with connect_closing(_KANBAN_DB) as k:
        row = k.execute("SELECT id FROM tasks WHERE substr(idempotency_key, 1, ?) = ?"
                        " AND status NOT IN ('done', 'archived') ORDER BY created_at DESC LIMIT 1",
                        (len(key), key)).fetchone()
        if row:
            return {"ok": True, "card": row[0], "existing": True, "status": kanban_db.get_task(k, row[0]).status,
                    "started": False, "warning": ""}
        when = datetime.fromtimestamp(h["ts"]).strftime("%Y-%m-%d %H:%M")
        where = project if _local_file(project) else f"https://{_gitlab_host()}/{project}, conformance.yaml"
        lines = [f"Host {host} fails these checks of the fleet standard {std.get('standard') or project} "
                 f"({where}), in its report of {when} "
                 f"against standard revision {h['standard_rev'] or 'unknown'}.", ""]
        for r, res in failing:
            lines.append(f"- {r.get('title') or r['id']} ({r['id']}, {r.get('severity') or 'no severity'}): "
                         f"{res['status']}{' - ' + str(res['value']) if res['value'] not in (None, '') else ''}")
            lines += [f"  Evidence: {e}" for e in res["evidence"]]
            lines += [f"  {label}: {str(r[f]).strip()}" for label, f in (("Why", "why"), ("Fix", "fix"),
                                                                         ("Run on the host", "remediate")) if r.get(f)]
        lines += ["", f"Fix these on {host}. Its next report (every few minutes) shows on the Operations "
                      "Conformance tab whether they pass. If a step needs a person, block this card and say what."]
        card = kanban_db.create_task(k, title=f"Fix conformance drift on {host}", body="\n".join(lines),
                                     assignee=profile, created_by="dashboard", idempotency_key=f"{key}{int(time.time())}")
        out = {"ok": True, "card": card, "existing": False, "status": kanban_db.get_task(k, card).status,
               "started": False, "warning": ""}
    _record_fix(host, dict(fix, card=card))
    # not started here: the gateway's tick calls this from inside a dispatcher tick, and its next tick starts it
    return out


@router.post("/conformance/projects/fix")
def fix_project(root: str = Body(..., embed=True), profile: str = Body(..., embed=True)) -> Dict[str, Any]:
    """File a kanban card for a profile to fix a project's failing checks, and start it.

    The project is checked again here rather than taken from the page. The card names each finding by
    file and line but leaves out the line itself, which for a key or token rule is the secret. One open
    card per project: asking again while it is open returns that card.
    """
    if profile not in kanban_db.list_profiles_on_disk():
        raise HTTPException(400, f"no profile named {profile!r}")
    if not _KANBAN_DB.exists():
        raise HTTPException(404, "no kanban board")
    scan = conformance_projects([root])
    row = scan["projects"][0]
    if row["error"]:
        raise HTTPException(409, f"{root}: {row['error']}")
    rules = {r["id"]: r for r in scan["rules"]}
    failing = [(rules[rid], res) for rid, res in row["results"].items() if res["status"] in ("fail", "warn")]
    if not failing:
        raise HTTPException(409, f"{root} passes every check")
    name = os.path.basename(root.rstrip("/\\")) or root
    key = f"operations:project:{root}:"
    with connect_closing(_KANBAN_DB) as k:
        found = k.execute("SELECT id FROM tasks WHERE substr(idempotency_key, 1, ?) = ?"
                          " AND status NOT IN ('done', 'archived') ORDER BY created_at DESC LIMIT 1",
                          (len(key), key)).fetchone()
        if found:
            return {"ok": True, "card": found[0], "existing": True, "status": kanban_db.get_task(k, found[0]).status,
                    "started": False, "warning": ""}
        lines = [f"The project at {root} fails these conformance checks, in the files git tracks there as of "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}.", ""]
        for r, res in failing:
            lines.append(f"- {r.get('title') or r['id']} ({r['id']}, {r.get('severity')}, from {r.get('source')}): "
                         f"{res['count']} line{'' if res['count'] == 1 else 's'}")
            lines += [f"  {f['file']}:{f['line']}" for f in res["findings"]]
            if res["count"] > len(res["findings"]):
                lines.append(f"  and {res['count'] - len(res['findings'])} more")
            if r.get("fix"):
                lines.append(f"  Fix: {str(r['fix']).strip()}")
        lines += ["", "The lines themselves are left out so no secret is copied onto this card; open each file at "
                      "that line. Fix these in that folder. The Operations Conformance tab shows within a minute "
                      "whether they pass. If a step needs a person (rotating a leaked key, say), block this card "
                      "and say what."]
        card = kanban_db.create_task(k, title=f"Fix conformance findings in {name}", body="\n".join(lines),
                                     assignee=profile, created_by="dashboard", idempotency_key=f"{key}{int(time.time())}")
        out = {"ok": True, "card": card, "existing": False, "status": kanban_db.get_task(k, card).status,
               "started": False, "warning": ""}
    return _start_now(out, card, profile)


@router.post("/attention/fix")
def fix_alert(key: str = Body(..., embed=True), title: str = Body(..., embed=True), body: str = Body(..., embed=True),
              profile: str = Body(..., embed=True)) -> Dict[str, Any]:
    """File a kanban card for a profile to look into one alert from the Notifications tab, and start it.

    The alert may have cleared since, so the page sends what it showed (title and brief) rather than
    the route looking it up again. One open card per alert key: asking again returns that card.
    """
    if profile not in kanban_db.list_profiles_on_disk():
        raise HTTPException(400, f"no profile named {profile!r}")
    if not _KANBAN_DB.exists():
        raise HTTPException(404, "no kanban board")
    title, body = " ".join(title.split())[:200], body.strip()[:8000]
    if not key.strip() or not title:
        raise HTTPException(400, "an alert needs a key and a title")
    key = f"operations:alert:{key.strip()}:"
    with connect_closing(_KANBAN_DB) as k:
        found = k.execute("SELECT id FROM tasks WHERE substr(idempotency_key, 1, ?) = ?"
                          " AND status NOT IN ('done', 'archived') ORDER BY created_at DESC LIMIT 1",
                          (len(key), key)).fetchone()
        if found:
            return {"ok": True, "card": found[0], "existing": True, "status": kanban_db.get_task(k, found[0]).status,
                    "started": False, "warning": ""}
        card = kanban_db.create_task(k, title=f"Look into: {title}", body=body, assignee=profile,
                                     created_by="dashboard", idempotency_key=f"{key}{int(time.time())}")
        out = {"ok": True, "card": card, "existing": False, "status": kanban_db.get_task(k, card).status,
               "started": False, "warning": ""}
    return _start_now(out, card, profile)


_RULE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_PLAIN = re.compile(r"[A-Za-z0-9_./~-]+")


def _flow(v: Any) -> str:
    """One value as YAML flow text, in the standard's own style: { kind: port, timeout_s: 2 }."""
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k}: {_flow(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_flow(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str) and not (_PLAIN.fullmatch(v) and safe_load(v) == v):
        return json.dumps(v)
    return str(v)


def add_rule(text: str, kind: str, rule: Dict[str, Any]) -> str:
    """conformance.yaml with the rule appended to its host_rules or repo_rules list.

    Inserted as text after the list's last entry, so every comment and the file's layout stay
    as they are; comments that introduce the next top-level key stay with that key.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((i for i, l in enumerate(lines) if l.rstrip() == f"{kind}:"), None)
    if start is None:
        raise HTTPException(409, f"conformance.yaml has no block-style {kind}: list; add this rule by hand")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and not lines[i][0].isspace() and not lines[i].startswith("#")), len(lines))
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    entry = [f"  - id: {rule['id']}\n"] + [f"    {k}: {_flow(v)}\n" for k, v in rule.items() if k != "id"]
    return "".join(lines[:end] + (["\n"] if end > start + 1 else []) + entry + lines[end:])


@router.post("/conformance/rules")
def propose_rule(kind: str = Body(..., embed=True), rule: Dict[str, Any] = Body(..., embed=True),
                 to: str = Body("repo", embed=True)) -> Dict[str, Any]:
    """Open a merge request that adds a rule to the standard; main is never written directly.

    The repo's own CI validates the rule on the MR (check kinds are its fixed vocabulary), and
    a person merges it. A local standard has no repo to review it, so the rule is written into
    the file. With to="org" (business mode) the rule goes to the organisation's copy of the
    standard instead, see propose_to_org.
    """
    project = _project()
    if kind not in ("host_rules", "repo_rules"):
        raise HTTPException(400, "kind must be host_rules or repo_rules")
    rule = {k: v for k, v in rule.items() if v not in (None, "", [])}
    missing = [f for f in ("id", "title", "severity", "check", "why", "fix") if f not in rule]
    if missing:
        raise HTTPException(400, f"missing {', '.join(missing)}")
    if not _RULE_ID.fullmatch(str(rule["id"])):
        raise HTTPException(400, "id is lowercase letters, digits and dashes")
    try:
        check = safe_load(rule["check"]) if isinstance(rule["check"], str) else rule["check"]
    except YAMLError as exc:
        raise HTTPException(400, f"check is not YAML: {exc}")
    if not isinstance(check, dict) or not isinstance(check.get("kind"), str):
        raise HTTPException(400, "check is a mapping with a kind, e.g. { kind: port, url: ..., timeout_s: 2 }")
    rule["check"] = check
    order = ("id", "title", "severity", "applies_to", "check", "why", "fix")
    rule = {k: rule[k] for k in order if k in rule}
    pid = urllib.parse.quote(project, safe="")
    local = _local_file(project)
    try:
        if local:
            text = local.read_text(encoding="utf-8")
        else:
            f = json.loads(_gitlab(f"projects/{pid}/repository/files/conformance.yaml?ref=main"))
            text = base64.b64decode(f["content"]).decode("utf-8")
        std = safe_load(text) or {}
    except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError, YAMLError) as exc:
        raise _gitlab_error(exc)
    if to == "org":
        return propose_to_org(kind, rule, text, std)
    taken = {r.get("id") for r in (std.get("host_rules") or []) + (std.get("repo_rules") or [])}
    if rule["id"] in taken:
        raise HTTPException(409, f"the standard already has a rule {rule['id']}")
    new = add_rule(text, kind, rule)
    try:
        back = (safe_load(new).get(kind) or [])[-1:]
    except YAMLError:
        back = []
    if back != [rule]:
        raise HTTPException(400, "the rule does not read back the same once written into conformance.yaml; add it by hand")
    if local:
        local.write_text(new, encoding="utf-8")
        return {"ok": True, "path": str(local)}
    branch = f"operations/rule-{rule['id']}-{int(time.time())}"
    title = f"Add {kind[:-6]} rule {rule['id']}: {rule['title']}"
    try:
        # start_sha pins the commit to the main this text was read from, so a change merged
        # since then shows in the MR as a conflict instead of being reverted.
        _gitlab(f"projects/{pid}/repository/commits", {
            "branch": branch, "start_sha": f["commit_id"], "commit_message": title,
            "actions": [{"action": "update", "file_path": "conformance.yaml", "content": new}]})
        mr = json.loads(_gitlab(f"projects/{pid}/merge_requests", {
            "source_branch": branch, "target_branch": "main", "title": title, "remove_source_branch": True,
            "description": f"Proposed from the Operations dashboard.\n\n```yaml\n{add_rule(kind + ':', kind, rule).strip()}\n```"}))
    except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        raise _gitlab_error(exc)
    return {"ok": True, "mr_url": mr["web_url"], "branch": branch}


# --- business mode -------------------------------------------------------------------------
# Business mode is detected, never installed: an account in a shared Hermes organisation, with
# Hermes skill sync on, shares the standard as an org skill named after the standard. The plugin
# sends every role the same request through Hermes' own sync client; the sync server decides
# whether it publishes or waits for an admin, and the page shows what it answered.
ORG_CHECK_S = 300
_org_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def org_mode(fresh: bool = False) -> Dict[str, Any]:
    """{mode: "business" | "single", org_id, org_name, role, reason}. org_id is set whenever the
    account is in a shared organisation, even when sync is off (reason says why it is single)."""
    if not fresh and _org_cache["value"] and time.time() - _org_cache["at"] < ORG_CHECK_S:
        return _org_cache["value"]
    out: Dict[str, Any] = {"mode": "single", "org_id": None, "org_name": None, "role": None, "reason": ""}
    try:
        from tools import skills_sync_client as ssc
        from tools.skills_sync_client_org import resolve_org_identity
        try:
            ident = resolve_org_identity()
        except ssc.SyncInertError as exc:
            out["reason"] = str(exc)
        else:
            claims = ident.get("claims") or {}
            out.update(org_id=ident["org_id"], role=ident["org_role"],
                       org_name=claims.get("org_name") or claims.get("org_slug") or ident["org_id"])
            if not ssc.sync_feature_enabled():
                out["reason"] = "Hermes skill sync is off (sync.enabled in config.yaml)"
            elif not ssc.resolve_sync_base_url():
                out["reason"] = "no sync server is set (sync.base_url)"
            else:
                out["mode"] = "business"
    except Exception as exc:  # an older Hermes without org sync, or credentials that fail to load
        out["reason"] = f"could not check for an organisation: {exc}"
    _org_cache.update(at=time.time(), value=out)
    return out


SOP_START, SOP_END = "<!-- operations:rules -->", "<!-- /operations:rules -->"


def sop_markdown(name: str, std: Dict[str, Any], old: str = "") -> str:
    """SKILL.md for a standard: the rules in prose for people, between markers so anything written
    around them by hand is kept, and a front matter marking it as a standard when it is new."""
    rules = []
    for kind, label in (("host_rules", "Host rule"), ("repo_rules", "Repo rule")):
        for r in std.get(kind) or []:
            applies = ", ".join(r.get("applies_to") or []) or "every host"
            rules.append(f"### {r.get('title') or r['id']} ({r['id']})\n\n{label}, {r.get('severity', 'warns')}"
                         + (f", applies to {applies}" if kind == "host_rules" else "")
                         + f".\n\nWhy: {str(r.get('why') or '').strip()}\n\nFix: {str(r.get('fix') or '').strip()}\n")
    block = f"{SOP_START}\n## Rules\n\n" + "\n".join(rules) + f"\n{SOP_END}"
    if SOP_START in old and SOP_END in old:
        return old[:old.index(SOP_START)] + block + old[old.index(SOP_END) + len(SOP_END):]
    if old:
        return old.rstrip() + "\n\n" + block + "\n"
    return (f"---\nname: {name}\ndescription: The {name} standard: what hosts and repos are checked against,"
            f" why, and how to fix a failure.\nkind: standard\n---\n\n# {name}\n\n"
            "The machine-readable rules are in conformance.yaml next to this file; the list below is"
            " generated from it.\n\n" + block + "\n")


def propose_to_org(kind: str, rule: Dict[str, Any], repo_text: str, repo_std: Dict[str, Any]) -> Dict[str, Any]:
    """Add the rule to the org skill named after the standard and propose it through Hermes' sync client.

    The skill is the org mirror's copy when there is one (edited in place, as Hermes edits org
    skills), else a new local one seeded from the repo's standard. Its conformance.yaml gets the
    rule and SKILL.md is regenerated from it. A failed send keeps the local edit, which
    `hermes sync propose <name>` sends later.
    """
    org = org_mode(fresh=True)
    if org["mode"] != "business":
        raise HTTPException(409, f"business mode is off: {org['reason']}")
    name = str(repo_std.get("standard") or "")
    if not _RULE_ID.fullmatch(name):
        raise HTTPException(409, "the standard has no `standard:` name to share it under")
    from tools import skills_sync_client as ssc
    from tools.skills_sync_client_org import propose_skill
    rel = ssc._skill_rel_path(name)  # the same lookup propose_skill makes
    skill = ssc._skills_dir() / (str(rel) if rel else f"standards/{name}")
    yml, md = skill / "conformance.yaml", skill / "SKILL.md"
    text = yml.read_text(encoding="utf-8") if yml.exists() else repo_text
    std = safe_load(text) or {}
    if rule["id"] in {r.get("id") for r in (std.get("host_rules") or []) + (std.get("repo_rules") or [])}:
        raise HTTPException(409, f"{name} already has a rule {rule['id']}")
    new = add_rule(text, kind, rule)
    try:
        new_std = safe_load(new)
    except YAMLError:
        new_std = {}
    if (new_std.get(kind) or [])[-1:] != [rule]:
        raise HTTPException(400, f"the rule does not read back the same once written into {name}'s conformance.yaml; add it by hand")
    skill.mkdir(parents=True, exist_ok=True)
    for path, body in ((yml, new), (md, sop_markdown(name, new_std, md.read_text(encoding="utf-8") if md.exists() else ""))):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
    try:
        res = propose_skill(name, message=f"Add {kind[:-6]} rule {rule['id']}: {rule['title']}")
    except Exception as exc:
        raise HTTPException(502, f"{exc}. The rule is saved in the local copy of {name}; run"
                                 f" `hermes sync propose {name}` to send it.")
    return {"ok": True, "skill": name, "org": org["org_name"],
            "status": "proposal_pending" if res.get("proposal_pending") else "published",
            "proposal_id": res.get("proposal_id")}


# --- roles ---------------------------------------------------------------------------------
# Who is looking: each role has a job description, the activity it watches and the Topology layout it
# opens with. The built-in ones are defaults; edits, additions and removals are saved for this install
# in plugin-data and can be reset to these.
_ROLES = get_default_hermes_root() / "plugin-data" / "operations" / "roles.json"
LAYOUTS = ("columns", "area", "radial", "switchboard", "arc", "burst")
WATCH_KINDS = ("incident", "change", "task", "conformance")
ROLE_DEFAULTS: List[Dict[str, Any]] = [
    {"id": "all", "name": "Everyone", "summary": "A shared view of the whole system for anyone on the team: what is up, what changed and what needs a person.",
     "responsibilities": ["Notice what needs attention and who it belongs to", "Follow a change or an incident from its first sign to its fix"],
     "skills": ["Reading dashboards and logs", "Knowing which team owns which part of the system"],
     "watch": list(WATCH_KINDS), "layout": "area"},
    {"id": "sre", "name": "SRE", "summary": "Site reliability engineering: keeps services available and fast within their objectives, leads incident response and removes the causes of repeat failures.",
     "responsibilities": ["Respond to incidents, mitigate them and lead the review afterwards", "Define service level objectives and track error budgets",
                          "Watch error rates, latency and saturation, and alert on what matters", "Plan capacity and failover so one failure does not take a service down",
                          "Automate away repetitive operational work"],
     "skills": ["Incident response and on-call", "Observability: metrics, logs and traces", "Service level indicators, objectives and error budgets",
                "Failure modes of distributed systems", "Linux and networking", "Scripting and automation (Python, shell)"],
     "watch": ["incident", "conformance"], "layout": "area"},
    {"id": "devops", "name": "DevOps", "summary": "Delivery and configuration: builds and runs the pipelines that ship changes, and keeps every environment matching its declared configuration.",
     "responsibilities": ["Build and maintain CI/CD pipelines", "Review and merge changes, and release or roll them back safely",
                          "Manage infrastructure and configuration as code", "Find and fix configuration drift between hosts and their standard",
                          "Keep environments consistent from development to production"],
     "skills": ["CI/CD systems and pipeline design", "Git workflows and code review", "Infrastructure as code and configuration management",
                "Containers and service orchestration", "Release engineering and rollback"],
     "watch": ["change", "task", "conformance"], "layout": "area"},
    {"id": "mlops", "name": "AI and MLOps", "summary": "Model operations: serves models reliably, routes requests to the right model and keeps quality, latency and cost in balance.",
     "responsibilities": ["Deploy, load and retire models on the serving pool", "Design model routing and fallbacks for each role",
                          "Evaluate model quality, latency and cost before and after a change", "Watch token use and failed model calls",
                          "Plan accelerator and memory capacity for the models in use"],
     "skills": ["Model serving and inference servers", "Model evaluation and benchmarking", "Routing, fallback and failover design",
                "Quantization and memory sizing", "Prompt and context management", "Python and ML tooling"],
     "watch": ["incident", "change"], "layout": "arc"},
    {"id": "cloud", "name": "Cloud and platform", "summary": "Platform and capacity: runs the hosts, pool and cloud providers the rest of the team builds on, and keeps them sized, reachable and affordable.",
     "responsibilities": ["Plan capacity for hosts and the serving pool", "Track cloud provider usage, limits and spend",
                          "Keep the platform reachable: networking, access and secrets", "Give other teams a reliable, documented platform",
                          "Balance local and cloud capacity for cost and resilience"],
     "skills": ["Cloud providers and their APIs", "Capacity planning", "Networking and access control", "Cost management",
                "Identity, access and secret management", "Platform engineering"],
     "watch": ["incident", "change", "conformance"], "layout": "radial"},
]
ROLE_TEXT_MAX, ROLE_LIST_MAX, ROLES_MAX = 600, 20, 30


def _clean_role(r: Any) -> Dict[str, Any]:
    """A role from the page, checked field by field: the file is read back by every viewer."""
    if not isinstance(r, dict):
        raise HTTPException(400, "each role is an object")
    rid, name = r.get("id"), r.get("name")
    if not isinstance(rid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", rid):
        raise HTTPException(400, "a role id is 1-40 lowercase letters, digits or dashes")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise HTTPException(400, f"role {rid}: a name of 1-60 characters is required")
    summary = r.get("summary", "")
    if not isinstance(summary, str) or len(summary) > ROLE_TEXT_MAX:
        raise HTTPException(400, f"role {rid}: the job description is text of at most {ROLE_TEXT_MAX} characters")
    out = {"id": rid, "name": name.strip(), "summary": summary.strip()}
    for key in ("responsibilities", "skills"):
        items = r.get(key, [])
        if not isinstance(items, list) or len(items) > ROLE_LIST_MAX or not all(isinstance(x, str) and len(x) <= 200 for x in items):
            raise HTTPException(400, f"role {rid}: {key} is a list of at most {ROLE_LIST_MAX} lines of 200 characters")
        out[key] = [x.strip() for x in items if x.strip()]
    watch = r.get("watch", [])
    if not isinstance(watch, list) or not all(isinstance(k, str) and k in WATCH_KINDS for k in watch):
        raise HTTPException(400, f"role {rid}: watch is a list of {', '.join(WATCH_KINDS)}")
    out["watch"] = [k for k in WATCH_KINDS if k in watch]
    if not isinstance(r.get("layout"), str) or r["layout"] not in LAYOUTS:
        raise HTTPException(400, f"role {rid}: layout is one of {', '.join(LAYOUTS)}")
    out["layout"] = r["layout"]
    return out


@router.get("/roles")
def roles_get() -> Dict[str, Any]:
    """The roles in use (this install's saved ones, else the built-in ones) and the built-in ones to reset to."""
    out: Dict[str, Any] = {"defaults": ROLE_DEFAULTS, "saved": False, "errors": {}}
    try:
        out["roles"] = [_clean_role(r) for r in json.loads(_ROLES.read_text(encoding="utf-8"))["roles"]]
        out["saved"] = True
    except FileNotFoundError:
        out["roles"] = ROLE_DEFAULTS
    except (OSError, ValueError, KeyError, TypeError, HTTPException) as exc:
        out["roles"] = ROLE_DEFAULTS
        out["errors"]["roles"] = f"{_ROLES} could not be read, so the built-in roles are shown: {getattr(exc, 'detail', exc)}"
    return out


@router.put("/roles")
def roles_put(roles: List[Any] = Body(..., embed=True)) -> Dict[str, Any]:
    """Save this install's roles. The page sends the whole list; the built-in ones are the reset."""
    if not roles or len(roles) > ROLES_MAX:
        raise HTTPException(400, f"between 1 and {ROLES_MAX} roles")
    clean = [_clean_role(r) for r in roles]
    if len({r["id"] for r in clean}) != len(clean):
        raise HTTPException(400, "role ids must be unique")
    _ROLES.parent.mkdir(parents=True, exist_ok=True)
    tmp = _ROLES.with_suffix(".tmp")
    tmp.write_text(json.dumps({"roles": clean}, indent=2), encoding="utf-8")
    os.replace(tmp, _ROLES)
    return {"roles": clean, "defaults": ROLE_DEFAULTS, "saved": True, "errors": {}}


@router.delete("/roles")
def roles_reset() -> Dict[str, Any]:
    """Back to the built-in roles."""
    _ROLES.unlink(missing_ok=True)
    return {"roles": ROLE_DEFAULTS, "defaults": ROLE_DEFAULTS, "saved": False, "errors": {}}


DRIFT_ACTIONS = ("button", "automatic", "off")


def _profile_config(name: str) -> Path:
    root = get_default_hermes_root()
    return root / "config.yaml" if name == "default" else root / "profiles" / name / "config.yaml"


def profile_states() -> List[Dict[str, Any]]:
    """Each profile with operations "on", "off" (someone put it in plugins.disabled) or "missing"."""
    out = []
    for name in kanban_db.list_profiles_on_disk():
        try:
            pl = (safe_load(_profile_config(name).read_text(encoding="utf-8")) or {}).get("plugins") or {}
        except (OSError, YAMLError):
            pl = {}
        state = ("off" if ID in (pl.get("disabled") or []) else
                 "on" if ID in (pl.get("enabled") or []) else "missing")
        out.append({"profile": name, "state": state})
    return out


@router.get("/setup")
def setup() -> Dict[str, Any]:
    """What a first run still needs, for the setup checklist on the Overview page."""
    s, own = _settings(), _settings_in(load_config())
    action = s.get("drift_action") if s.get("drift_action") in DRIFT_ACTIONS else "button"
    return {"profiles": profile_states(), "metrics": _METRICS_DB.exists(), "kanban": _KANBAN_DB.exists(),
            "conformance_project": _standard_project(),
            "alert_target": (s.get("alert_target") or "").strip(),
            "drift_action": action, "drift_profile": (s.get("drift_profile") or "").strip(),
            "profile": "default" if get_hermes_home() == get_default_hermes_root() else get_hermes_home().name,
            "settings_page": (get_hermes_home() / "plugins" / ID).is_dir(),
            "from_default": sorted(k for k in ("conformance_project", "alert_target", "drift_action", "drift_profile")
                                   if k in s and k not in own)}

