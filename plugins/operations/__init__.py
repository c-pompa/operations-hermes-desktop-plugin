"""Operations plugin, Python half: starts kanban cards the Handoffs page scheduled for later, and
registers the auxiliary model task Activity summaries use.

Scheduling a card parks it in kanban's own ``scheduled`` status with a note ending
``(operations:<epoch>)``. On every kanban dispatcher tick (the gateway's, about once a minute)
cards whose time has come go back to ``ready``, and the next tick starts the assigned profile's
worker. The note format is shared with dashboard/plugin_api.py, which writes it.

With ``alert_target`` set in this plugin's settings (``discord`` for the home channel, or
``discord:<channel>``), the same tick posts new Needs attention items there, so a host going quiet
or a gateway going fatal is seen without Desktop open.

With ``drift_action`` set to ``automatic`` and a ``drift_profile``, the tick also files the
Conformance tab's fix card for each host whose newest report fails a check, the same card the
"File a fix card" button files, at most once a day per host.
"""
import importlib.util
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from hermes_cli import kanban_db
from hermes_cli.kanban_db_connect import connect_closing

START_AT = re.compile(r"\(operations:(\d+)\)")
# Where the heartbeat is written, so the dashboard process can read what this one did. The shared
# root, like the plugin's other plugin-data files (dashboard/plugin_api.py resolves the same way).
HERMES_ROOT = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
HEARTBEAT_FILE = HERMES_ROOT / "plugin-data" / "operations" / "heartbeat.json"
# The kanban dispatcher ticks about once a minute; two missed intervals (this long) means the tick
# is not ours and the sweep runs on the plugin's own timer instead.
TICK_INTERVAL_S = 60
OWN_TIMER_CHECK_S = 30


def start_at(conn, task_id):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='scheduled'"
                       " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    m = START_AT.search((json.loads(row[0]) or {}).get("reason") or "") if row and row[0] else None
    return int(m.group(1)) if m else None


def release_due(conn, now):
    """Scheduled cards whose start time has passed go back to ready; returns their ids."""
    due = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status='scheduled'")
           if (start_at(conn, r[0]) or now + 1) <= now]
    return [t for t in due if kanban_db.unblock_task(conn, t)]


def _on_tick(board=None, dry_run=False, **_):
    if dry_run:
        return
    with connect_closing(board=board) as conn:
        release_due(conn, time.time())


ALERT_POLL_S = 60
_api = None
log = logging.getLogger(__name__)


def new_alerts(out, seen):
    """plugin.js startAlerts in Python: (items not in seen, keys to remember). The first run (seen
    None) only records; kanban is left to the Kanban plugin; an unreadable source forgets nothing."""
    items = [i for i in out.get("items") or [] if i.get("key") and i.get("source") != "kanban"]
    fresh = [i for i in items if i["key"] not in seen] if seen is not None else []
    keys = [i["key"] for i in items]
    if out.get("errors"):
        keys = list(dict.fromkeys([*(seen or []), *keys]))
    return fresh, keys


def alert_text(fresh):
    if len(fresh) > 3:
        return f"Operations: {len(fresh)} new items need attention\n" + "\n".join(f"- {i['text']}" for i in fresh)
    return "\n".join(f"Operations: {i['text']}" + (f" ({i['action']})" if i.get("action") else "") for i in fresh)


def _dashboard_api():
    global _api
    if _api is None:  # the dashboard half is a file, not a package
        spec = importlib.util.spec_from_file_location(
            "operations_api", Path(__file__).parent / "dashboard" / "plugin_api.py")
        _api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_api)
    return _api


def _alert(ctx, dry_run=False, **_):
    target = ctx.get_config("alert_target")
    # the tick can fire once per board; the stored time keeps it to one poll a minute
    if dry_run or not target or time.time() - (ctx.state.get("alerts.at") or 0) < ALERT_POLL_S - 5:
        return
    ctx.state.set("alerts.at", time.time())
    fresh, keys = new_alerts(_dashboard_api().attention_route(), ctx.state.get("alerts.seen"))
    if fresh:
        from tools.send_message_tool import send_message_tool
        try:
            res = json.loads(send_message_tool({"target": target, "message": alert_text(fresh)}))
        except Exception as exc:  # the platform's own failure; the tick goes on
            res = {"error": str(exc)}
        if res.get("error"):
            # not remembered as seen, so the next poll sends them again
            log.warning("operations alert to %s failed: %s", target, res["error"])
            return
    ctx.state.set("alerts.seen", keys)


DRIFT_POLL_S = 300
DRIFT_RETRY_S = 24 * 3600


def drift_due(hosts, last, now):
    """Hosts whose fresh, verified newest report fails or warns on a check, with no automatic card tried
    for a day. An unverified report (its ingest token could not vouch for the host) stays on the button
    flow: it must not auto-start a card."""
    return [h["host"] for h in hosts
            if h.get("verified", True) and not h.get("stale")
            and any(r["status"] in ("fail", "warn") for r in h["results"].values())
            and now - (last.get(h["host"]) or 0) >= DRIFT_RETRY_S]


def _drift(ctx, dry_run=False, **_):
    profile = ctx.get_config("drift_profile")
    if (dry_run or ctx.get_config("drift_action") != "automatic" or not profile
            or time.time() - (ctx.state.get("drift.at") or 0) < DRIFT_POLL_S):
        return
    api = _dashboard_api()
    if not (ctx.get_config("conformance_project") or api._LOCAL_STANDARD.is_file()):
        return
    ctx.state.set("drift.at", time.time())
    read = _metrics_read(api)
    if read is None or not api._KANBAN_DB.exists():
        return
    path, source = read
    if source == "none":
        return  # nothing to read host checks from; the page reports that
    m = api._ro(path)
    try:
        hosts = api.host_results(m, {}, time.time())
    finally:
        m.close()
    last = ctx.state.get("drift.last") or {}
    for host in drift_due(hosts, last, time.time()):
        try:
            api.drift_card(host, profile)
        except api.HTTPException as exc:
            # 409: nothing the standard declares is failing, so wait a day like a filed card.
            # Anything else (GitLab down, no such profile) is tried again on the next poll.
            if exc.status_code != 409:
                log.warning("operations: automatic fix card for %s failed: %s", host, exc.detail)
                continue
        last[host] = time.time()
    ctx.state.set("drift.last", last)


def read_heartbeat():
    try:
        data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_heartbeat(**fields):
    """Record what the last sweep did, for the page. Never fatal: a heartbeat it cannot write is
    reported on the plugin's status, not raised into a hook."""
    data = read_heartbeat()
    data.update(fields)
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(HEARTBEAT_FILE)
        return True
    except OSError as exc:
        log.warning("operations: could not write the heartbeat file: %s", exc)
        return False


def tick_due(last_tick, now, interval=TICK_INTERVAL_S):
    """No dispatcher tick within two intervals: the sweep must run on the plugin's own timer."""
    return last_tick is None or now - last_tick >= 2 * interval


def is_real_context(ctx):
    """True only for the context Hermes itself hands a plugin (a test's stand-in is not one)."""
    try:
        from hermes_cli.plugins import PluginContext
    except Exception:
        return False
    return isinstance(ctx, PluginContext)


def _metrics_read(api):
    """The store host checks are read from: the plugin's own store by default, the fleet DB as the
    fallback (dashboard/plugin_api.py decides), or None when there is nothing to read."""
    resolve = getattr(api, "metrics_source", None)
    if resolve is not None:
        try:
            return resolve()
        except Exception as exc:
            log.warning("operations: could not resolve the metrics store: %s", exc)
    return (api._METRICS_DB, "metricsdash") if api._METRICS_DB.exists() else None


def _collect(dry_run=False, **_):
    """Run the own-store collector (the dashboard half owns the code). A failure here is logged and
    also lands in the collector's own status, which GET /storage reports: never a silent catch."""
    if dry_run:
        return
    try:
        _dashboard_api().collect_once(kind="tick")
    except Exception as exc:
        log.warning("operations: metrics collection failed: %s", exc)


def sweep(ctx, by, now=None):
    """One alert + drift + collection sweep, marked with what ran it ("tick" | "own-timer")."""
    now = time.time() if now is None else now
    _collect()
    _alert(ctx)
    _drift(ctx)
    write_heartbeat(last_sweep=now, by=by)
    return by


def _on_heartbeat_tick(ctx, dry_run=False, **_):
    """A kanban dispatcher tick arrived: record it and sweep. The page then reads "heartbeat: tick"."""
    if dry_run:
        return None
    now = time.time()
    try:
        ctx.state.set("heartbeat.last_tick", now)
    except Exception as exc:
        log.warning("operations: could not record the dispatcher tick: %s", exc)
    return sweep(ctx, "tick", now)


def _heartbeat(ctx, dry_run=False, **_):
    """The fallback sweep, on the plugin's own timer: only when the dispatcher tick has gone quiet
    for two intervals. Marks the page "heartbeat: own timer" so the difference is visible."""
    if dry_run:
        return None
    now = time.time()
    last_tick = ctx.state.get("heartbeat.last_tick")
    if last_tick is not None and not tick_due(last_tick, now):
        return None
    return sweep(ctx, "own-timer", now)


_OWN_TIMER_STARTED = False


def start_own_timer(ctx):
    """A small daemon timer for hosts whose kanban dispatcher never ticks (no board).

    Not a collector daemon: it only decides, every OWN_TIMER_CHECK_S, whether the tick has gone
    quiet, and then runs the same sweep the tick would have run.
    """
    global _OWN_TIMER_STARTED
    if _OWN_TIMER_STARTED or os.environ.get("OPERATIONS_HEARTBEAT_TIMER") == "0":
        return
    _OWN_TIMER_STARTED = True

    def loop():
        while True:
            time.sleep(OWN_TIMER_CHECK_S)
            try:
                _heartbeat(ctx)
            except Exception as exc:  # the timer must outlive any one bad sweep
                log.warning("operations: own-timer heartbeat failed: %s", exc)

    threading.Thread(target=loop, name="operations-heartbeat", daemon=True).start()


def register(ctx):
    ctx.register_hook("on_kanban_dispatch_tick", _on_tick)
    ctx.register_hook("on_kanban_dispatch_tick", lambda **kw: _alert(ctx, **kw))
    ctx.register_hook("on_kanban_dispatch_tick", lambda **kw: _drift(ctx, **kw))
    # The heartbeat rides the same tick, but it also has to survive the tick never arriving: see
    # _heartbeat / start_own_timer.
    ctx.register_hook("on_kanban_dispatch_tick", lambda **kw: _on_heartbeat_tick(ctx, **kw))
    if is_real_context(ctx):
        # Only a real Hermes host turns the collector on: importing the dashboard half in a test
        # must never tail, or write to, a real install.
        api = _dashboard_api()
        api.enable_collection()
        start_own_timer(ctx)
    # Its own model slot, so Activity summaries (dashboard/plugin_api.py, opt-in) run on a small
    # model the user picks rather than the main one.
    ctx.register_auxiliary_task(
        "operations_summary", display_name="Operations activity summaries",
        description="Rewrites the Operations page's Activity entries as short readable sentences.")
