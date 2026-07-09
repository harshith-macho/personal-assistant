# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Jerviss** — Harshith Mittapally's personal AI Telegram bot. Long-polls the Telegram Bot API, handles slash commands, and routes free-text through a Claude tool-use agentic loop. Integrates Gmail, Google Calendar, LinkedIn (Playwright automation), stock watchlist, and job tracking.

## Running

```bash
# Install
pip install -r requirements.txt
python -m playwright install chromium

# One-time Google Calendar auth (generates token.json)
python setup_calendar.py   # or: python calendar_auth.py

# Start (foreground / dev)
python bot_server.py

# Background / production
pkill -f bot_server.py; rm -f "$TEMP/akshay_bot.pid"
nohup python bot_server.py > assistant.log 2>&1 &
```

Credentials are loaded from `~/.env` (never `.env` in the repo). Copy `.env.example` and fill in all values.

**Singleton guard:** on startup, the bot writes its PID to `$TEMP/akshay_bot.pid` and exits if another instance is alive. To force a restart, delete that file first.

## Architecture

### Core loop — `bot_server.py`

Long-polls `getUpdates` (30 s timeout). On each update:
- `is_authorized()` checks sender is personal chat or the configured group
- Slash commands dispatch directly to handler functions
- Non-command text first checks `~/.apply_qa.json` — if `linkedin_apply.py` is mid-form and waiting for a field answer, the reply is passed back via that file and the update is consumed (see Form Q&A below)
- Otherwise free-text goes to `ask_claude()` — an agentic tool-use loop that calls Claude until `stop_reason != "tool_use"`, accumulating tool results. Keeps the last 20 messages in `conversation_history` (in-memory, resets on restart).
- Claude model: `claude-haiku-4-5-20251001`

**On startup:** `startup_job_catchup()` resends any `pending` jobs found in the last 2 hours back to the Jobs topic (handles restarts mid-session).

**Background threads (all daemon, started in `start_background_tasks()`):**
- `_job_search_worker` — triggers `linkedin_apply.py autoapply` every 45 minutes
- `_health_check_worker` — sends a health ping to the Jobs topic every 6 hours
- `_networking_worker` — runs `linkedin_network.run_networking()` daily at 10am
- `_tech_alerts_worker` — calls `linkedin_alerts.run_hourly()` every hour
- `_linkedin_courses_worker` — calls `linkedin_alerts.run_linkedin_courses()` every 6 hours
- `_resume_update_worker` — calls `resume_updater.run_resume_update()` every Sunday at 9am

**Callback routing (inline buttons):**
- `fc_` / `fco_` / `fs_` prefixes → `linkedin_feed.handle_feed_callback` (comment/connect/skip)
- `ru_yes_` / `ru_no_` prefixes → `resume_updater.handle_resume_callback`
- Everything else → `linkedin_apply.handle_callback` (job approve/skip)

**Error alerting:** 5+ consecutive poll errors trigger a Telegram alert to the Jobs topic (1hr cooldown between alerts).

### Message routing — `telegram_topics.py`

All outbound sends go through here. Reads `TELEGRAM_GROUP_ID` + `TELEGRAM_TOPIC_*` from `~/.env` and routes to the correct topic thread (`Chat`, `Emails`, `Jobs`, `Stocks`, `Daily`). If no group is configured, falls back to `TELEGRAM_CHAT_ID`.

### LinkedIn automation — `linkedin_apply.py` + `linkedin_auth.py`

Playwright-driven. `linkedin_auth.py` performs a browser login and saves `linkedin_session.json`; subsequent calls restore that session. Modes: `find` (search → send to Telegram for approval), `apply` (apply to approved jobs), `autoapply` (find + apply in one pass), `login` (re-authenticate). Job search keywords and target profile are hard-coded as `JOB_KEYWORDS` and `MY_INTERESTS` constants at the top of `linkedin_apply.py`. `AUTO_APPLY_SCORE_THRESHOLD` (default 7) controls how selective auto-apply is.

**Form Q&A IPC:** When a LinkedIn form has an unknown field, `linkedin_apply.py` writes `~/.apply_qa.json` with `status: "pending"` and the field label, then polls for `status: "answered"`. `bot_server.py` intercepts the next non-command message and writes the answer back. If the subprocess times out, the answer is cached via `form_filler._save_cached_answer()` for retry.

### LinkedIn networking — `linkedin_network.py`

Playwright-driven connection requests to HRs, recruiters, and peers in Ireland. Run via `/network` or the daily background thread at 10am. `get_status_report()` returns stats shown by `/netstatus`.

### Tech alerts — `linkedin_alerts.py`

Fetches tech news, LinkedIn Learning courses, and updates; sends to the Daily topic. Called hourly by background thread and on-demand via `/alerts`.

### LinkedIn feed — `linkedin_feed.py`

Scrapes the LinkedIn feed with Playwright, scores posts via Claude (surfaces only those scoring ≥ 7/10), and sends them to the Jobs topic for approval. On approval: Claude generates and posts a comment; optionally sends a connection request. Callback data prefixes: `fc_` (comment approve), `fco_` (comment + connect), `fs_` (skip). Feed posts are stored in the `feed_posts` table inside `applied_jobs.db`.

### Job pipeline — `job_tracker.py`

SQLite at `applied_jobs.db` (auto-created, shared with `linkedin_feed.py`). Two tables:
- `jobs` — job applications; stages: `applied → phone_screen → interview → offer → rejected/withdrawn`
- `feed_posts` — LinkedIn feed posts managed by `linkedin_feed.py`

The bot's `/mystatus` and `/update` commands call `format_status_report()` and `handle_update_command()` from this module.

### Scheduled scripts (run via cron, not the bot process)

| Script | Schedule |
|---|---|
| `smart_email_alert.py` | Every 15 min — priority Gmail watch |
| `morning_briefing.py` | 8 am weekdays — calendar + emails summary |
| `tech_digest.py` | 8:30 am weekdays — tech news digest |
| `linkedin_jobs.py` | 9 am weekdays — parse LinkedIn job alert emails |
| `linkedin_apply.py autoapply` | Every 2 h weekdays — auto-apply loop |
| `job_tracker.py` | 6 pm weekdays — daily pipeline summary |

## Slash commands

| Command | Description |
|---|---|
| `/autoapply` | Find + apply to all new Easy Apply jobs automatically (no approval) |
| `/findjobs` | Search jobs and send each to Jobs topic for manual approval |
| `/applyjobs` | Apply only to jobs manually approved via `/findjobs` |
| `/mystatus` | Show full job application pipeline |
| `/update <id> <stage> [note]` | Update an application stage |
| `/feed` | Scan LinkedIn feed, send top posts for comment/connection approval |
| `/network` | Send LinkedIn connection requests to HRs, recruiters & peers in Ireland |
| `/netstatus` | Show networking stats — total sent, by type, recent requests |
| `/resumeupdate` | Analyze recent job descriptions vs resume, post suggestions to Jobs topic |
| `/alerts` | Fetch latest tech news, courses & LinkedIn updates (posts to Daily) |
| `/schedule` | Post today's Google Calendar events to Daily topic |
| `/emails` | Summarize last 2 hours of Gmail |
| `/stocks` | Trigger an immediate stock drop check |
| `/jobs` | Parse LinkedIn job alert emails from Gmail |
| `/post <topic>` | Generate and post to LinkedIn on the given topic |
| `/digest` | Fetch and post tech news digest to Daily topic |
| `/login` | Open LinkedIn login browser window (use when session expires) |

Free-form text in any topic or DM goes to `ask_claude()`.

## Key files to know

| File | Purpose |
|---|---|
| `bot_server.py` | Entry point; `SYSTEM_PROMPT` and `TOOLS` list live here — update for persona changes |
| `telegram_topics.py` | Topic routing; edit `TOPICS` dict if thread IDs change |
| `resume_tailor.py` | Contains hardcoded `RESUME` constant — update when CV changes |
| `linkedin_apply.py` | `JOB_KEYWORDS`, `MY_INTERESTS`, `AUTO_APPLY_SCORE_THRESHOLD` control job targeting |
| `linkedin_network.py` | LinkedIn connection request automation; `get_status_report()` for `/netstatus` |
| `linkedin_alerts.py` | Tech news + LinkedIn Learning scanner; `run_hourly()` + `run_linkedin_courses()` |
| `resume_updater.py` | Analyzes job descriptions vs resume; `run_resume_update()` + `handle_resume_callback()` |
| `form_filler.py` | Caches form field answers; `_save_cached_answer()` called on late/timed-out Q&A replies |
| `resume_manager.py` | Standalone script (not wired into bot_server.py): receives a resume PDF, saves to role slots (`resume_cloud`, `resume_ml`, `resume_swe`, `resume_devops` under `resumes/`), and uploads to LinkedIn as default resume |
| `job_tracker.py` | Schema defined in `init_db()`; two tables: `jobs` and `feed_posts` |
| `calendar_bot.py` | Google Calendar API; timezone defaults to `America/Los_Angeles` |

## Gitignored files you must create locally

| File | How to get it |
|---|---|
| `~/.env` | Copy `.env.example`, fill in values |
| `credentials.json` | Google Cloud Console → OAuth 2.0 Desktop app |
| `token.json` | Auto-generated by `python setup_calendar.py` |
| `linkedin_session.json` | Auto-generated on first LinkedIn login |
| `applied_jobs.db` | Auto-created by `job_tracker.py` on first run |

## Troubleshooting

**"Bot already running" / 409 Conflict from Telegram** — two instances are polling. Kill all and remove the PID file.

On Windows:
```powershell
Stop-Process -Name python -Force
Remove-Item "$env:TEMP\akshay_bot.pid" -ErrorAction SilentlyContinue
```

On macOS/Linux:
```bash
pkill -f bot_server.py
rm -f "$TEMP/akshay_bot.pid"
```

**Google Calendar errors** — `token.json` expired; re-run `python calendar_auth.py` after deleting the old token.

**LinkedIn login fails** — delete `linkedin_session.json` and run `python linkedin_auth.py` to re-authenticate.

**Gmail connection refused** — re-enable 2-Step Verification, generate a new App Password at myaccount.google.com → Security → App Passwords, update `GMAIL_APP_PASSWORD` in `~/.env`.

**Bot not receiving group messages** — disable bot privacy mode: message @BotFather → `/setprivacy` → select bot → Disable. Also confirm the bot is a group member.
