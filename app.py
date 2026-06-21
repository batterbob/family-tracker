"""ChoreBoard — Flask entrypoint.

Single-process (no gunicorn workers) so background scheduling fires once.
"""
import collections
import os
import re
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, g, jsonify, render_template, request, abort,
                   session, redirect)

import db
import logic
import notify
import scheduler

logging.basicConfig(level=logging.INFO)

# In-memory ring buffer — keeps the last 500 log records for /admin/applog.
_LOG_BUFFER = collections.deque(maxlen=500)

class _RingHandler(logging.Handler):
    def emit(self, record):
        _LOG_BUFFER.append({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "msg": self.format(record),
        })

_ring = _RingHandler()
_ring.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_ring)

app = Flask(__name__)

# Ensure the data dir exists, then create/seed the DB once at startup.
os.makedirs(db.DATA_DIR, exist_ok=True)
with db.connect() as _c:
    db.init_db(_c, os.environ)
    # Stable secret so admin sessions survive restarts (seeded once on first run).
    app.secret_key = logic.get_setting(_c, "flask_secret") or os.urandom(32).hex()
app.permanent_session_lifetime = timedelta(days=7)


# --------------------------------------------------------------------------- #
# Per-request DB connection
# --------------------------------------------------------------------------- #
def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def effective_today():
    """Local 'today', with an optional ?today=/JSON override when CHORE_DEBUG=1.

    The override is for manually exercising in-program-window dates before the
    season starts; it is inert unless CHORE_DEBUG is enabled.
    """
    if os.environ.get("CHORE_DEBUG", "0") in ("1", "true", "True"):
        override = request.args.get("today")
        if not override and request.is_json:
            override = (request.get_json(silent=True) or {}).get("today")
        if not override and request.form:
            override = request.form.get("today")
        if override:
            try:
                return logic.s2d(override)
            except ValueError:
                pass
    return logic.today()


@app.after_request
def _no_store(resp):
    """Mobile Safari caches aggressively; force fresh totals on kid/status pages."""
    p = request.path
    if (p == "/status" or p == "/healthz" or p.rstrip("/") in _KID_PATHS
            or p.startswith("/api/") or p.startswith("/admin")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def require_admin(f):
    """Guard for /admin* routes — redirect to the login page when not signed in."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return wrapper


_KID_PATHS = set()  # filled after we know the slugs (below)


# --------------------------------------------------------------------------- #
# Global template context (injected into every render_template call)
# --------------------------------------------------------------------------- #
@app.context_processor
def inject_globals():
    conn = get_db()
    g_set = lambda key, default: logic.get_setting(conn, key, default)
    return {
        "app_name": g_set("app_name", "ChoreBoard"),
        "program_label": g_set("program_label", "Activity Tracker"),
        "reading_label": g_set("reading_label", "Reading"),
        "reading_enabled": g_set("reading_enabled", "1") != "0",
        "outdoor_label": g_set("outdoor_label", "Outdoor Time"),
        "outdoor_enabled": g_set("outdoor_enabled", "1") != "0",
        "nav_kids": [{"name": k["name"], "slug": k["url_slug"]}
                     for k in logic.active_kids(conn)],
    }


# --------------------------------------------------------------------------- #
# First-run detection
# --------------------------------------------------------------------------- #
def is_first_run(conn):
    """True when setup hasn't been completed and no kids exist yet."""
    if logic.get_setting(conn, "setup_complete", "0") == "1":
        return False
    return len(logic.active_kids(conn)) == 0


# --------------------------------------------------------------------------- #
# Shared view-model builders (reused by the page render and the JSON APIs)
# --------------------------------------------------------------------------- #
def _fmt_hm(mins):
    h, m = divmod(int(mins), 60)
    if h and m:
        return "%d hr %d min" % (h, m)
    if h:
        return "%d hr" % h
    return "%d min" % m


def _fmt_time(iso):
    """ISO timestamp -> '8:15 AM' (local, as stored). None passes through."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return "%d:%02d %s" % (dt.hour % 12 or 12, dt.minute,
                           "AM" if dt.hour < 12 else "PM")


def _pct(value, target):
    return min(100, int(value / target * 100)) if target > 0 else 0


def log_section(conn, kid, kind, d):
    """View-model for a reading/outdoor log section, used by render + API."""
    table = "reading_logs" if kind == "reading" else "outdoor_logs"
    ws = logic.week_start(d)
    targets = logic.prorated_targets(conn, kid, ws)
    target = targets["reading"] if kind == "reading" else targets["outdoor"]
    weekly = (logic.weekly_reading if kind == "reading" else logic.weekly_outdoor)(
        conn, kid["id"], ws)
    today_total = logic.today_minutes(conn, table, kid["id"], d)
    entries = [{"id": r["id"], "minutes": r["minutes"]}
               for r in logic.today_entries(conn, table, kid["id"], d)]

    in_program = logic.in_program_window(conn, d) and not logic.is_paused(conn, d)
    pace_state, pace_needed = (logic.pace(conn, target, weekly, ws, d)
                               if in_program else ("inactive", None))
    return {
        "kind": kind,
        "weekly": weekly,
        "target": target,
        "today_total": today_total,
        "entries": entries,
        "pace_state": pace_state,
        "pace_needed": pace_needed,
        "met": weekly >= target and target > 0,
        "weekly_hm": _fmt_hm(weekly),
        "target_hm": _fmt_hm(target),
    }


def kid_view(conn, kid, d):
    """Assemble the full kid-page context."""
    logic.finalize_past_weeks(conn, d)            # finalize any past weeks first
    logic.ensure_camp_credit(conn, d)             # idempotent camp outdoor credit
    logic.ensure_rotation_for_week(conn, d)       # auto-advance rotating chores

    banner = logic.banner_state(conn, kid, d)
    ws = logic.week_start(d)

    daily = logic.assigned_daily_chores(conn, kid["id"], d)
    done_map = logic.completed_chore_ids(conn, kid["id"], d)
    daily_rows = [{"id": c["id"], "name": c["name"], "notes": c["notes"] or "",
                   "done": c["id"] in done_map, "kind": c["type"],
                   "overdue": (c["type"] == "alternate_daily"
                               and logic.alt_daily_is_overdue(c, d))}
                  for c in daily]
    checklist_done, completed_at = logic.checklist_status(conn, kid["id"], d)

    weekly = logic.weekly_chores_for_kid(conn, kid["id"], d)
    scheduled = logic.scheduled_for_kid(conn, kid["id"], d)

    as_needed = [{"id": a["id"], "name": a["name"],
                  "done": a["completed_at"] is not None}
                 for a in logic.as_needed_for_kid(conn, kid["id"], d)]

    stars, streak = logic.scoreboard(conn, kid["id"])
    reward = logic.get_setting(conn, "scoreboard_reward_text", "") or ""

    # Bonus history (last 5 finalized weeks)
    bonus_history = []
    for row in logic.kid_bonus_history(conn, kid["id"]):
        ws_h = logic.s2d(row["week_start_date"])
        we_h = logic.week_end(ws_h)
        label_h = ws_h.strftime("%b ") + str(ws_h.day) + "–" + str(we_h.day)
        bonus_history.append({
            "label": label_h,
            "earned": row["bonus_earned"] == 1,
            "reading": row["reading_minutes"],
            "reading_target": row["reading_target"],
            "outdoor_hm": _fmt_hm(row["outdoor_minutes"]),
            "outdoor_target_hm": _fmt_hm(row["outdoor_target"]),
        })

    # Allowance
    bonus_dollars_str = logic.get_setting(conn, "bonus_dollar_amount", "") or ""
    bonus_dollars = None
    total_earned = None
    if bonus_dollars_str:
        try:
            bonus_dollars = float(bonus_dollars_str)
            total_earned = bonus_dollars * stars
        except ValueError:
            pass

    debug = os.environ.get("CHORE_DEBUG", "0") in ("1", "true", "True")
    return {
        "kid": kid,
        "debug": debug,
        "render_today": logic.d2s(d),
        "today_long": d.strftime("%A, %B ") + str(d.day),
        "banner": banner,
        "rotation": logic.rotating_chore_for_kid(conn, kid["id"], ws),
        "daily": daily_rows,
        "checklist_done": checklist_done,
        "completed_at": completed_at,
        "weekly": weekly,
        "scheduled": scheduled,
        "as_needed": as_needed,
        "reading": log_section(conn, kid, "reading", d),
        "outdoor": log_section(conn, kid, "outdoor", d),
        "stars": stars,
        "streak": streak,
        "reward": reward,
        "makeup": logic.makeup_banner(conn, kid["id"], d),
        "last_week_bonus": logic.last_week_bonus(conn, kid["id"], d),
        "bonus_history": bonus_history,
        "bonus_dollars": bonus_dollars,
        "total_earned": total_earned,
        "reading_quick": [15, 25, 30, 60],
        "outdoor_quick": [15, 30, 60, 90],
    }


def status_view(conn, d):
    """Read-only at-a-glance view of both kids for the parent /status page."""
    logic.finalize_past_weeks(conn, d)
    logic.ensure_camp_credit(conn, d)
    logic.ensure_rotation_for_week(conn, d)

    ws = logic.week_start(d)
    in_program = logic.in_program_window(conn, d)
    cards = []
    for kid in logic.active_kids(conn):
        targets = logic.prorated_targets(conn, kid, ws)
        r = logic.weekly_reading(conn, kid["id"], ws)
        o = logic.weekly_outdoor(conn, kid["id"], ws)
        done, completed_at = logic.checklist_status(conn, kid["id"], d)
        as_needed = [{"name": a["name"],
                      "done": a["completed_at"] is not None,
                      "time": _fmt_time(a["completed_at"])}
                     for a in logic.as_needed_for_kid(conn, kid["id"], d)]
        weekly_done = [{"name": w["name"], "done": w["done"]}
                       for w in logic.weekly_chores_for_kid(conn, kid["id"], d)]
        cards.append({
            "name": kid["name"],
            "checklist_done": done,
            "completed_time": _fmt_time(completed_at),
            "as_needed": as_needed,
            "weekly": weekly_done,
            "reading": {"weekly": r, "target": targets["reading"],
                        "pct": _pct(r, targets["reading"])},
            "outdoor": {"weekly_hm": _fmt_hm(o), "target_hm": _fmt_hm(targets["outdoor"]),
                        "pct": _pct(o, targets["outdoor"])},
            "on_break": logic.is_paused(conn, d),
            "last_week_bonus": logic.last_week_bonus(conn, kid["id"], d),
        })
    return {
        "today_long": d.strftime("%A, %B ") + str(d.day),
        "in_program": in_program,
        "cards": cards,
    }


def admin_view(conn, d):
    """Read-only dashboard data: today's status + this week's progress per kid."""
    logic.finalize_past_weeks(conn, d)
    logic.ensure_camp_credit(conn, d)
    logic.ensure_rotation_for_week(conn, d)

    ws = logic.week_start(d)
    cards = []
    for kid in logic.active_kids(conn):
        done, completed_at = logic.checklist_status(conn, kid["id"], d)
        comp_days, elapsed_days = logic.checklist_days_this_week(conn, kid["id"], d)
        targets = logic.prorated_targets(conn, kid, ws)
        done_ids = logic.completed_chore_ids(conn, kid["id"], d)
        assigned_daily = logic.assigned_daily_chores(conn, kid["id"], d)
        cards.append({
            "id": kid["id"],
            "name": kid["name"],
            "slug": kid["url_slug"],
            "checklist_done": done,
            "completed_time": _fmt_time(completed_at),
            "on_break": logic.is_paused(conn, d),
            "as_needed": [{"id": a["id"], "name": a["name"],
                           "done": a["completed_at"] is not None,
                           "time": _fmt_time(a["completed_at"])}
                          for a in logic.as_needed_for_kid(conn, kid["id"], d)],
            "checklist_days": comp_days,
            "checklist_elapsed": elapsed_days,
            "reading": log_section(conn, kid, "reading", d),
            "outdoor": log_section(conn, kid, "outdoor", d),
            "last_week_bonus": logic.last_week_bonus(conn, kid["id"], d),
            "prorated": targets["active_days"] < 7,
            "active_days": targets["active_days"],
            "reading_target": targets["reading"],
            "outdoor_target": targets["outdoor"],
            "daily_incomplete": [{"id": c["id"], "name": c["name"]}
                                 for c in assigned_daily if c["id"] not in done_ids],
            "daily_complete": [{"id": c["id"], "name": c["name"]}
                               for c in assigned_daily if c["id"] in done_ids],
        })

    # Chore list with per-chore assignment state for the table's assign toggles.
    assigned_map = {}
    for r in conn.execute("SELECT chore_id, kid_id FROM weekly_assignments").fetchall():
        assigned_map.setdefault(r["chore_id"], set()).add(r["kid_id"])
    chore_rows = []
    for r in conn.execute(
            "SELECT id, name, type, active, is_rotating, due_weekday, "
            "reminder_lead_days, due_label, alt_day_parity FROM chores WHERE deleted=0 "
            "ORDER BY type, id").fetchall():
        chore_rows.append(dict(r, assigned_ids=sorted(assigned_map.get(r["id"], set()))))

    as_needed_chores = conn.execute(
        "SELECT id, name FROM chores WHERE type='as_needed' AND active=1 AND deleted=0 "
        "ORDER BY id").fetchall()
    debug = os.environ.get("CHORE_DEBUG", "0") in ("1", "true", "True")
    return {
        "today_long": d.strftime("%A, %B ") + str(d.day),
        "in_program": logic.in_program_window(conn, d),
        "cards": cards,
        "all_chores": chore_rows,
        "kids_list": [{"id": k["id"], "name": k["name"]} for k in logic.active_kids(conn)],
        "as_needed_chores": [dict(r) for r in as_needed_chores],
        "rotation": logic.rotation_table(conn, d),
        "debug_today": logic.d2s(d) if debug else None,
        "setup_done": request.args.get("setup_done"),
    }


def _int_or_none(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _valid_date(s):
    try:
        logic.s2d(s)
        return True
    except (ValueError, TypeError):
        return False


def _valid_time(s):
    return bool(re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", s or ""))


def _specials_with_paused(conn):
    """Return special_periods rows enriched with paused_chore_ids (a set of ints)."""
    result = []
    for sp in logic.special_periods(conn):
        rows = conn.execute(
            "SELECT chore_id FROM special_period_paused_chores WHERE special_period_id=?",
            (sp["id"],)).fetchall()
        result.append(dict(sp, paused_chore_ids={r["chore_id"] for r in rows}))
    return result


def settings_view(conn):
    kids = [{"id": k["id"], "name": k["name"], "slug": k["url_slug"],
             "reading_target": k["reading_target_minutes"],
             "outdoor_target": k["outdoor_target_minutes"],
             "has_passphrase": bool(k["passphrase_hash"])}
            for k in logic.active_kids(conn)]
    all_kids = conn.execute("SELECT * FROM kids ORDER BY id").fetchall()
    g = lambda key, default="": logic.get_setting(conn, key, default)
    return {
        "kids": kids,
        "all_kids": [dict(k) for k in all_kids],
        "app_name_val": g("app_name", "ChoreBoard"),
        "program_label_val": g("program_label", "Activity Tracker"),
        "reading_label_val": g("reading_label", "Reading"),
        "reading_enabled_val": g("reading_enabled", "1") != "0",
        "outdoor_label_val": g("outdoor_label", "Outdoor Time"),
        "outdoor_enabled_val": g("outdoor_enabled", "1") != "0",
        "timezone_val": g("timezone", "America/New_York"),
        "reminder_time": g("reminder_time", "10:00"),
        "program_start": g("program_start_date", ""),
        "program_end": g("program_end_date", ""),
        "reward": g("scoreboard_reward_text", ""),
        "bonus_dollar_amount": g("bonus_dollar_amount", ""),
        "notify_service": g("notify_service", "none"),
        "notify_pushover_app_token": g("notify_pushover_app_token", ""),
        "notify_pushover_user_key": g("notify_pushover_user_key", ""),
        "notify_telegram_token": g("notify_telegram_token", ""),
        "notify_telegram_chatid": g("notify_telegram_chatid", ""),
        "notify_discord_webhook": g("notify_discord_webhook", ""),
        "notify_slack_webhook": g("notify_slack_webhook", ""),
        "notify_ntfy_topic": g("notify_ntfy_topic", ""),
        "notify_ntfy_host": g("notify_ntfy_host", ""),
        "notify_gotify_url": g("notify_gotify_url", ""),
        "notify_gotify_token": g("notify_gotify_token", ""),
        "notify_urls": g("notify_urls", ""),
        "passphrase_required_val": g("passphrase_required", "0") == "1",
        "notify_test": request.args.get("notify_test"),
        "specials": _specials_with_paused(conn),
        "pauseable_chores": [dict(r) for r in conn.execute(
            "SELECT id, name, type FROM chores "
            "WHERE type IN ('daily','alternate_daily','weekly','scheduled') "
            "AND active=1 AND deleted=0 ORDER BY type, id").fetchall()],
        "saved": request.args.get("saved"),
        "pwerror": request.args.get("pwerror"),
    }


def history_view(conn, d):
    """Week-by-week breakdown (newest first) + per-kid streak/stars summary."""
    logic.finalize_past_weeks(conn, d)
    kids = logic.active_kids(conn)
    daily = logic.active_daily_chores(conn)

    summary = []
    for kid in kids:
        stars, streak = logic.scoreboard(conn, kid["id"])
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM weekly_results WHERE kid_id=? AND is_paused_week=0",
            (kid["id"],)).fetchone()["n"]
        summary.append({"name": kid["name"], "stars": stars, "total": total,
                        "streak": streak})

    weeks = []
    for wr in conn.execute("SELECT DISTINCT week_start_date FROM weekly_results "
                           "ORDER BY week_start_date DESC").fetchall():
        ws = logic.s2d(wr["week_start_date"])
        we = logic.week_end(ws)
        kcards = []
        for kid in kids:
            res = conn.execute(
                "SELECT * FROM weekly_results WHERE kid_id=? AND week_start_date=?",
                (kid["id"], wr["week_start_date"])).fetchone()
            if res is None:
                continue
            comp, active = logic.checklist_days_in_week(conn, kid["id"], ws)
            breakdown = []
            for ch in daily:
                cnt = conn.execute(
                    "SELECT COUNT(*) AS n FROM chore_completions WHERE kid_id=? "
                    "AND chore_id=? AND completion_date>=? AND completion_date<=?",
                    (kid["id"], ch["id"], logic.d2s(ws), logic.d2s(we))).fetchone()["n"]
                breakdown.append({"name": ch["name"], "count": cnt})
            kcards.append({
                "name": kid["name"],
                "paused": res["is_paused_week"] == 1,
                "bonus": res["bonus_earned"],
                "reading": res["reading_minutes"], "reading_target": res["reading_target"],
                "reading_met": res["reading_minutes"] >= res["reading_target"],
                "outdoor_hm": _fmt_hm(res["outdoor_minutes"]),
                "outdoor_target_hm": _fmt_hm(res["outdoor_target"]),
                "outdoor_met": res["outdoor_minutes"] >= res["outdoor_target"],
                "active_days": res["active_days"],
                "checklist_days": comp, "checklist_active": active,
                "breakdown": breakdown,
            })
        weeks.append({
            "label": "%s – %s" % (ws.strftime("%b ") + str(ws.day),
                                  we.strftime("%b ") + str(we.day)),
            "kids": kcards,
            "prorated": any((not k["paused"]) and k["active_days"] < 7 for k in kcards),
        })
    return {"summary": summary, "weeks": weeks}


def logs_view(conn, d):
    """Reading/outdoor entries for the current week, per kid, for admin editing."""
    ws = logic.week_start(d)
    we = logic.week_end(ws)
    kids = []
    for kid in logic.active_kids(conn):
        rows = {}
        for kind, table in (("reading", "reading_logs"), ("outdoor", "outdoor_logs")):
            rows[kind] = [dict(r) for r in conn.execute(
                "SELECT id, log_date, minutes, source FROM %s "
                "WHERE kid_id=? AND log_date >= ? AND log_date <= ? "
                "ORDER BY log_date, id" % table,
                (kid["id"], logic.d2s(ws), logic.d2s(we))).fetchall()]
        kids.append({"name": kid["name"], "reading": rows["reading"],
                     "outdoor": rows["outdoor"]})
    debug = os.environ.get("CHORE_DEBUG", "0") in ("1", "true", "True")
    return {
        "week_label": "%s – %s" % (ws.strftime("%b ") + str(ws.day),
                                   we.strftime("%b ") + str(we.day)),
        "kids": kids,
        "debug_today": logic.d2s(d) if debug else None,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    conn = get_db()
    if is_first_run(conn):
        return redirect("/setup")
    return redirect("/status")


@app.route("/healthz")
def healthz():
    """Lightweight health check for Uptime Kuma — verifies the DB is reachable.

    Public (no auth) and exposes nothing sensitive. 200 = healthy, 503 = down.
    """
    try:
        get_db().execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001 - any DB failure means unhealthy
        return jsonify({"status": "error"}), 503
    return jsonify({"status": "ok"}), 200


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    conn = get_db()
    error = None
    if request.method == "POST":
        stored = logic.get_setting(conn, "admin_password_hash", "")
        if db.verify_password(stored, request.form.get("password", "")):
            session.permanent = True
            session["admin"] = True
            return redirect("/admin")
        error = "Incorrect password."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")


@app.route("/admin")
@require_admin
def admin_dashboard():
    conn = get_db()
    d = effective_today()
    return render_template("admin.html", **admin_view(conn, d))


def _admin_redirect():
    """Redirect back to the dashboard, preserving the debug ?today if present."""
    today = request.form.get("today")
    return redirect("/admin?today=" + today if today else "/admin")


def _schedule_fields(ctype):
    """Parse the scheduled-chore fields from the form; returns
    (due_weekday, lead_days, due_label). Rotation is set via the table toggle."""
    if ctype != "scheduled":
        return (None, None, None)
    return (
        _int_or_none(request.form.get("due_weekday")),
        _int_or_none(request.form.get("reminder_lead_days")) or 0,
        (request.form.get("due_label") or "").strip(),
    )


def _alt_daily_fields(ctype):
    """Parse alt_day_parity (0=even, 1=odd) for alternate_daily chores."""
    if ctype != "alternate_daily":
        return None
    val = request.form.get("alt_day_parity", "0")
    return 1 if val == "1" else 0


@app.route("/admin/chore/add", methods=["POST"])
@require_admin
def admin_chore_add():
    conn = get_db()
    name = (request.form.get("name") or "").strip()
    ctype = request.form.get("type")
    if name and ctype in ("daily", "weekly", "as_needed", "scheduled", "alternate_daily"):
        wd, lead, label = _schedule_fields(ctype)
        parity = _alt_daily_fields(ctype)
        notes = (request.form.get("notes") or "").strip()
        conn.execute(
            "INSERT INTO chores (name, type, is_rotating, active, deleted, "
            "created_at, due_weekday, reminder_lead_days, due_label, alt_day_parity, notes) "
            "VALUES (?,?,0,1,0,?,?,?,?,?,?)",
            (name, ctype, logic.now_iso(), wd, lead, label, parity, notes or None))
        conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/edit", methods=["POST"])
@require_admin
def admin_chore_edit():
    """Rename a chore and/or change its type."""
    conn = get_db()
    name = (request.form.get("name") or "").strip()
    ctype = request.form.get("type")
    chore_id = request.form.get("chore_id")
    if name and ctype in ("daily", "weekly", "as_needed", "scheduled", "alternate_daily"):
        # is_rotating is owned by the chores-table Rotate toggle, never the form.
        cur = conn.execute("SELECT is_rotating FROM chores WHERE id=?",
                           (chore_id,)).fetchone()
        rot = cur["is_rotating"] if cur else 0
        wd, lead, label = _schedule_fields(ctype)
        parity = _alt_daily_fields(ctype)
        notes = (request.form.get("notes") or "").strip()
        conn.execute(
            "UPDATE chores SET name=?, type=?, is_rotating=?, due_weekday=?, "
            "reminder_lead_days=?, due_label=?, alt_day_parity=?, notes=? WHERE id=? AND deleted=0",
            (name, ctype, rot, wd, lead, label, parity, notes or None, chore_id))
        conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/toggle", methods=["POST"])
@require_admin
def admin_chore_toggle():
    conn = get_db()
    conn.execute("UPDATE chores SET active = 1 - active WHERE id=? AND deleted=0",
                 (request.form.get("chore_id"),))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/delete", methods=["POST"])
@require_admin
def admin_chore_delete():
    conn = get_db()
    conn.execute("UPDATE chores SET deleted=1, active=0 WHERE id=?",
                 (request.form.get("chore_id"),))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/kid/<int:kid_id>/complete-all", methods=["POST"])
@require_admin
def admin_complete_all(kid_id):
    """Mark every incomplete daily chore done for a kid today (parent-verified)."""
    conn = get_db()
    d = effective_today()
    kid = conn.execute("SELECT * FROM kids WHERE id=? AND active=1", (kid_id,)).fetchone()
    if kid is None:
        abort(404)
    done_map = logic.completed_chore_ids(conn, kid_id, d)
    for c in logic.assigned_daily_chores(conn, kid_id, d):
        if c["id"] not in done_map:
            conn.execute(
                "INSERT OR IGNORE INTO chore_completions (kid_id, chore_id, "
                "completion_date, completed_at, parent_verified) VALUES (?,?,?,?,1)",
                (kid_id, c["id"], logic.d2s(d), logic.now_iso()))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/checklist/override", methods=["POST"])
@require_admin
def admin_checklist_override():
    """Mark a daily chore done for a kid today, flagged parent_verified."""
    conn = get_db()
    d = effective_today()
    kid_id = request.form.get("kid_id")
    chore_id = request.form.get("chore_id")
    chore = conn.execute(
        "SELECT 1 FROM chores WHERE id=? AND type IN ('daily', 'alternate_daily') AND active=1",
        (chore_id,)).fetchone()
    if chore:
        conn.execute(
            "INSERT OR IGNORE INTO chore_completions (kid_id, chore_id, "
            "completion_date, completed_at, parent_verified) VALUES (?,?,?,?,1)",
            (kid_id, chore_id, logic.d2s(d), logic.now_iso()))
        conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/uncomplete", methods=["POST"])
@require_admin
def admin_chore_uncomplete():
    """Remove today's completion record for a daily chore (parent override)."""
    conn = get_db()
    d = effective_today()
    kid_id = request.form.get("kid_id")
    chore_id = request.form.get("chore_id")
    conn.execute(
        "DELETE FROM chore_completions WHERE kid_id=? AND chore_id=? AND completion_date=?",
        (kid_id, chore_id, logic.d2s(d)))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/assign", methods=["POST"])
@require_admin
def admin_assign():
    """Assign an as-needed chore to a kid (skip if already pending)."""
    conn = get_db()
    kid_id = request.form.get("kid_id")
    chore_id = request.form.get("chore_id")
    chore = conn.execute("SELECT 1 FROM chores WHERE id=? AND type='as_needed' AND active=1",
                         (chore_id,)).fetchone()
    pending = conn.execute(
        "SELECT 1 FROM as_needed_assignments WHERE kid_id=? AND chore_id=? "
        "AND completed_at IS NULL", (kid_id, chore_id)).fetchone()
    if chore and not pending:
        conn.execute(
            "INSERT INTO as_needed_assignments (kid_id, chore_id, assigned_at, "
            "completed_at) VALUES (?,?,?,NULL)", (kid_id, chore_id, logic.now_iso()))
        conn.commit()
    return _admin_redirect()


@app.route("/admin/assign/complete", methods=["POST"])
@require_admin
def admin_assign_complete():
    conn = get_db()
    conn.execute(
        "UPDATE as_needed_assignments SET completed_at=? WHERE id=? AND completed_at IS NULL",
        (logic.now_iso(), request.form.get("assignment_id")))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/assign", methods=["POST"])
@require_admin
def admin_chore_assign():
    """Assign a recurring chore (daily/weekly/scheduled, non-rotating) to a kid."""
    conn = get_db()
    kid_id = request.form.get("kid_id")
    chore_id = request.form.get("chore_id")
    chore = conn.execute(
        "SELECT 1 FROM chores WHERE id=? "
        "AND type IN ('daily','weekly','scheduled','alternate_daily') "
        "AND is_rotating=0 AND active=1", (chore_id,)).fetchone()
    if chore:
        conn.execute(
            "INSERT OR IGNORE INTO weekly_assignments (chore_id, kid_id, created_at) "
            "VALUES (?,?,?)", (chore_id, kid_id, logic.now_iso()))
        conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/unassign", methods=["POST"])
@require_admin
def admin_chore_unassign():
    conn = get_db()
    conn.execute("DELETE FROM weekly_assignments WHERE chore_id=? AND kid_id=?",
                 (request.form.get("chore_id"), request.form.get("kid_id")))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/set-type", methods=["POST"])
@require_admin
def admin_chore_set_type():
    """Change just the type of a chore; preserves name and existing config fields."""
    conn = get_db()
    chore_id = request.form.get("chore_id")
    ctype = request.form.get("type")
    if ctype not in ("daily", "weekly", "as_needed", "scheduled", "alternate_daily"):
        return _admin_redirect()
    row = conn.execute(
        "SELECT due_weekday, reminder_lead_days, due_label, alt_day_parity "
        "FROM chores WHERE id=? AND deleted=0", (chore_id,)).fetchone()
    if row is None:
        return _admin_redirect()
    if ctype == "scheduled":
        wd = row["due_weekday"] if row["due_weekday"] is not None else 0
        lead = row["reminder_lead_days"] if row["reminder_lead_days"] is not None else 5
        label = row["due_label"] or ""
    else:
        wd, lead, label = None, None, None
    parity = (row["alt_day_parity"] if row["alt_day_parity"] is not None else 0) \
             if ctype == "alternate_daily" else None
    conn.execute(
        "UPDATE chores SET type=?, due_weekday=?, reminder_lead_days=?, due_label=?, "
        "alt_day_parity=? WHERE id=? AND deleted=0",
        (ctype, wd, lead, label, parity, chore_id))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/set-who", methods=["POST"])
@require_admin
def admin_chore_set_who():
    """Single-select assignment: none / both / kid_<id> / rotate_<id>."""
    conn = get_db()
    chore_id = request.form.get("chore_id")
    mode = request.form.get("mode", "none")
    row = conn.execute("SELECT is_rotating FROM chores WHERE id=? AND deleted=0",
                       (chore_id,)).fetchone()
    if row is None or mode == "rotating":
        return _admin_redirect()
    kids = logic.active_kids(conn)
    valid_kid_ids = {k["id"] for k in kids}
    # Clear current state
    if row["is_rotating"]:
        conn.execute("UPDATE chores SET is_rotating=0 WHERE id=?", (chore_id,))
        conn.execute("DELETE FROM rotating_chore_assignments WHERE chore_id=?", (chore_id,))
    conn.execute("DELETE FROM weekly_assignments WHERE chore_id=?", (chore_id,))
    if mode == "both":
        for k in kids:
            conn.execute(
                "INSERT OR IGNORE INTO weekly_assignments (chore_id, kid_id, created_at) "
                "VALUES (?,?,?)", (chore_id, k["id"], logic.now_iso()))
    elif mode.startswith("kid_"):
        kid_id = _int_or_none(mode[4:])
        if kid_id in valid_kid_ids:
            conn.execute(
                "INSERT OR IGNORE INTO weekly_assignments (chore_id, kid_id, created_at) "
                "VALUES (?,?,?)", (chore_id, kid_id, logic.now_iso()))
    elif mode.startswith("rotate_"):
        start_kid_id = _int_or_none(mode[7:])
        if start_kid_id not in valid_kid_ids and kids:
            start_kid_id = kids[0]["id"]
        conn.execute("UPDATE chores SET is_rotating=1 WHERE id=?", (chore_id,))
        ws = logic.d2s(logic.week_start(effective_today()))
        conn.execute(
            "INSERT INTO rotating_chore_assignments (chore_id, kid_id, week_start_date, is_override) "
            "VALUES (?,?,?,0)", (chore_id, start_kid_id, ws))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/chore/rotate", methods=["POST"])
@require_admin
def admin_chore_rotate():
    """Toggle rotation on any chore. ON: fixed assignments drop, this week's pick
    is seeded (then it auto-swaps). OFF: rotation assignments drop (chore becomes
    unassigned until you assign it)."""
    conn = get_db()
    chore_id = request.form.get("chore_id")
    row = conn.execute("SELECT is_rotating FROM chores WHERE id=? AND deleted=0",
                       (chore_id,)).fetchone()
    if row is None:
        return _admin_redirect()
    if row["is_rotating"]:
        conn.execute("UPDATE chores SET is_rotating=0 WHERE id=?", (chore_id,))
        conn.execute("DELETE FROM rotating_chore_assignments WHERE chore_id=?", (chore_id,))
    else:
        conn.execute("UPDATE chores SET is_rotating=1 WHERE id=?", (chore_id,))
        conn.execute("DELETE FROM weekly_assignments WHERE chore_id=?", (chore_id,))
        ws = logic.d2s(logic.week_start(effective_today()))
        exists = conn.execute("SELECT 1 FROM rotating_chore_assignments "
                              "WHERE chore_id=? AND week_start_date=?",
                              (chore_id, ws)).fetchone()
        kids = logic.active_kids(conn)
        if not exists and kids:
            start_id = _int_or_none(request.form.get("start_kid_id"))
            valid_ids = {k["id"] for k in kids}
            kid_id = start_id if start_id in valid_ids else kids[0]["id"]
            conn.execute("INSERT INTO rotating_chore_assignments (chore_id, kid_id, "
                         "week_start_date, is_override) VALUES (?,?,?,0)",
                         (chore_id, kid_id, ws))
    conn.commit()
    return _admin_redirect()


@app.route("/admin/rotation/swap", methods=["POST"])
@require_admin
def admin_rotation_swap():
    conn = get_db()
    logic.swap_rotation_this_week(conn, effective_today())
    return _admin_redirect()


@app.route("/admin/rotation/swap-one", methods=["POST"])
@require_admin
def admin_rotation_swap_one():
    conn = get_db()
    chore_id = _int_or_none(request.form.get("chore_id"))
    if chore_id:
        logic.swap_rotation_for_chore(conn, chore_id, effective_today())
    return _admin_redirect()


# ---- Settings page ------------------------------------------------------- #
@app.route("/admin/settings")
@require_admin
def admin_settings():
    return render_template("settings.html", **settings_view(get_db()))


@app.route("/admin/settings/targets", methods=["POST"])
@require_admin
def admin_settings_targets():
    conn = get_db()
    for k in logic.active_kids(conn):
        try:
            r = int(request.form.get("reading_%d" % k["id"]))
            o = int(request.form.get("outdoor_%d" % k["id"]))
        except (TypeError, ValueError):
            continue
        if r > 0 and o > 0:
            conn.execute("UPDATE kids SET reading_target_minutes=?, "
                         "outdoor_target_minutes=? WHERE id=?", (r, o, k["id"]))
    conn.commit()
    return redirect("/admin/settings?saved=1")


@app.route("/admin/settings/general", methods=["POST"])
@require_admin
def admin_settings_general():
    conn = get_db()
    reminder = (request.form.get("reminder_time") or "").strip()
    if reminder == "off" or _valid_time(reminder):
        logic.set_setting(conn, "reminder_time", reminder)
    for field, key in (("program_start", "program_start_date"),
                       ("program_end", "program_end_date")):
        val = (request.form.get(field) or "").strip()
        if _valid_date(val) or val == "":
            logic.set_setting(conn, key, val)
    logic.set_setting(conn, "scoreboard_reward_text",
                      (request.form.get("reward") or "").strip())
    logic.set_setting(conn, "bonus_dollar_amount",
                      (request.form.get("bonus_dollar_amount") or "").strip())
    conn.commit()
    return redirect("/admin/settings?saved=1")


@app.route("/admin/settings/appconfig", methods=["POST"])
@require_admin
def admin_settings_appconfig():
    conn = get_db()
    for key, form_key in (("app_name", "app_name"), ("program_label", "program_label"),
                          ("reading_label", "reading_label"),
                          ("outdoor_label", "outdoor_label"),
                          ("timezone", "timezone")):
        val = (request.form.get(form_key) or "").strip()
        if val:
            logic.set_setting(conn, key, val)
    logic.set_setting(conn, "reading_enabled",
                      "1" if request.form.get("reading_enabled") else "0")
    logic.set_setting(conn, "outdoor_enabled",
                      "1" if request.form.get("outdoor_enabled") else "0")
    conn.commit()
    return redirect("/admin/settings?saved=1")


@app.route("/admin/settings/notify/test", methods=["POST"])
@require_admin
def admin_settings_notify_test():
    conn = get_db()
    app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
    ok = notify.send(conn, "%s — Test" % app_name,
                     "Test notification from %s. If you see this, notifications are working!" % app_name)
    return redirect("/admin/settings?notify_test=%s" % ("ok" if ok else "fail"))


@app.route("/admin/settings/notify", methods=["POST"])
@require_admin
def admin_settings_notify():
    conn = get_db()
    f = lambda k: (request.form.get(k) or "").strip()
    logic.set_setting(conn, "notify_service", f("notify_service") or "none")
    for key in ("notify_pushover_app_token", "notify_pushover_user_key",
                "notify_telegram_token", "notify_telegram_chatid",
                "notify_discord_webhook", "notify_slack_webhook",
                "notify_ntfy_topic", "notify_ntfy_host",
                "notify_gotify_url", "notify_gotify_token",
                "notify_urls"):
        logic.set_setting(conn, key, f(key))
    logic.set_setting(conn, "passphrase_required",
                      "1" if request.form.get("passphrase_required") else "0")
    conn.commit()
    return redirect("/admin/settings?saved=1")


@app.route("/admin/settings/password", methods=["POST"])
@require_admin
def admin_settings_password():
    conn = get_db()
    stored = logic.get_setting(conn, "admin_password_hash", "")
    current = request.form.get("current", "")
    new = request.form.get("new", "")
    confirm = request.form.get("confirm", "")
    if db.verify_password(stored, current) and new and new == confirm:
        logic.set_setting(conn, "admin_password_hash", db.hash_password(new))
        conn.commit()
        return redirect("/admin/settings?saved=1")
    return redirect("/admin/settings?pwerror=1")


def _save_paused_chores(conn, sp_id):
    """Replace the paused-chore list for a special period from form checkboxes."""
    conn.execute("DELETE FROM special_period_paused_chores WHERE special_period_id=?", (sp_id,))
    for cid in request.form.getlist("paused_chores"):
        cid_int = _int_or_none(cid)
        if cid_int:
            conn.execute(
                "INSERT OR IGNORE INTO special_period_paused_chores "
                "(special_period_id, chore_id) VALUES (?,?)", (sp_id, cid_int))


@app.route("/admin/special/add", methods=["POST"])
@require_admin
def admin_special_add():
    conn = get_db()
    label = (request.form.get("label") or "").strip()
    ptype = request.form.get("type")
    start = (request.form.get("start_date") or "").strip()
    end = (request.form.get("end_date") or "").strip()
    omd = request.form.get("outdoor_minutes_per_day")
    if label and ptype in ("paused", "outdoor_credit") and _valid_date(start) and _valid_date(end):
        try:
            omd = int(omd) if ptype == "outdoor_credit" else None
        except (TypeError, ValueError):
            omd = None
        pause_reading = 1 if request.form.get("pause_reading") else 0
        pause_outdoor = 1 if request.form.get("pause_outdoor") else 0
        cur = conn.execute(
            "INSERT INTO special_periods (label, type, start_date, end_date, "
            "outdoor_minutes_per_day, pause_reading, pause_outdoor) VALUES (?,?,?,?,?,?,?)",
            (label, ptype, start, end, omd, pause_reading, pause_outdoor))
        _save_paused_chores(conn, cur.lastrowid)
        conn.commit()
    return redirect("/admin/settings?saved=1")


@app.route("/admin/special/edit", methods=["POST"])
@require_admin
def admin_special_edit():
    conn = get_db()
    sp_id = request.form.get("id")
    label = (request.form.get("label") or "").strip()
    ptype = request.form.get("type")
    start = (request.form.get("start_date") or "").strip()
    end = (request.form.get("end_date") or "").strip()
    omd = request.form.get("outdoor_minutes_per_day")
    if label and ptype in ("paused", "outdoor_credit") and _valid_date(start) and _valid_date(end):
        try:
            omd = int(omd) if ptype == "outdoor_credit" else None
        except (TypeError, ValueError):
            omd = None
        pause_reading = 1 if request.form.get("pause_reading") else 0
        pause_outdoor = 1 if request.form.get("pause_outdoor") else 0
        conn.execute(
            "UPDATE special_periods SET label=?, type=?, start_date=?, end_date=?, "
            "outdoor_minutes_per_day=?, pause_reading=?, pause_outdoor=? WHERE id=?",
            (label, ptype, start, end, omd, pause_reading, pause_outdoor, sp_id))
        _save_paused_chores(conn, sp_id)
        conn.commit()
    return redirect("/admin/settings?saved=1")


@app.route("/admin/special/delete", methods=["POST"])
@require_admin
def admin_special_delete():
    conn = get_db()
    sp_id = request.form.get("id")
    conn.execute("DELETE FROM special_period_paused_chores WHERE special_period_id=?", (sp_id,))
    conn.execute("DELETE FROM special_periods WHERE id=?", (sp_id,))
    conn.commit()
    return redirect("/admin/settings?saved=1")


# ---- History page -------------------------------------------------------- #
@app.route("/admin/history")
@require_admin
def admin_history():
    conn = get_db()
    return render_template("history.html", **history_view(conn, effective_today()))


# ---- Log edit page ------------------------------------------------------- #
@app.route("/admin/logs")
@require_admin
def admin_logs():
    conn = get_db()
    return render_template("logs.html", **logs_view(conn, effective_today()))


def _logs_redirect():
    today = request.form.get("today")
    return redirect("/admin/logs?today=" + today if today else "/admin/logs")


@app.route("/admin/log/edit", methods=["POST"])
@require_admin
def admin_log_edit():
    conn = get_db()
    kind = request.form.get("kind")
    if kind not in ("reading", "outdoor"):
        abort(400)
    table = "reading_logs" if kind == "reading" else "outdoor_logs"
    try:
        minutes = int(request.form.get("minutes"))
    except (TypeError, ValueError):
        minutes = 0
    if minutes > 0:
        conn.execute("UPDATE %s SET minutes=? WHERE id=? AND source='manual'" % table,
                     (minutes, request.form.get("log_id")))
        conn.commit()
    return _logs_redirect()


@app.route("/admin/log/delete", methods=["POST"])
@require_admin
def admin_log_delete():
    conn = get_db()
    kind = request.form.get("kind")
    if kind not in ("reading", "outdoor"):
        abort(400)
    table = "reading_logs" if kind == "reading" else "outdoor_logs"
    conn.execute("DELETE FROM %s WHERE id=? AND source='manual'" % table,
                 (request.form.get("log_id"),))
    conn.commit()
    return _logs_redirect()


@app.route("/status")
def status_page():
    conn = get_db()
    d = effective_today()
    return render_template("status.html", **status_view(conn, d))


@app.route("/<slug>")
def kid_page(slug):
    conn = get_db()
    kid = logic.kid_by_slug(conn, slug)
    if kid is None:
        abort(404)
    passphrase_required = logic.get_setting(conn, "passphrase_required", "0") == "1"
    if passphrase_required and kid["passphrase_hash"]:
        if not session.get("kid_auth_" + slug):
            return redirect("/login/" + slug)
    d = effective_today()
    return render_template("kid.html", **kid_view(conn, kid, d))


def _require_kid(conn, payload):
    kid = logic.kid_by_slug(conn, (payload or {}).get("slug", ""))
    if kid is None:
        abort(404)
    return kid


def _maybe_reinstate_bonus(conn, kid, d):
    """After any kid action on Monday, see if the make-up bonus is now earned."""
    row = logic.check_makeup_reinstatement(conn, kid["id"], d)
    if row is not None:
        app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
        notify.send(conn, app_name,
                    "%s earned their bonus back!" % kid["name"])
        return True
    return False


@app.route("/api/chore/complete", methods=["POST"])
def api_chore_complete():
    conn = get_db()
    data = request.get_json(silent=True) or {}
    kid = _require_kid(conn, data)
    d = effective_today()
    kind = data.get("kind", "daily")

    if kind == "as_needed":
        assignment_id = data.get("id")
        row = conn.execute(
            "SELECT a.*, c.name FROM as_needed_assignments a "
            "JOIN chores c ON c.id=a.chore_id "
            "WHERE a.id=? AND a.kid_id=?", (assignment_id, kid["id"])).fetchone()
        if row is None:
            abort(404)
        if row["completed_at"] is None:
            conn.execute("UPDATE as_needed_assignments SET completed_at=? WHERE id=?",
                         (logic.now_iso(), row["id"]))
            conn.commit()
            app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
            notify.send(conn, app_name,
                        "%s completed: %s" % (kid["name"], row["name"]))
        return jsonify({"ok": True})

    # Daily / alternate_daily / weekly / scheduled chore — all record a
    # chore_completion row for today. Only daily/alternate_daily fire the
    # checklist notification.
    ctype = kind if kind in ("weekly", "scheduled", "alternate_daily") else "daily"
    chore_id = data.get("id")
    chore = conn.execute("SELECT * FROM chores WHERE id=? AND type=? AND active=1",
                         (chore_id, ctype)).fetchone()
    if chore is None:
        abort(404)
    conn.execute(
        "INSERT OR IGNORE INTO chore_completions (kid_id, chore_id, "
        "completion_date, completed_at, parent_verified) VALUES (?,?,?,?,0)",
        (kid["id"], chore_id, logic.d2s(d), logic.now_iso()))
    conn.commit()

    done = False
    if ctype in ("daily", "alternate_daily"):
        done, _ = logic.checklist_status(conn, kid["id"], d)
        if done:
            app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
            notify.send_once(conn, kid["id"], "daily_complete", "%s ✓" % app_name,
                             "%s finished their daily checklist!" % kid["name"], d)
    reinstated = _maybe_reinstate_bonus(conn, kid, d)
    return jsonify({"ok": True, "checklist_done": done, "bonus_reinstated": reinstated})


@app.route("/api/log", methods=["POST"])
def api_log_add():
    conn = get_db()
    data = request.get_json(silent=True) or {}
    kid = _require_kid(conn, data)
    d = effective_today()
    kind = data.get("kind")
    if kind not in ("reading", "outdoor"):
        abort(400)
    try:
        minutes = int(data.get("minutes"))
    except (TypeError, ValueError):
        abort(400)
    if minutes <= 0 or minutes > 600:
        abort(400)

    table = "reading_logs" if kind == "reading" else "outdoor_logs"
    conn.execute(
        "INSERT INTO %s (kid_id, log_date, minutes, source, logged_at) "
        "VALUES (?,?,?, 'manual', ?)" % table,
        (kid["id"], logic.d2s(d), minutes, logic.now_iso()))
    conn.commit()

    reinstated = _maybe_reinstate_bonus(conn, kid, d)
    section = log_section(conn, kid, kind, d)
    section["bonus_reinstated"] = reinstated
    return jsonify(section)


@app.route("/api/log", methods=["DELETE"])
def api_log_remove():
    conn = get_db()
    data = request.get_json(silent=True) or {}
    kid = _require_kid(conn, data)
    d = effective_today()
    kind = data.get("kind")
    if kind not in ("reading", "outdoor"):
        abort(400)
    table = "reading_logs" if kind == "reading" else "outdoor_logs"
    # Same-day, manual entries only — kids can't delete camp auto-credit or history.
    conn.execute(
        "DELETE FROM %s WHERE id=? AND kid_id=? AND log_date=? AND source='manual'"
        % table, (data.get("id"), kid["id"], logic.d2s(d)))
    conn.commit()
    return jsonify(log_section(conn, kid, kind, d))


# ---- Kid management ---------------------------------------------------- #
_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


@app.route("/admin/kid/add", methods=["POST"])
@require_admin
def admin_kid_add():
    conn = get_db()
    name = (request.form.get("name") or "").strip()
    slug = (request.form.get("slug") or "").strip().lower()
    if name and slug and _SLUG_RE.match(slug):
        try:
            conn.execute(
                "INSERT INTO kids (name, url_slug, active, created_at) VALUES (?,?,1,?)",
                (name, slug, logic.now_iso()))
            conn.commit()
        except Exception:
            pass  # duplicate slug
    return redirect("/admin/settings?saved=1")


@app.route("/admin/kid/rename", methods=["POST"])
@require_admin
def admin_kid_rename():
    conn = get_db()
    kid_id = request.form.get("kid_id")
    name = (request.form.get("name") or "").strip()
    slug = (request.form.get("slug") or "").strip().lower()
    if name and slug and _SLUG_RE.match(slug):
        try:
            conn.execute("UPDATE kids SET name=?, url_slug=? WHERE id=?",
                         (name, slug, kid_id))
            conn.commit()
        except Exception:
            pass  # duplicate slug
    return redirect("/admin/settings?saved=1")


@app.route("/admin/kid/deactivate", methods=["POST"])
@require_admin
def admin_kid_deactivate():
    conn = get_db()
    kid_id = request.form.get("kid_id")
    conn.execute("UPDATE kids SET active=0 WHERE id=?", (kid_id,))
    conn.commit()
    return redirect("/admin/settings?saved=1")


@app.route("/admin/kid/activate", methods=["POST"])
@require_admin
def admin_kid_activate():
    conn = get_db()
    kid_id = request.form.get("kid_id")
    conn.execute("UPDATE kids SET active=1 WHERE id=?", (kid_id,))
    conn.commit()
    return redirect("/admin/settings?saved=1")


@app.route("/admin/kid/passphrase", methods=["POST"])
@require_admin
def admin_kid_passphrase():
    conn = get_db()
    kid_id = request.form.get("kid_id")
    passphrase = (request.form.get("passphrase") or "").strip()
    if passphrase:
        conn.execute("UPDATE kids SET passphrase_hash=? WHERE id=?",
                     (db.hash_password(passphrase), kid_id))
    else:
        conn.execute("UPDATE kids SET passphrase_hash=NULL WHERE id=?", (kid_id,))
    conn.commit()
    return redirect("/admin/settings?saved=1")


# ---- Per-kid passphrase login ------------------------------------------ #
@app.route("/login/<slug>", methods=["GET", "POST"])
def kid_login(slug):
    conn = get_db()
    kid = logic.kid_by_slug(conn, slug)
    if kid is None:
        abort(404)
    if not kid["passphrase_hash"]:
        return redirect("/" + slug)
    if request.method == "POST":
        entered = (request.form.get("passphrase") or "").strip()
        if db.verify_password(kid["passphrase_hash"], entered):
            session["kid_auth_" + slug] = True
            return redirect("/" + slug)
        return render_template("kid_login.html", kid=kid, error="Incorrect passphrase.")
    return render_template("kid_login.html", kid=kid, error=None)


# ---- Setup wizard -------------------------------------------------------- #
@app.route("/setup", methods=["GET", "POST"])
def setup_wizard():
    conn = get_db()
    if not is_first_run(conn):
        return redirect("/admin")
    if request.method == "POST":
        app_name = (request.form.get("app_name") or "ChoreBoard").strip()
        tz = (request.form.get("timezone") or "America/New_York").strip()
        password = (request.form.get("password") or "").strip()
        logic.set_setting(conn, "app_name", app_name)
        logic.set_setting(conn, "program_label",
                          (request.form.get("program_label") or "Activity Tracker").strip())
        logic.set_setting(conn, "timezone", tz)
        if password:
            logic.set_setting(conn, "admin_password_hash", db.hash_password(password))
        # Add kids from form (up to 4)
        for i in range(1, 5):
            name = (request.form.get("kid%d_name" % i) or "").strip()
            slug = (request.form.get("kid%d_slug" % i) or "").strip().lower()
            if name and slug and _SLUG_RE.match(slug):
                try:
                    conn.execute(
                        "INSERT INTO kids (name, url_slug, active, created_at) VALUES (?,?,1,?)",
                        (name, slug, logic.now_iso()))
                except Exception:
                    pass  # duplicate slug
        logic.set_setting(conn, "setup_complete", "1")
        conn.commit()
        session["admin"] = True
        return redirect("/admin?setup_done=1")
    common_tz = [
        "America/New_York", "America/Chicago", "America/Denver",
        "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
        "Europe/London", "Europe/Paris", "Europe/Berlin", "Asia/Tokyo",
        "Asia/Shanghai", "Australia/Sydney",
    ]
    return render_template("setup.html", common_tz=common_tz)


# ---- Data export / import ------------------------------------------------ #
@app.route("/admin/export")
@require_admin
def admin_export():
    import json as _json
    conn = get_db()
    tables = ["kids", "chores", "chore_completions", "as_needed_assignments",
              "weekly_assignments", "rotating_chore_assignments", "reading_logs",
              "outdoor_logs", "settings", "notifications_sent", "weekly_results",
              "special_periods", "special_period_paused_chores", "makeup_owed"]
    data = {}
    for t in tables:
        try:
            rows = conn.execute("SELECT * FROM %s" % t).fetchall()
            data[t] = [dict(r) for r in rows]
        except Exception:
            data[t] = []
    from flask import Response
    payload = _json.dumps(data, indent=2, default=str)
    return Response(payload, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=choreboard_export.json"})


@app.route("/admin/import", methods=["POST"])
@require_admin
def admin_import():
    import json as _json
    f = request.files.get("import_file")
    if not f or not f.filename.endswith(".json"):
        return redirect("/admin/settings?import_error=bad_file")
    try:
        data = _json.loads(f.read().decode("utf-8"))
    except Exception:
        return redirect("/admin/settings?import_error=bad_file")
    if not isinstance(data, dict):
        return redirect("/admin/settings?import_error=bad_file")

    # Import order matters for FK constraints; disable them during the replace.
    tables = ["kids", "chores", "chore_completions", "as_needed_assignments",
              "weekly_assignments", "rotating_chore_assignments", "reading_logs",
              "outdoor_logs", "settings", "notifications_sent", "weekly_results",
              "special_periods", "special_period_paused_chores", "makeup_owed"]
    conn = get_db()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for t in tables:
            rows = data.get(t)
            if rows is None:
                continue
            conn.execute("DELETE FROM %s" % t)
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ",".join(["?"] * len(cols))
            conn.executemany(
                "INSERT OR IGNORE INTO %s (%s) VALUES (%s)" % (t, ",".join(cols), placeholders),
                [[r.get(c) for c in cols] for r in rows])
        conn.commit()
    except Exception as exc:
        conn.rollback()
        app.logger.error("Import failed: %s", exc)
        return redirect("/admin/settings?import_error=failed")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return redirect("/admin/settings?import_done=1")


# ---- Server log viewer ----------------------------------------------------- #
@app.route("/admin/applog")
@require_admin
def admin_applog():
    conn = get_db()
    app_name = logic.get_setting(conn, "app_name", "ChoreBoard")
    entries = list(_LOG_BUFFER)   # oldest → newest; reverse for display
    return render_template("applog.html", entries=entries, app_name=app_name,
                           nav_kids=logic.active_kids(conn))


# Populate kid paths for the no-store filter now that the DB is seeded.
with db.connect() as _c:
    _KID_PATHS = {"/" + r["url_slug"] for r in logic.active_kids(_c)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7823"))
    # Start the background scheduler here (single process) so reminders/summaries
    # fire exactly once. use_reloader=False keeps it to one process; threaded=True
    # handles the two-iPads-at-once case.
    scheduler.start(os.environ)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

