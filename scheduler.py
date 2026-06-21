"""Background scheduler — the only part of the app that acts on its own clock.

Sends time-of-day notifications that fire whether or not anyone has a page open:
  * mid-morning reminder (default 10:00) if a kid's checklist isn't done
  * Sunday 7pm household week summary

A single minute-interval job checks both; `notifications_sent` (UNIQUE on
kid_id+date+type) makes every send once-per-day idempotent, so polling is safe.
Runs in the single Flask process — never start more than one.
"""
import logging
import os
from datetime import time, timedelta

import requests

import db
import logic
import notify

log = logging.getLogger("chore.scheduler")

# Household-level notifications (the weekly summary) use this sentinel kid_id so
# the notifications_sent UNIQUE constraint dedups them (SQLite lets NULLs repeat).
HOUSEHOLD = 0


def _parse_hhmm(s):
    try:
        h, m = (s or "").split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def _hr(mins):
    """Minutes -> compact hours string: 300 -> '5', 210 -> '3.5'."""
    return ("%.1f" % (mins / 60)).rstrip("0").rstrip(".")


def build_summary(conn, d):
    """Sunday-summary message for the week ending on d."""
    ws = logic.week_start(d)
    acts = logic.activities(conn, enabled_only=True)
    parts = []
    for kid in logic.active_kids(conn):
        targets = logic.prorated_targets(conn, kid, ws)
        bits = []
        for a in acts:
            cur = logic.weekly_activity_total(conn, a["id"], kid["id"], ws)
            tgt = targets["by_activity"].get(a["id"], 0)
            mark = "✓" if cur >= tgt else "✗"
            if a["unit"] == "minutes":
                bits.append("%s %s/%s hr %s" % (a["label"], _hr(cur), _hr(tgt), mark))
            else:
                bits.append("%s %d/%d %s" % (a["label"], cur, tgt, mark))
        parts.append("%s: %s" % (kid["name"], ", ".join(bits) if bits else "checklist only"))
    return " ".join(parts)


def maybe_morning_reminder(conn, now, d):
    """Fire the mid-morning reminder for any kid who hasn't finished today."""
    if logic.is_paused(conn, d):
        return                                  # no reminders on paused vacation days
    rt = logic.get_setting(conn, "reminder_time", "10:00")
    if rt == "off":
        return
    t = _parse_hhmm(rt)
    if t is None or now.time() < t:
        return
    app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
    for kid in logic.active_kids(conn):
        if not logic.assigned_daily_chores(conn, kid["id"], d):
            continue                            # this kid has no daily chores -> skip
        done, _ = logic.checklist_status(conn, kid["id"], d)
        if done:
            continue
        notify.send_once(conn, kid["id"], "morning_reminder", "%s 🔔" % app_name,
                         "%s hasn't finished their checklist yet." % kid["name"], d)


def maybe_sunday_summary(conn, now, d):
    """Fire the household week summary on Sunday at/after 7pm (once)."""
    if d.weekday() != 6 or now.hour < 19:       # Sunday == 6
        return
    if logic.is_paused(conn, d):                # paused week -> no summary
        return
    app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
    notify.send_once(conn, HOUSEHOLD, "weekly_summary", "%s — Week Summary" % app_name,
                     build_summary(conn, d), d)


def maybe_monday_recap(conn, now, d):
    """Monday morning: fire a recap of last week's results (once)."""
    if d.weekday() != 0 or now.hour < 8:   # Monday only, after 8am
        return
    if logic.is_paused(conn, d):
        return
    last_sunday = d - timedelta(days=1)
    app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
    notify.send_once(conn, HOUSEHOLD, "monday_recap",
                     "%s — Last Week" % app_name,
                     build_summary(conn, last_sunday), d)


def maybe_scheduled_due(conn, now, d):
    """On a scheduled chore's due evening, Pushover once if it isn't done yet."""
    if now.hour < 17:                            # give them the day; nudge in the evening
        return
    ws = logic.week_start(d)
    app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
    for ch in conn.execute(
            "SELECT * FROM chores WHERE type='scheduled' AND active=1").fetchall():
        if ch["due_weekday"] != d.weekday():
            continue
        for kid in logic.active_kids(conn):
            if not logic.chore_assigned_to(conn, ch, kid["id"], ws):
                continue
            st = logic.scheduled_state(conn, kid["id"], ch, d)
            if st["state"] in ("due_today", "overdue"):
                label = " (%s)" % ch["due_label"] if ch["due_label"] else ""
                notify.send_once(
                    conn, kid["id"], "scheduled_due:%d" % ch["id"], "%s 🔔" % app_name,
                    "Reminder: %s — %s%s is due tonight." % (kid["name"], ch["name"], label),
                    d)


def heartbeat(env=None):
    """Ping a push-monitor URL if HEALTHCHECK_URL is set.

    Because this runs from the scheduler tick, a received heartbeat proves the
    background job is alive — not just that the web server answers. Works with
    any HTTP push monitor (Uptime Kuma, Healthchecks.io, etc.). Fire-and-forget:
    a failure is logged, never raised.
    """
    env = env if env is not None else os.environ
    url = (env.get("HEALTHCHECK_URL") or "").strip()
    if not url:
        return False
    # If the URL already has a query string (some monitors ship with
    # ?status=up&msg=OK), use it as-is to avoid duplicating parameters.
    if "?" not in url:
        url = url + "?status=up&msg=OK"
    try:
        requests.get(url, timeout=5)
        return True
    except Exception as exc:  # noqa: BLE001 - heartbeat is best-effort
        log.warning("Heartbeat ping failed: %s", exc)
        return False


def run_tick(env=None):
    """One scheduler tick: fire anything due, then heartbeat. Own connection."""
    conn = db.connect()
    try:
        now = logic.now(env)
        d = now.date()
        if logic.in_program_window(conn, d):    # off-season: no reminders/summaries
            maybe_morning_reminder(conn, now, d)
            maybe_scheduled_due(conn, now, d)
            maybe_sunday_summary(conn, now, d)
            maybe_monday_recap(conn, now, d)
    finally:
        conn.close()
    heartbeat(env)                              # always — proves the tick ran


def _safe_tick(env):
    try:
        run_tick(env)
    except Exception as exc:                     # never let a tick crash the scheduler
        log.error("scheduler tick failed: %s", exc)


def start(env=None):
    """Start the minute-interval background scheduler. Call once, in the server."""
    from apscheduler.schedulers.background import BackgroundScheduler
    conn = db.connect()
    try:
        tz = logic.get_tz(env=env, conn=conn)
    finally:
        conn.close()
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(lambda: _safe_tick(env), "interval", minutes=1,
                  id="tick", max_instances=1, coalesce=True)
    sched.start()
    log.info("Scheduler started (minute interval).")
    return sched

