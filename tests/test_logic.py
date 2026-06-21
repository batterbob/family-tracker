"""Unit tests for the date/weekly/proration/camp/make-up math.

Run from the project root:  python -m unittest discover -s tests
"""
import os
import sys
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db          # noqa: E402
import logic       # noqa: E402
import scheduler   # noqa: E402

NY = ZoneInfo("America/New_York")

ENV = {"TZ": "America/New_York", "ADMIN_PASSWORD": "x"}


def fresh_db():
    conn = db.connect(":memory:")
    db.init_db(conn, ENV)
    db.seed_test_data(conn, ENV)
    return conn


def kid_id(conn, slug):
    return logic.kid_by_slug(conn, slug)["id"]


def complete_checklist(conn, kid, d):
    for c in logic.active_daily_chores(conn):
        conn.execute(
            "INSERT OR IGNORE INTO chore_completions (kid_id, chore_id, "
            "completion_date, completed_at, parent_verified) VALUES (?,?,?,?,0)",
            (kid, c["id"], logic.d2s(d), logic.now_iso(ENV)))
    conn.commit()


def add_log(conn, key, kid, dstr, amount):
    """Insert a manual activity log for the activity with the given key."""
    aid = logic.activity_by_key(conn, key)["id"]
    conn.execute(
        "INSERT INTO activity_logs (activity_id, kid_id, log_date, amount, source, logged_at) "
        "VALUES (?,?,?,?, 'manual', ?)", (aid, kid, dstr, amount, logic.now_iso(ENV)))
    conn.commit()


def count_credit_logs(conn, key, kid):
    """Number of auto-credited logs for the activity with the given key."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM activity_logs al JOIN activities a ON a.id = al.activity_id "
        "WHERE a.key=? AND al.kid_id=? AND al.source='credit_auto'", (key, kid)).fetchone()["c"]


class DateMath(unittest.TestCase):
    def test_week_start_is_monday(self):
        # 2026-06-24 is a Wednesday; its Monday is 2026-06-22.
        self.assertEqual(logic.week_start(date(2026, 6, 24)), date(2026, 6, 22))
        self.assertEqual(logic.week_start(date(2026, 6, 22)), date(2026, 6, 22))
        self.assertEqual(logic.week_start(date(2026, 6, 28)), date(2026, 6, 22))

    def test_str_roundtrip(self):
        self.assertEqual(logic.s2d(logic.d2s(date(2026, 7, 3))), date(2026, 7, 3))


class Proration(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.alex = logic.kid_by_slug(self.conn, "alex")

    def test_shoulder_week_leaving(self):
        # Week Jun29-Jul5: cruise starts Jul3 -> Jul3,4,5 paused, 4 active days.
        t = logic.prorated_targets(self.conn, self.alex, date(2026, 6, 29))
        self.assertEqual(t["active_days"], 4)
        self.assertEqual(t["reading"], 100)   # round(175*4/7)
        self.assertEqual(t["outdoor"], 171)   # round(300*4/7)

    def test_fully_paused_week(self):
        # Week Jul6-12 sits entirely inside the cruise -> 0 active days.
        t = logic.prorated_targets(self.conn, self.alex, date(2026, 7, 6))
        self.assertEqual(t["active_days"], 0)
        self.assertEqual(t["reading"], 0)
        self.assertEqual(t["outdoor"], 0)

    def test_camp_week_is_full_target(self):
        # Week Jul20-26 is all in-program, none paused -> full target.
        t = logic.prorated_targets(self.conn, self.alex, date(2026, 7, 20))
        self.assertEqual(t["active_days"], 7)
        self.assertEqual(t["outdoor"], 300)


class CampCredit(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def test_idempotent_and_fills_week(self):
        a = kid_id(self.conn, "alex")
        # Run twice; must not double-count.
        logic.ensure_camp_credit(self.conn, date(2026, 8, 1))
        logic.ensure_camp_credit(self.conn, date(2026, 8, 1))
        n = count_credit_logs(self.conn, "outdoor", a)
        # Camp Jul20-31: weekdays = Jul20-24 and Jul27-31 = 10 days.
        self.assertEqual(n, 10)
        # Camp week Jul20-26: 5 weekdays x 60 = 300 = exactly the outdoor goal.
        self.assertEqual(logic.weekly_outdoor(self.conn, a, date(2026, 7, 20)), 300)

    def test_credit_not_granted_before_today(self):
        a = kid_id(self.conn, "alex")
        logic.ensure_camp_credit(self.conn, date(2026, 7, 22))  # mid-camp
        n = count_credit_logs(self.conn, "outdoor", a)
        self.assertEqual(n, 3)  # only Jul20, 21, 22 credited so far


class Pace(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def test_sunday_divide_by_zero_guard(self):
        ws = date(2026, 6, 22)
        state, needed = logic.pace(self.conn, 175, 100, ws, date(2026, 6, 28))  # Sun
        self.assertEqual(state, "no_days_left")
        self.assertIsNone(needed)

    def test_behind_midweek(self):
        ws = date(2026, 6, 22)
        state, needed = logic.pace(self.conn, 175, 0, ws, date(2026, 6, 23))  # Tue
        self.assertEqual(state, "behind")
        self.assertEqual(needed, 35)  # ceil(175 / 5 remaining active days)

    def test_met_hides_pace(self):
        ws = date(2026, 6, 22)
        state, needed = logic.pace(self.conn, 175, 175, ws, date(2026, 6, 24))
        self.assertEqual(state, "met")


class Banner(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.alex = logic.kid_by_slug(self.conn, "alex")

    def test_out_of_program(self):
        b = logic.banner_state(self.conn, self.alex, date(2026, 6, 19))
        self.assertEqual(b["state"], "out_of_program")

    def test_on_break(self):
        b = logic.banner_state(self.conn, self.alex, date(2026, 7, 8))
        self.assertEqual(b["state"], "on_break")

    def test_monday_empty_is_green(self):
        # Monday with nothing logged -> on track (full week ahead).
        b = logic.banner_state(self.conn, self.alex, date(2026, 6, 22))
        self.assertEqual(b["state"], "on_track")


class MakeupMonday(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.a = kid_id(self.conn, "alex")

    def test_miss_creates_deficit_then_reinstates(self):
        # Week 1 (Jun22-28): log below target so it finalizes as a miss.
        add_log(self.conn, "reading", self.a, "2026-06-24", 100)
        add_log(self.conn, "outdoor", self.a, "2026-06-24", 200)

        logic.finalize_past_weeks(self.conn, date(2026, 6, 30))  # week is now past
        owed = self.conn.execute(
            "SELECT * FROM makeup_owed WHERE kid_id=? AND for_week_start='2026-06-29'",
            (self.a,)).fetchone()
        self.assertIsNotNone(owed)
        self.assertEqual(owed["reading_deficit"], 75)   # 175 - 100
        self.assertEqual(owed["outdoor_deficit"], 100)  # 300 - 200

        monday = date(2026, 6, 29)
        # Not yet satisfied: missing the deficit minutes and checklist.
        self.assertIsNone(logic.check_makeup_reinstatement(self.conn, self.a, monday))

        # Make it up on Monday: deficit minutes + complete checklist.
        add_log(self.conn, "reading", self.a, "2026-06-29", 75)
        add_log(self.conn, "outdoor", self.a, "2026-06-29", 100)
        complete_checklist(self.conn, self.a, monday)

        row = logic.check_makeup_reinstatement(self.conn, self.a, monday)
        self.assertIsNotNone(row)
        still_open = logic.open_makeup(self.conn, self.a, monday)
        self.assertIsNone(still_open)  # satisfied_at now set


class Scoreboard(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.a = kid_id(self.conn, "alex")

    def test_paused_week_does_not_break_streak(self):
        rows = [
            ("2026-06-22", 1, 0),  # earned
            ("2026-06-29", None, 1),  # paused (excluded)
            ("2026-07-06", 1, 0),  # earned
        ]
        for ws, bonus, paused in rows:
            self.conn.execute(
                "INSERT INTO weekly_results (kid_id, week_start_date, reading_minutes, "
                "outdoor_minutes, reading_target, outdoor_target, active_days, "
                "is_paused_week, bonus_earned, computed_at) "
                "VALUES (?,?,0,0,0,0,7,?,?,?)",
                (self.a, ws, paused, bonus, logic.now_iso(ENV)))
        self.conn.commit()
        stars, streak = logic.scoreboard(self.conn, self.a)
        self.assertEqual(stars, 2)
        self.assertEqual(streak, 2)  # paused week skipped, not broken


class ChoresOnlyBonus(unittest.TestCase):
    """With both activities disabled, the bonus is earned by completing the
    daily checklist on enough days of the week."""
    def setUp(self):
        self.conn = fresh_db()
        self.conn.execute("UPDATE activities SET enabled=0")  # chores-only mode
        self.conn.commit()
        self.a = kid_id(self.conn, "alex")
        # Jun22-28 is a fully active (7-day) week in the seed program window.
        self.week = [date(2026, 6, d) for d in range(22, 29)]

    def _bonus(self):
        logic.finalize_past_weeks(self.conn, date(2026, 6, 30))
        return self.conn.execute(
            "SELECT bonus_earned FROM weekly_results "
            "WHERE kid_id=? AND week_start_date='2026-06-22'",
            (self.a,)).fetchone()["bonus_earned"]

    def test_all_days_earns_bonus(self):
        for d in self.week:
            complete_checklist(self.conn, self.a, d)
        self.assertEqual(self._bonus(), 1)

    def test_missing_a_day_misses_bonus_by_default(self):
        for d in self.week[:-1]:  # 6 of 7 days
            complete_checklist(self.conn, self.a, d)
        self.assertEqual(self._bonus(), 0)

    def test_threshold_relaxes_requirement(self):
        logic.set_setting(self.conn, "checklist_min_days", "5")
        for d in self.week[:5]:  # exactly 5 days
            complete_checklist(self.conn, self.a, d)
        self.assertEqual(self._bonus(), 1)


class GenericActivities(unittest.TestCase):
    """A third, count-unit activity participates in the weekly bonus."""
    def setUp(self):
        self.conn = fresh_db()
        self.a = kid_id(self.conn, "alex")
        self.ws = date(2026, 6, 22)
        self.conn.execute(
            "INSERT INTO activities (key, label, unit, enabled, sort_order, "
            "supports_credit, default_target) VALUES ('water','Water','count',1,2,0,5)")
        self.conn.commit()

    def _bonus(self):
        logic.finalize_past_weeks(self.conn, date(2026, 6, 30))
        return self.conn.execute(
            "SELECT bonus_earned FROM weekly_results "
            "WHERE kid_id=? AND week_start_date='2026-06-22'", (self.a,)).fetchone()["bonus_earned"]

    def test_third_activity_gates_bonus(self):
        add_log(self.conn, "reading", self.a, "2026-06-24", 175)
        add_log(self.conn, "outdoor", self.a, "2026-06-24", 300)
        self.assertEqual(self._bonus(), 0)  # water target unmet

    def test_all_three_met_earns_bonus(self):
        add_log(self.conn, "reading", self.a, "2026-06-24", 175)
        add_log(self.conn, "outdoor", self.a, "2026-06-24", 300)
        add_log(self.conn, "water", self.a, "2026-06-24", 5)
        self.assertEqual(self._bonus(), 1)

    def test_default_target_used_without_override(self):
        water = logic.activity_by_key(self.conn, "water")
        alex = logic.kid_by_slug(self.conn, "alex")
        t = logic.prorated_targets(self.conn, alex, self.ws)
        self.assertEqual(t["by_activity"][water["id"]], 5)


class ChorePoints(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.a = kid_id(self.conn, "alex")
        self.ws = date(2026, 6, 22)

    def test_points_sum_for_week(self):
        self.conn.execute("UPDATE chores SET points=5 WHERE type='daily'")
        self.conn.commit()
        complete_checklist(self.conn, self.a, date(2026, 6, 24))
        n_daily = self.conn.execute(
            "SELECT COUNT(*) AS n FROM chores WHERE type='daily'").fetchone()["n"]
        self.assertEqual(logic.weekly_points(self.conn, self.a, self.ws), 5 * n_daily)

    def test_points_outside_week_excluded(self):
        self.conn.execute("UPDATE chores SET points=5 WHERE type='daily'")
        self.conn.commit()
        complete_checklist(self.conn, self.a, date(2026, 6, 15))  # the prior week
        self.assertEqual(logic.weekly_points(self.conn, self.a, self.ws), 0)

    def test_as_needed_points_counted(self):
        ch = self.conn.execute(
            "SELECT id FROM chores WHERE type='as_needed' LIMIT 1").fetchone()["id"]
        self.conn.execute("UPDATE chores SET points=3 WHERE id=?", (ch,))
        self.conn.execute(
            "INSERT INTO as_needed_assignments (kid_id, chore_id, assigned_at, completed_at) "
            "VALUES (?,?,?,?)", (self.a, ch, logic.now_iso(ENV), "2026-06-24T10:00:00"))
        self.conn.commit()
        self.assertEqual(logic.weekly_points(self.conn, self.a, self.ws), 3)


class WeeklyChores(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.a = kid_id(self.conn, "alex")
        self.chore = self.conn.execute(
            "SELECT id FROM chores WHERE name='Clean your room'").fetchone()["id"]

    def test_seeded_for_both_kids(self):
        rows = logic.weekly_chores_for_kid(self.conn, self.a, date(2026, 6, 22))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Clean your room")
        self.assertFalse(rows[0]["done"])

    def test_done_persists_through_week_then_resets(self):
        # Complete it on Wednesday Jun 24.
        self.conn.execute(
            "INSERT INTO chore_completions (kid_id, chore_id, completion_date, "
            "completed_at, parent_verified) VALUES (?,?,?,?,0)",
            (self.a, self.chore, "2026-06-24", logic.now_iso(ENV)))
        self.conn.commit()
        # Still shows done later the same week (Sunday Jun 28).
        self.assertTrue(logic.weekly_done(self.conn, self.a, self.chore, date(2026, 6, 28)))
        # Resets the next week (Monday Jun 29) -> open again.
        self.assertFalse(logic.weekly_done(self.conn, self.a, self.chore, date(2026, 6, 29)))

    def test_weekly_does_not_affect_daily_checklist(self):
        # Completing the weekly chore must not mark the daily checklist done.
        self.conn.execute(
            "INSERT INTO chore_completions (kid_id, chore_id, completion_date, "
            "completed_at, parent_verified) VALUES (?,?,?,?,0)",
            (self.a, self.chore, "2026-06-24", logic.now_iso(ENV)))
        self.conn.commit()
        done, _ = logic.checklist_status(self.conn, self.a, date(2026, 6, 24))
        self.assertFalse(done)


class AdminAuth(unittest.TestCase):
    def test_password_verify(self):
        conn = fresh_db()
        stored = logic.get_setting(conn, "admin_password_hash")
        self.assertTrue(db.verify_password(stored, "x"))     # ENV ADMIN_PASSWORD
        self.assertFalse(db.verify_password(stored, "wrong"))
        self.assertFalse(db.verify_password("", "x"))
        self.assertFalse(db.verify_password("no-dollar", "x"))


class ChecklistDays(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.a = kid_id(self.conn, "alex")

    def test_days_completed_vs_elapsed(self):
        complete_checklist(self.conn, self.a, date(2026, 6, 22))  # Mon
        complete_checklist(self.conn, self.a, date(2026, 6, 23))  # Tue
        # Wed: 2 of 3 active days so far (Wed not finished).
        comp, elapsed = logic.checklist_days_this_week(self.conn, self.a, date(2026, 6, 24))
        self.assertEqual((comp, elapsed), (2, 3))

    def test_full_week_count_for_history(self):
        complete_checklist(self.conn, self.a, date(2026, 6, 22))
        complete_checklist(self.conn, self.a, date(2026, 6, 23))
        # Whole week 06-22: 2 done out of 7 active days.
        comp, active = logic.checklist_days_in_week(self.conn, self.a, date(2026, 6, 22))
        self.assertEqual((comp, active), (2, 7))


class Rotation(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.alex = kid_id(self.conn, "alex")
        self.jordan = kid_id(self.conn, "jordan")
        self.trash = self.conn.execute(
            "SELECT id FROM chores WHERE name='Take indoor trash to outdoor bins'"
        ).fetchone()["id"]

    def _holder(self, chore_id, ws):
        row = self.conn.execute(
            "SELECT kid_id FROM rotating_chore_assignments "
            "WHERE chore_id=? AND week_start_date=?", (chore_id, ws)).fetchone()
        return row["kid_id"] if row else None

    def test_seed_week1(self):
        self.assertEqual(self._holder(self.trash, "2026-06-22"), self.alex)

    def test_auto_swap_next_week(self):
        logic.ensure_rotation_for_week(self.conn, date(2026, 6, 29))
        self.assertEqual(self._holder(self.trash, "2026-06-29"), self.jordan)

    def test_backfill_alternates(self):
        # Jump several weeks; intermediate weeks must be backfilled, alternating.
        logic.ensure_rotation_for_week(self.conn, date(2026, 7, 13))
        self.assertEqual(self._holder(self.trash, "2026-07-06"), self.alex)
        self.assertEqual(self._holder(self.trash, "2026-07-13"), self.jordan)

    def test_manual_swap(self):
        logic.swap_rotation_this_week(self.conn, date(2026, 6, 24))  # week of 06-22
        self.assertEqual(self._holder(self.trash, "2026-06-22"), self.jordan)


def complete_chore(conn, kid, chore_id, dstr):
    conn.execute(
        "INSERT OR IGNORE INTO chore_completions (kid_id, chore_id, completion_date, "
        "completed_at, parent_verified) VALUES (?,?,?,?,0)",
        (kid, chore_id, dstr, logic.now_iso(ENV)))
    conn.commit()


class ScheduledChores(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.d = kid_id(self.conn, "jordan")   # bins rotate to Jordan in week 1
        # Pin creation well before the test dates so overdue logic is deterministic.
        self.conn.execute("UPDATE chores SET created_at='2026-06-01T00:00:00' "
                          "WHERE name='Put bins out at curb'")
        self.conn.commit()
        self.bins = self.conn.execute(
            "SELECT * FROM chores WHERE name='Put bins out at curb'").fetchone()

    def test_seeded_as_scheduled(self):
        self.assertEqual(self.bins["type"], "scheduled")
        self.assertEqual(self.bins["due_weekday"], 0)        # Monday
        self.assertEqual(self.bins["reminder_lead_days"], 5)
        self.assertEqual(self.bins["is_rotating"], 1)

    def test_countdown(self):
        complete_chore(self.conn, self.d, self.bins["id"], "2026-06-22")  # prev Monday done
        st = logic.scheduled_state(self.conn, self.d, self.bins, date(2026, 6, 24))  # Wed
        self.assertEqual(st["state"], "countdown")
        self.assertEqual(st["days_until"], 5)                # Wed -> next Mon

    def test_due_today(self):
        complete_chore(self.conn, self.d, self.bins["id"], "2026-06-22")
        st = logic.scheduled_state(self.conn, self.d, self.bins, date(2026, 6, 29))  # Mon
        self.assertEqual(st["state"], "due_today")

    def test_overdue_after_due_day(self):
        # Nothing done; on Wed the previous Monday's bins is overdue.
        st = logic.scheduled_state(self.conn, self.d, self.bins, date(2026, 7, 1))
        self.assertEqual(st["state"], "overdue")

    def test_done_today(self):
        complete_chore(self.conn, self.d, self.bins["id"], "2026-06-29")
        st = logic.scheduled_state(self.conn, self.d, self.bins, date(2026, 6, 29))
        self.assertEqual(st["state"], "done")

    def test_hidden_when_far_out(self):
        complete_chore(self.conn, self.d, self.bins["id"], "2026-06-22")
        st = logic.scheduled_state(self.conn, self.d, self.bins, date(2026, 6, 23))  # 6 days out
        self.assertEqual(st["state"], "idle")

    def test_new_chore_not_overdue(self):
        # Created after the previous Monday -> that occurrence shouldn't be overdue.
        self.conn.execute("UPDATE chores SET created_at='2026-06-23T00:00:00' WHERE id=?",
                          (self.bins["id"],))
        self.conn.commit()
        ch = self.conn.execute("SELECT * FROM chores WHERE id=?",
                               (self.bins["id"],)).fetchone()
        st = logic.scheduled_state(self.conn, self.d, ch, date(2026, 6, 24))
        self.assertEqual(st["state"], "countdown")   # not 'overdue'

    def test_for_kid_shows_for_assigned_only(self):
        rows = logic.scheduled_for_kid(self.conn, self.d, date(2026, 6, 24))
        self.assertEqual([r["name"] for r in rows], ["Put bins out at curb"])
        alex = kid_id(self.conn, "alex")
        self.assertEqual(logic.scheduled_for_kid(self.conn, alex, date(2026, 6, 24)), [])

    def test_migration_converts_existing_bins(self):
        # Simulate an old-style row, then run the migration.
        self.conn.execute(
            "UPDATE chores SET type='as_needed', due_weekday=NULL, "
            "reminder_lead_days=NULL, due_label=NULL WHERE id=?", (self.bins["id"],))
        self.conn.commit()
        db._migrate_bins_to_scheduled(self.conn)
        self.conn.commit()
        row = self.conn.execute("SELECT type, due_weekday FROM chores WHERE id=?",
                                (self.bins["id"],)).fetchone()
        self.assertEqual((row["type"], row["due_weekday"]), ("scheduled", 0))


class Assignment(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.a = kid_id(self.conn, "alex")
        self.dn = kid_id(self.conn, "jordan")
        self.dish = self.conn.execute(
            "SELECT id FROM chores WHERE name='Empty the dishwasher'").fetchone()["id"]

    def test_daily_seeded_to_both(self):
        self.assertEqual(len(logic.assigned_daily_chores(self.conn, self.a, date(2026, 6, 24))), 3)
        self.assertEqual(len(logic.assigned_daily_chores(self.conn, self.dn, date(2026, 6, 24))), 3)

    def test_unassign_daily_from_one_kid(self):
        self.conn.execute("DELETE FROM weekly_assignments WHERE chore_id=? AND kid_id=?",
                          (self.dish, self.dn))
        self.conn.commit()
        self.assertEqual(len(logic.assigned_daily_chores(self.conn, self.dn, date(2026, 6, 24))), 2)
        self.assertEqual(len(logic.assigned_daily_chores(self.conn, self.a, date(2026, 6, 24))), 3)
        # Jordan's checklist is now just his 2 chores.
        for ch in logic.assigned_daily_chores(self.conn, self.dn, date(2026, 6, 24)):
            complete_chore(self.conn, self.dn, ch["id"], "2026-06-24")
        done, _ = logic.checklist_status(self.conn, self.dn, date(2026, 6, 24))
        self.assertTrue(done)

    def test_rotating_daily_assignment(self):
        self.conn.execute("UPDATE chores SET is_rotating=1 WHERE id=?", (self.dish,))
        self.conn.execute("DELETE FROM weekly_assignments WHERE chore_id=?", (self.dish,))
        self.conn.execute("INSERT INTO rotating_chore_assignments (chore_id, kid_id, "
                          "week_start_date, is_override) VALUES (?,?,?,0)",
                          (self.dish, self.a, "2026-06-22"))
        self.conn.commit()
        ch = self.conn.execute("SELECT * FROM chores WHERE id=?", (self.dish,)).fetchone()
        self.assertTrue(logic.chore_assigned_to(self.conn, ch, self.a, date(2026, 6, 22)))
        self.assertFalse(logic.chore_assigned_to(self.conn, ch, self.dn, date(2026, 6, 22)))

    def test_migrate_daily_assignments(self):
        self.conn.execute("DELETE FROM weekly_assignments WHERE chore_id IN "
                          "(SELECT id FROM chores WHERE type='daily')")
        self.conn.execute("DELETE FROM settings WHERE key='daily_assignment_migrated'")
        self.conn.commit()
        db._migrate_daily_assignments(self.conn)
        self.conn.commit()
        self.assertEqual(len(logic.assigned_daily_chores(self.conn, self.a, date(2026, 6, 24))), 3)
        self.assertEqual(len(logic.assigned_daily_chores(self.conn, self.dn, date(2026, 6, 24))), 3)


class Scheduler(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.a = kid_id(self.conn, "alex")
        self.conn.execute("UPDATE chores SET created_at='2026-06-01T00:00:00' "
                          "WHERE name='Put bins out at curb'")
        self.conn.commit()

    def _sent(self, ntype):
        return self.conn.execute(
            "SELECT COUNT(*) n FROM notifications_sent WHERE notification_type=?",
            (ntype,)).fetchone()["n"]

    def test_morning_reminder_fires_once_when_incomplete(self):
        now = datetime(2026, 6, 24, 10, 30, tzinfo=NY)  # Wed, after 10:00
        d = now.date()
        scheduler.maybe_morning_reminder(self.conn, now, d)
        scheduler.maybe_morning_reminder(self.conn, now, d)  # idempotent
        # One per kid (both incomplete), no duplicates on the second call.
        self.assertEqual(self._sent("morning_reminder"), 2)

    def test_no_reminder_before_time(self):
        now = datetime(2026, 6, 24, 9, 30, tzinfo=NY)  # before 10:00
        scheduler.maybe_morning_reminder(self.conn, now, now.date())
        self.assertEqual(self._sent("morning_reminder"), 0)

    def test_no_reminder_when_done(self):
        d = date(2026, 6, 24)
        complete_checklist(self.conn, self.a, d)
        now = datetime(2026, 6, 24, 10, 30, tzinfo=NY)
        scheduler.maybe_morning_reminder(self.conn, now, d)
        # Alex done -> only Jordan gets reminded.
        rows = self.conn.execute(
            "SELECT kid_id FROM notifications_sent WHERE notification_type='morning_reminder'"
        ).fetchall()
        self.assertEqual([r["kid_id"] for r in rows], [kid_id(self.conn, "jordan")])

    def test_reminder_off_disables(self):
        logic.set_setting(self.conn, "reminder_time", "off")
        now = datetime(2026, 6, 24, 11, 0, tzinfo=NY)
        scheduler.maybe_morning_reminder(self.conn, now, now.date())
        self.assertEqual(self._sent("morning_reminder"), 0)

    def test_no_reminder_on_paused_day(self):
        now = datetime(2026, 7, 8, 10, 30, tzinfo=NY)  # inside Alaska cruise
        scheduler.maybe_morning_reminder(self.conn, now, now.date())
        self.assertEqual(self._sent("morning_reminder"), 0)

    def test_sunday_summary_fires_once_after_7pm(self):
        now = datetime(2026, 6, 28, 19, 5, tzinfo=NY)  # Sunday 7:05pm
        d = now.date()
        scheduler.maybe_sunday_summary(self.conn, now, d)
        scheduler.maybe_sunday_summary(self.conn, now, d)
        self.assertEqual(self._sent("weekly_summary"), 1)

    def test_no_summary_before_7pm_or_other_day(self):
        scheduler.maybe_sunday_summary(self.conn, datetime(2026, 6, 28, 18, 0, tzinfo=NY), date(2026, 6, 28))
        scheduler.maybe_sunday_summary(self.conn, datetime(2026, 6, 24, 20, 0, tzinfo=NY), date(2026, 6, 24))
        self.assertEqual(self._sent("weekly_summary"), 0)

    def test_summary_message_format(self):
        msg = scheduler.build_summary(self.conn, date(2026, 6, 28))
        self.assertIn("Alex: Reading 0/2.9 hr ✗, Outdoor Time 0/5 hr ✗", msg)
        self.assertIn("Jordan:", msg)

    def test_heartbeat_noop_without_url(self):
        # No HEALTHCHECK_URL configured -> no-op, no network, no raise.
        self.assertFalse(scheduler.heartbeat({}))

    def test_scheduled_due_fires_once_in_evening(self):
        d = date(2026, 6, 22)  # Monday; bins assigned to Jordan this week
        evening = datetime(2026, 6, 22, 19, 0, tzinfo=NY)
        scheduler.maybe_scheduled_due(self.conn, evening, d)
        scheduler.maybe_scheduled_due(self.conn, evening, d)  # dedup
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM notifications_sent "
            "WHERE notification_type LIKE 'scheduled_due:%'").fetchone()["n"]
        self.assertEqual(n, 1)   # Jordan only, once

    def test_scheduled_due_silent_before_evening(self):
        scheduler.maybe_scheduled_due(self.conn, datetime(2026, 6, 22, 12, 0, tzinfo=NY),
                                      date(2026, 6, 22))
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM notifications_sent "
            "WHERE notification_type LIKE 'scheduled_due:%'").fetchone()["n"]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()


