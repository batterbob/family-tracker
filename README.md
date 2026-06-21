# ChoreBoard

A self-hosted web app for keeping kids on track during a structured program — summer, school year, whatever you're running. Kids check off daily chores and log reading and outdoor time on their own. Parents get a read-only status page and a password-protected admin dashboard. There area also built-in notiications to services like Pushover and others.

Plain Flask + SQLite + vanilla JS. Runs as a single Docker container. No build step, no database server to manage.

MIT License.

---

## What it does

Kids get their own page (by URL — no login needed) with a daily checklist, weekly chores, and activity logs. Each week, if they hit their reading and outdoor targets, they earn a bonus star. The admin dashboard shows you everything at a glance: today's checklist status, weekly progress, and activity totals.

A scheduler fires a mid-morning reminder via a notification service (e.g. Pushover) if a kid's checklist isn't done, and a Sunday evening summary of the week. Vacations and special periods pause or credit activities automatically.

---

## Pages

| URL | Who | Notes |
|-----|-----|-------|
| `/alex`, `/jordan` | Kids | URL is the identity — no login. Chores, logs, scoreboard. |
| `/status` | Parents | Read-only. No password. Auto-refreshes every minute. |
| `/admin` | Parents | Password-protected dashboard. |
| `/admin/settings` | Parents | Targets, reminders, program window, vacations, notifications, password. |
| `/admin/logs` | Parents | Edit or delete this week's activity entries. |
| `/admin/history` | Parents | Week-by-week breakdown and streaks. |

---

## Screenshots

**Admin dashboard** — today's checklist status and weekly progress for each kid:

![Admin dashboard](docs/screenshots/admin-dashboard.png)

**Kid page** — daily checklist, weekly chores, and activity logging:

![Kid page](docs/screenshots/kid-page.png)

**Status page** — parent read-only view, no password:

![Status page](docs/screenshots/status-page.png)

**Settings** — kids, activity targets, program window, notifications:

![Settings](docs/screenshots/settings-page.png)

---

## Configuration (`.env`)

All secrets and runtime config live in a `.env` file in the project root. It is gitignored — never commit it. Create it from this template:

```dotenv
ADMIN_PASSWORD=change-me-before-first-run
PORT=7823
TZ=America/New_York
CHORE_DEBUG=0

# Notifications (pick one service — set the rest in /admin/settings after startup)
# NOTIFY_SERVICE=pushover
# NOTIFY_PUSHOVER_APP_TOKEN=your-token
# NOTIFY_PUSHOVER_USER_KEY=your-key

# Optional: ping a push monitor URL each minute to confirm the scheduler is running
# HEALTHCHECK_URL=https://your-monitor-url/ping
```

**`ADMIN_PASSWORD`** — set this before the first run. It gets hashed into the database on startup and is not re-read afterward. To change it later, use `/admin/settings`.

**Notifications** — supported services: Pushover, Telegram, Discord, Slack, ntfy, Gotify, or any Apprise URL. Set credentials in Settings after the first run — or put them in the environment and they'll be picked up automatically.

**`TZ`** — all date logic runs in local time. Use a [tz database name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) (e.g. `America/Chicago`, `Europe/London`).

**`CHORE_DEBUG`** — leave `0` in production. When `1`, you can append `?today=YYYY-MM-DD` to any page to preview other dates before the program starts.

---

## Deploy on Docker

```bash
git clone https://github.com/batterbob/choreboard.git
cd choreboard
cp .env.example .env   # edit with your values
docker compose up -d --build
```

Confirm it came up:

```bash
docker compose logs -f
# should see: Scheduler started (minute interval)
```

The app runs on port 7823. The SQLite database persists in `./data/` (mounted as a Docker volume), so it survives container restarts and rebuilds.

**Updating:** pull new code, then `docker compose up -d --build`. The database migrates itself non-destructively.

---

## First-run setup

On first startup with an empty database, the app redirects to a setup wizard. Enter your kids' names (URL slugs are auto-filled), your timezone, and the program window (start and end dates). You can add chores and fine-tune everything from the admin dashboard once it's running.

---

## Monitoring

The app exposes a health endpoint at `/healthz`. It returns `{"status":"ok"}` with HTTP 200 when the app and database are healthy, and 503 if the database can't be reached.

For scheduler monitoring, set `HEALTHCHECK_URL` in your `.env`. The scheduler pings that URL every minute; if the tick stops, your monitor knows. Works with Uptime Kuma (push monitor), Healthchecks.io, or any HTTP endpoint. If `HEALTHCHECK_URL` is blank, the heartbeat is disabled.

---

## How it works

**Single process.** The app runs single-process so the background scheduler fires exactly once. Don't put it behind a multi-worker server.

**Scheduler.** A minute-interval APScheduler job checks whether to send the morning reminder (if a kid's checklist isn't done by `reminder_time`) and the Sunday 7pm week summary. Everything else — weekly results, activity credit, chore rotation — computes on page load.

**Database.** SQLite in WAL mode. The schema uses non-destructive migrations (`ALTER TABLE ADD COLUMN IF NOT EXISTS`) so updating the app never drops your data.

**Timezone.** Every date is the local date in `TZ`. The `tzdata` package is a dependency so this works on both Windows and slim containers.

**Chore types.**
- *Daily* — appear every day; must be checked off each day to count.
- *Weekly* — appear in the "This week" section; just needs to be done once before Sunday.
- *Scheduled* — tied to a specific weekday with a countdown (e.g. "trash goes out Monday night").
- *As-needed* — assigned by a parent from the admin dashboard for that day only.
- *Rotating* — switches between kids each week automatically.

**Special periods.** Vacation periods pause the program (no chores, no reminders). Activity-credit periods auto-log outdoor minutes for camp, travel, or any other structured program.

