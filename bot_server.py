#!/usr/bin/env python3
"""
Jerviss — Harshith's personal AI assistant Telegram bot.
Runs continuously, responds to messages via Claude, and accepts commands.
"""

import requests
import anthropic
import time
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from dotenv import dotenv_values
from calendar_bot import get_calendar_summary
from linkedin_jobs import get_job_summary
from linkedin_post import post_from_topic
from job_tracker import format_status_report, handle_update_command
import subprocess

# Line-buffer stdout/stderr so `print()` shows up in assistant.log in real time —
# launchd redirects them to a file, which Python block-buffers by default,
# making the log look frozen even while the process is actively running.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Runtime paths — resolved dynamically so the repo works on any machine
BASE       = Path(__file__).parent
PYTHON     = sys.executable
STOCKS_DIR = Path.home() / "Akshay" / "stockspredictor"

# Prevent duplicate instances
import platform as _platform
PIDFILE = Path(os.environ.get("TEMP", "/tmp")) / "akshay_bot.pid"
if PIDFILE.exists():
    old_pid = int(PIDFILE.read_text().strip())
    _bot_alive = False
    try:
        os.kill(old_pid, 0)
        # PID exists — confirm it's actually a Python process (PID reuse guard)
        import subprocess as _sp
        if _platform.system() == "Windows":
            _out = _sp.run(
                ["tasklist", "/FI", f"PID eq {old_pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True
            ).stdout.lower()
            _bot_alive = "python" in _out
        else:
            _out = _sp.run(
                ["ps", "-p", str(old_pid), "-o", "comm="],
                capture_output=True, text=True
            ).stdout.lower()
            _bot_alive = "python" in _out
    except OSError:
        pass  # Process gone
    if _bot_alive:
        print(f"Bot already running (PID {old_pid}). Exiting.")
        sys.exit(0)
    print(f"Stale PID file (PID {old_pid} is not Python) — continuing.")
PIDFILE.write_text(str(os.getpid()))

import atexit
atexit.register(lambda: PIDFILE.unlink(missing_ok=True))

config = dotenv_values(Path.home() / ".env")

ANTHROPIC_KEY  = config.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = config.get("TELEGRAM_CHAT_ID")

TELEGRAM_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Conversation history for context (last 20 messages)
conversation_history = []

# Telegram message_id -> fetched email metadata, so a native Telegram "reply" to
# an email digest message can be matched back to the email it's replying to.
EMAIL_INDEX_FILE = BASE / "email_index.json"


def _load_email_index():
    if EMAIL_INDEX_FILE.exists():
        try:
            return json.loads(EMAIL_INDEX_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_email_index(index):
    # Keep only the most recent 300 entries to avoid unbounded growth
    trimmed = dict(list(index.items())[-300:])
    EMAIL_INDEX_FILE.write_text(json.dumps(trimmed))


EMAIL_INDEX = _load_email_index()

# Pending outbound email drafts awaiting Send/Cancel approval (draft_id -> draft dict).
# In-memory only, like conversation_history — resets on restart.
PENDING_EMAIL_DRAFTS = {}

SYSTEM_PROMPT = """You are Jerviss, Harshith Mittapally's personal AI assistant. Here's everything you know about him:

PERSONAL:
- Full name: Harshith Mittapally
- Email: harshithreddy200811@gmail.com | Phone: +353899879815
- Originally from: Telangana, India
- Currently based in: Dublin, Ireland
- Education: MSc Computing, Griffith College Dublin (expected graduation Sep/Oct 2026)
  * Sem 1 passed, Sem 2 results pending, thesis in progress
  * Thesis title: "Intelligent Network Intrusion Detection Using Explainable Machine Learning and Cloud Deployment"

EDUCATION:
- MSc Computer Science, Griffith College Dublin, Ireland (Sep 2025 – Sep 2026)
  * Thesis: "Intelligent Network Intrusion Detection Using Explainable Machine Learning and Cloud Deployment"
  * Projects: cloud-native apps, CI/CD automation, mobile dev, ML, containerized microservices
- BTech Computer Science, CMR College of Engineering & Technology, Hyderabad, India (2020–2024)

TECHNICAL SKILLS:
- Languages: C#, Python, Bash, SQL
- Cloud: AWS (EC2, EKS, IAM, S3, ALB, CloudWatch)
- Containers & Orchestration: Docker, Kubernetes, Helm
- CI/CD: Jenkins, GitHub Actions
- Infrastructure as Code: Terraform, YAML
- Web & APIs: ASP.NET Core, RESTful APIs, HTML, CSS, JavaScript
- Databases: SQL Server, PostgreSQL
- Monitoring: Prometheus, Grafana
- Tools: Git, GitHub, Visual Studio, VS Code, Postman, SSMS
- OS: Linux, Windows

EXPERIENCE:
- Graduate Program Projects, Griffith College Dublin (Oct 2025 – Present)
  * Built backend services in C#/.NET, containerized with Docker and Kubernetes
  * Deployed cloud-hosted services on AWS (EC2, EKS, S3)
  * Designed CI/CD pipelines with Jenkins and GitHub Actions
  * Provisioned infrastructure using Terraform and Kubernetes YAML
  * Implemented monitoring and logging with Prometheus/Grafana

GOALS & INTERESTS:
- Land a DevOps / Software Engineering / Cloud role in Ireland
- Deepen AWS expertise (pursuing certification)
- Explore ML/AI — neural networks, deep learning, MLOps, cybersecurity ML
- Building personal AI/automation tools

PRIMARY USE CASES FOR THIS BOT:
- Analyse Gmail and surface only the important emails
- Apply to software jobs on LinkedIn and other platforms (Ireland-focused)
- General assistant for daily tasks, scheduling, and questions

Be concise, friendly, and helpful. Keep responses short — this is Telegram, not an essay.
Use emojis occasionally. Help with job hunting, AWS/cloud advice, ML questions, email summaries, and anything else Harshith needs."""


def send_message(text, parse_mode="Markdown", thread_id=None, src_chat_id=None, reply_markup=None):
    """Send a message back to where it came from, or the group topic if not specified."""
    from telegram_topics import GROUP_ID, TOPICS, _TARGET
    # Reply directly to the originating chat (DM or different group)
    if src_chat_id and src_chat_id != str(GROUP_ID):
        payload = {"chat_id": src_chat_id, "text": text, "parse_mode": parse_mode}
    else:
        payload = {"chat_id": _TARGET, "text": text, "parse_mode": parse_mode}
        effective_thread = thread_id if thread_id else (TOPICS["chat"] if GROUP_ID else 0)
        if effective_thread:
            payload["message_thread_id"] = effective_thread
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    if payload.get("parse_mode") is None:
        # Telegram rejects an explicit null parse_mode ("unsupported parse_mode") —
        # it must be omitted entirely to send plain, unparsed text.
        payload.pop("parse_mode", None)
    resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    if not resp.ok:
        print(f"send_message failed: {resp.status_code} {resp.text}")
        if resp.status_code == 429:
            # Rate-limited — Telegram tells us how long to back off
            retry_after = resp.json().get("parameters", {}).get("retry_after", 2)
            time.sleep(retry_after + 0.5)
        else:
            time.sleep(1)
        resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if not resp.ok:
            print(f"send_message retry also failed: {resp.status_code} {resp.text}")
    return resp.ok


def get_updates(offset=None):
    """Poll Telegram for new messages and callback queries."""
    params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
    if offset:
        params["offset"] = offset
    resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    if not resp.ok:
        data = resp.json()
        if data.get("error_code") == 409:
            print("409 Conflict — another instance running, waiting 60s...")
            time.sleep(60)
        return {"result": []}
    return resp.json()


def is_authorized(msg):
    """Accept messages from the group or the personal chat."""
    chat = msg.get("chat", {})
    chat_id   = str(chat.get("id", ""))
    chat_type = chat.get("type", "")
    from telegram_topics import GROUP_ID
    return chat_id == TELEGRAM_CHAT or chat_id == str(GROUP_ID) or chat_type in ("group", "supergroup")


def handle_email_callback(update):
    """Send or discard a pending email draft (from a reply or /compose_email) on button tap."""
    cb        = update.get("callback_query", {})
    data      = cb.get("data", "")
    cb_id     = cb.get("id")
    chat_id   = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    thread_id = cb.get("message", {}).get("message_thread_id")

    is_send  = data.startswith("em_send_")
    draft_id = data[len("em_send_"):] if is_send else data[len("em_cancel_"):]
    draft    = PENDING_EMAIL_DRAFTS.pop(draft_id, None)
    print(f"[email-callback] data={data} draft_id={draft_id} found_draft={draft is not None} known_drafts={list(PENDING_EMAIL_DRAFTS.keys())}")

    if not draft:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                       json={"callback_query_id": cb_id, "text": "Draft expired or already handled."})
        send_message(
            "⚠️ That draft is no longer available (bot restarted since, or it was already sent/cancelled).",
            thread_id=thread_id, src_chat_id=chat_id
        )
        return

    if not is_send:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Cancelled."})
        send_message("🗑️ Draft discarded — nothing sent.", thread_id=thread_id, src_chat_id=chat_id)
        return

    try:
        from email_actions import send_via_smtp
        send_via_smtp(
            draft["from_account"], draft["to"], draft["subject"], draft["body"],
            in_reply_to=draft.get("in_reply_to"), references=draft.get("references")
        )
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Sent!"})
        send_message(f"✅ Sent to {draft['to']} from {draft['from_account']}.", thread_id=thread_id, src_chat_id=chat_id)
    except Exception as e:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Failed to send."})
        send_message(f"⚠️ Failed to send email: {e}", thread_id=thread_id, src_chat_id=chat_id)


TOOLS = [
    {
        "name": "create_calendar_event",
        "description": "Create a Google Calendar event for Harshith. If attendee emails are provided, send them a Google Meet invite. Always add a Google Meet link when scheduling a meeting with other people.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":      {"type": "string", "description": "Event title"},
                "date":       {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "start_time": {"type": "string", "description": "Start time in HH:MM 24h format"},
                "end_time":   {"type": "string", "description": "End time in HH:MM 24h format"},
                "location":   {"type": "string", "description": "Optional physical location"},
                "attendees":  {"type": "array", "items": {"type": "string"}, "description": "List of attendee email addresses — they will receive a Google Meet invite"},
                "add_meet":   {"type": "boolean", "description": "Add a Google Meet video link (default true when attendees present)"}
            },
            "required": ["title", "date", "start_time", "end_time"]
        }
    },
    {
        "name": "list_calendar_events",
        "description": "List Harshith's upcoming calendar events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days ahead to look (default 7)"}
            }
        }
    },
    {
        "name": "delete_calendar_event",
        "description": "Delete a calendar event by title (and optional date).",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date":  {"type": "string", "description": "YYYY-MM-DD, optional"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "get_job_applications",
        "description": "Get Harshith's full job application pipeline/summary from the database. Each job has a short ref id like J012.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stage": {"type": "string", "description": "Filter by stage: applied, phone_screen, interview, offer, rejected. Leave empty for all."}
            }
        }
    },
    {
        "name": "get_job_status",
        "description": "Look up the current status of ONE specific job application by its short reference ID (e.g. J012, J7) or a partial LinkedIn job ID. Use this whenever Harshith asks about a particular role by its ID, e.g. 'what's the status of J12?' or 'did I apply to J005?'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref_id": {"type": "string", "description": "The job's short reference ID, e.g. J012 (also accepts j12, 12, or a partial LinkedIn ID)."}
            },
            "required": ["ref_id"]
        }
    },
    {
        "name": "fetch_emails",
        "description": "Fetch and summarize Harshith's recent Gmail emails.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "How many hours back to look (default 2)"}
            }
        }
    },
    {
        "name": "compose_email",
        "description": "Draft a new email to send from Harshith's Gmail. Write a complete, polished email body yourself based on the conversation — don't just pass Harshith's raw instruction through. The draft is posted to the Emails topic for Harshith to approve before it actually sends; nothing sends automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body":    {"type": "string", "description": "Full email body text, polished and ready to send"},
                "account": {"type": "string", "description": "Which of Harshith's Gmail accounts to send from (optional — defaults to his primary account)"}
            },
            "required": ["to", "subject", "body"]
        }
    },
    {
        "name": "check_stocks",
        "description": "Trigger a stock drop check for Harshith's watchlist.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "add_stock",
        "description": "Add a stock ticker to Harshith's watchlist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol e.g. AAPL, TSLA"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "remove_stock",
        "description": "Remove a stock ticker from Harshith's watchlist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol to remove"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "list_stocks",
        "description": "List all stocks currently in Harshith's watchlist.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "search_jobs",
        "description": "Search LinkedIn for new Easy Apply jobs matching Harshith's profile.",
        "input_schema": {"type": "object", "properties": {}}
    }
]


def execute_tool(name, params):
    """Run a tool and return a string result."""
    try:
        if name == "create_calendar_event":
            from calendar_bot import get_calendar_service
            import uuid
            svc = get_calendar_service()
            date = params["date"]
            attendees = params.get("attendees") or []
            add_meet = params.get("add_meet", bool(attendees))
            event = {
                "summary": params["title"],
                "start": {"dateTime": f"{date}T{params['start_time']}:00", "timeZone": "Europe/Dublin"},
                "end":   {"dateTime": f"{date}T{params['end_time']}:00",   "timeZone": "Europe/Dublin"},
            }
            if params.get("location"):
                event["location"] = params["location"]
            if attendees:
                event["attendees"] = [{"email": e} for e in attendees]
            if add_meet:
                event["conferenceData"] = {
                    "createRequest": {"requestId": str(uuid.uuid4()), "conferenceSolutionKey": {"type": "hangoutsMeet"}}
                }
            created = svc.events().insert(
                calendarId="primary", body=event,
                conferenceDataVersion=1 if add_meet else 0,
                sendUpdates="all" if attendees else "none"
            ).execute()
            meet_link = created.get("hangoutLink") or (created.get("conferenceData") or {}).get("entryPoints", [{}])[0].get("uri", "")
            result = f"✅ Created: *{created['summary']}* on {date} {params['start_time']}–{params['end_time']}"
            if meet_link:
                result += f"\n🎥 Meet link: {meet_link}"
            if attendees:
                result += f"\n📧 Invites sent to: {', '.join(attendees)}"
            return result

        elif name == "list_calendar_events":
            from calendar_bot import get_calendar_summary
            from telegram_topics import send_daily
            summary = get_calendar_summary()
            send_daily(summary)
            return "📅 Calendar posted to Daily topic."

        elif name == "delete_calendar_event":
            from calendar_bot import get_calendar_service
            from datetime import datetime, timezone, timedelta
            svc = get_calendar_service()
            now = datetime.now(timezone.utc)
            result = svc.events().list(
                calendarId="primary", timeMin=now.isoformat(),
                timeMax=(now + timedelta(days=60)).isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=50
            ).execute()
            title_lower = params["title"].lower()
            date_filter = params.get("date", "")
            for e in result.get("items", []):
                if title_lower in e.get("summary", "").lower():
                    start = e["start"].get("dateTime", e["start"].get("date", ""))
                    if not date_filter or date_filter in start:
                        svc.events().delete(calendarId="primary", eventId=e["id"]).execute()
                        return f"✅ Deleted: {e['summary']}"
            return f"❌ No event found matching '{params['title']}'"

        elif name == "get_job_applications":
            from job_tracker import format_status_report
            return format_status_report()

        elif name == "get_job_status":
            from job_tracker import get_job_status
            return get_job_status(params.get("ref_id", ""))

        elif name == "fetch_emails":
            from email_bot import fetch_recent_emails, summarize_with_claude
            emails = fetch_recent_emails(hours=params.get("hours", 1))
            if emails:
                post_and_index_emails(emails)  # so each one can be replied to directly
            return summarize_with_claude(emails)

        elif name == "compose_email":
            import uuid
            from email_actions import ACCOUNTS, DEFAULT_ACCOUNT
            from telegram_topics import send as send_topic
            account = params.get("account") or DEFAULT_ACCOUNT
            if account not in ACCOUNTS:
                account = DEFAULT_ACCOUNT
            draft_id = uuid.uuid4().hex[:10]
            PENDING_EMAIL_DRAFTS[draft_id] = {
                "from_account": account,
                "to":           params["to"],
                "subject":      params["subject"],
                "body":         params["body"],
                "in_reply_to":  None,
                "references":   None,
            }
            send_topic(
                f"✉️ New email draft — from {account}\n"
                f"To: {params['to']}\nSubject: {params['subject']}\n\n{params['body']}",
                topic="emails",
                parse_mode=None,
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Send",   "callback_data": f"em_send_{draft_id}"},
                    {"text": "❌ Cancel", "callback_data": f"em_cancel_{draft_id}"}
                ]]}
            )
            return "📧 Draft posted to the Emails topic for your approval — it won't send until you tap Send."

        elif name == "check_stocks":
            subprocess.Popen(
                [PYTHON, str(STOCKS_DIR / "stock_alerts.py")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return "📊 Stock check triggered — results coming to Stocks topic shortly."

        elif name == "add_stock":
            import sqlite3
            ticker = params["ticker"].upper().strip()
            db = str(STOCKS_DIR / "stocks_vanguard.db")
            conn = sqlite3.connect(db)
            existing = conn.execute("SELECT ticker FROM favorites WHERE ticker=?", (ticker,)).fetchone()
            if existing:
                conn.close()
                return f"📊 {ticker} is already in your watchlist."
            conn.execute("INSERT INTO favorites (ticker) VALUES (?)", (ticker,))
            conn.commit()
            conn.close()
            return f"✅ Added *{ticker}* to your stock watchlist."

        elif name == "remove_stock":
            import sqlite3
            ticker = params["ticker"].upper().strip()
            db = str(STOCKS_DIR / "stocks_vanguard.db")
            conn = sqlite3.connect(db)
            deleted = conn.execute("DELETE FROM favorites WHERE ticker=?", (ticker,)).rowcount
            conn.commit()
            conn.close()
            if deleted:
                return f"✅ Removed *{ticker}* from your watchlist."
            return f"❌ {ticker} wasn't in your watchlist."

        elif name == "list_stocks":
            import sqlite3
            db = str(STOCKS_DIR / "stocks_vanguard.db")
            conn = sqlite3.connect(db)
            tickers = [r[0] for r in conn.execute("SELECT ticker FROM favorites ORDER BY ticker").fetchall()]
            conn.close()
            if tickers:
                return "📊 Your watchlist: " + ", ".join(tickers)
            return "📊 Your watchlist is empty."

        elif name == "search_jobs":
            subprocess.Popen(
                [PYTHON, str(BASE / "linkedin_apply.py"), "find"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return "🔍 Job search triggered — results coming to Jobs topic shortly."

        return f"Unknown tool: {name}"

    except Exception as e:
        return f"Tool error ({name}): {e}"


def ask_claude(user_message):
    """Send message to Claude with tool use support — agentic loop."""
    global conversation_history

    conversation_history.append({"role": "user", "content": user_message})
    if len(conversation_history) > 20:
        conversation_history = conversation_history[-20:]

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=conversation_history
        )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    print(f"[tool] {block.name}({block.input}) → {str(result)[:80]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result)
                    })
            conversation_history.append({"role": "assistant", "content": response.content})
            conversation_history.append({"role": "user", "content": tool_results})

        else:
            reply = next((b.text for b in response.content if hasattr(b, "text")), "Done.")
            conversation_history.append({"role": "assistant", "content": reply})
            return reply


def handle_command(text):
    """Handle special /commands."""
    # Strip @BotUsername suffix that Telegram appends in group chats (e.g. /network@JervissBot)
    cmd = text.lower().strip().split("@")[0]

    if cmd == "/start":
        return "👋 Hey Harshith! I'm Jerviss, your personal assistant. Ask me anything or use:\n\n/schedule — today's calendar\n/emails — check recent emails\n/help — show all commands"

    if cmd == "/schedule":
        summary = get_calendar_summary()
        from telegram_topics import send_daily
        send_daily(summary)
        return "📅 Calendar posted to Daily topic!"

    if cmd == "/emails":
        try:
            from email_bot import fetch_recent_emails
            emails = fetch_recent_emails(hours=1)
            if not emails:
                return "No new emails in the last hour."
            posted = post_and_index_emails(emails)
            return f"📧 Posted {posted} email(s) above — reply to any of them directly to send a reply."
        except Exception as e:
            return f"⚠️ Could not fetch emails: {e}"

    if cmd == "/stocks":
        try:
            subprocess.Popen(
                [PYTHON, str(STOCKS_DIR / "stock_alerts.py")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return "📊 Checking your stocks... results coming shortly!"
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/jobs":
        try:
            return get_job_summary()
        except Exception as e:
            return f"⚠️ Could not fetch job alerts: {e}"

    if cmd.startswith("/post"):
        topic = text[5:].strip()
        if not topic:
            return "Usage: `/post your topic here`\nExample: `/post lessons learned from AWS certification`"
        try:
            success, post_text = post_from_topic(topic)
            if success:
                return f"✅ *Posted to LinkedIn!*\n\n{post_text}"
            else:
                return "⚠️ Post failed — check if Share on LinkedIn product is approved in your developer app."
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/mystatus":
        try:
            return format_status_report()
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd.startswith("/jobstatus"):
        try:
            from job_tracker import get_job_status
            ref = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
            return get_job_status(ref)
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd.startswith("/update"):
        try:
            return handle_update_command(text)
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/findjobs":
        try:
            subprocess.Popen(
                [PYTHON, str(BASE / "linkedin_apply.py"), "find"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return "🔍 Searching LinkedIn for Easy Apply jobs matching your profile...\nResults will appear below — tap ✅ Apply or ❌ Skip on each one."
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/feed":
        try:
            subprocess.Popen(
                [PYTHON, str(BASE / "linkedin_feed.py")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return "📰 Scanning your LinkedIn feed for quality posts... results coming shortly!"
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/applyjobs":
        try:
            subprocess.Popen(
                [PYTHON, str(BASE / "linkedin_apply.py"), "apply"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return "🚀 Applying to all jobs you approved... will update you when done!"
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/autoapply":
        return (
            "🚫 *Disabled (2026-07-28).*\n"
            "This used to apply with no approval step and no per-job JD scoring — "
            "it's how fabricated resumes went out to real companies. "
            "Use `/findjobs` to search + review, then `/applyjobs` to submit what you approved."
        )

    if cmd == "/digest":
        try:
            subprocess.Popen(
                [PYTHON, str(BASE / "tech_digest.py")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return "📰 Fetching today's tech digest... posting to Daily shortly!"
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/login":
        try:
            log_file = open(BASE / "bot.log", "a")
            subprocess.Popen(
                [PYTHON, "-u", str(BASE / "linkedin_apply.py"), "login"],
                stdout=log_file, stderr=log_file
            )
            return "🔐 Opening LinkedIn login browser on your machine — check the browser window and complete any verification shown."
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/alerts":
        try:
            log_file = open(BASE / "bot.log", "a")
            subprocess.Popen(
                [PYTHON, "-u", str(BASE / "linkedin_alerts.py")],
                stdout=log_file, stderr=log_file
            )
            return "🔔 Fetching latest tech news, courses & updates... posting to Daily shortly!"
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/resumeupdate":
        try:
            log_file = open(BASE / "bot.log", "a")
            subprocess.Popen(
                [PYTHON, "-u", str(BASE / "resume_updater.py")],
                stdout=log_file, stderr=log_file
            )
            return "📄 Analyzing job descriptions against your resume... suggestions coming to Jobs topic shortly!"
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/network":
        try:
            log_file = open(BASE / "bot.log", "a")
            subprocess.Popen(
                [PYTHON, "-u", str(BASE / "linkedin_network.py")],
                stdout=log_file, stderr=log_file
            )
            return "🤝 Sending LinkedIn connection requests to HRs, recruiters & professionals... results coming shortly!"
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/netstatus":
        try:
            from linkedin_network import get_status_report
            return get_status_report()
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/analytics":
        try:
            from linkedin_analytics import get_analytics_report
            return get_analytics_report()
        except Exception as e:
            return f"⚠️ Error: {e}"

    if cmd == "/help":
        return ("🤖 *Available Commands:*\n\n"
                "*Job Search:*\n"
                "/autoapply — disabled (bypassed approval); use /findjobs + /applyjobs\n"
                "/findjobs — search jobs (manual approval mode)\n"
                "/applyjobs — apply to manually approved jobs\n"
                "/mystatus — view all applications + pipeline\n"
                "/jobstatus [id] — status of one job by its ref id (e.g. J012)\n"
                "/analytics — funnel, match quality, networking & feed stats\n"
                "/update [id] [stage] [note] — update application stage\n"
                "  Stages: applied phone\\_screen interview offer rejected\n\n"
                "*Networking:*\n"
                "/alerts — fetch latest tech news, courses & LinkedIn updates now\n"
                "/resumeupdate — analyze recent job descriptions and suggest resume improvements\n"
                "/network — send connection requests to HRs, recruiters & peers in Ireland\n"
                "/netstatus — show networking stats (total sent, by type, recent)\n"
                "/feed — scan LinkedIn feed, approve comments + connections\n\n"
                "*Learning:*\n"
                "/digest — fetch today's tech digest (AWS, .NET, Kafka, fintech)\n\n"
                "*Daily:*\n"
                "/schedule — today's calendar\n"
                "/emails — last 1hr email summary\n"
                "/jobs — LinkedIn job alerts from Gmail\n"
                "/stocks — check stock drops now\n"
                "/post [topic] — post to LinkedIn\n\n"
                "Or just chat with me normally!")

    # Unknown slash command — return error instead of falling through to Claude
    return f"❓ Unknown command `{cmd}`. Type /help to see all available commands."


_error_count       = 0
_last_error_alert  = 0.0
_ERROR_COOLDOWN    = 3600  # seconds between error alerts


def startup_job_catchup():
    """Resend any pending jobs discovered in the last 2 hours to the Jobs topic."""
    try:
        import sqlite3
        from datetime import timedelta
        from linkedin_apply import DB_PATH, send_job_for_approval, init_db
        from telegram_topics import send_jobs

        init_db()
        cutoff = (datetime.now() - timedelta(hours=2)).isoformat()
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, title, company, location, url FROM jobs "
            "WHERE status='pending' AND found_at >= ?",
            (cutoff,)
        ).fetchall()
        conn.close()

        if rows:
            send_jobs(f"🔄 *Startup Catch-up* — {len(rows)} job(s) found while you were away (last 2 hrs):")
            for r in rows:
                send_job_for_approval({"id": r[0], "title": r[1], "company": r[2], "location": r[3], "url": r[4]})
    except Exception as e:
        print(f"Startup catch-up error: {e}")


def _job_search_worker():
    """Background thread: trigger a LinkedIn job search every 45 minutes.

    Approval-gated: this only finds + scores + sends jobs to Telegram for
    you to tap Auto Apply / Skip. Nothing gets submitted until you run
    /applyjobs on the approved queue.
    """
    while True:
        time.sleep(45 * 60)
        try:
            subprocess.Popen(
                [PYTHON, str(BASE / "linkedin_apply.py"), "find"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print(f"[{datetime.now().strftime('%H:%M')}] Auto job search triggered (approval-gated).")
        except Exception as e:
            print(f"Job search worker error: {e}")


def _health_check_worker():
    """Background thread: send a health ping to Jobs topic every 6 hours."""
    while True:
        time.sleep(6 * 3600)
        try:
            from telegram_topics import send_jobs
            send_jobs(f"💚 *Bot Health Check* — Running fine at {datetime.now().strftime('%H:%M, %d %b')}")
        except Exception as e:
            print(f"Health check worker error: {e}")


def _tech_alerts_worker():
    """Background thread: run tech alerts every hour."""
    while True:
        time.sleep(3600)
        try:
            from linkedin_alerts import run_hourly
            run_hourly()
            print(f"[{datetime.now().strftime('%H:%M')}] Hourly tech alerts sent.")
        except Exception as e:
            print(f"Tech alerts worker error: {e}")


def _email_alert_worker():
    """Background thread: scan Gmail for priority emails (interview/offer/recruiter/rejection) every hour."""
    while True:
        time.sleep(3600)
        try:
            from smart_email_alert import check_priority_emails
            check_priority_emails()
            print(f"[{datetime.now().strftime('%H:%M')}] Hourly priority email scan done.")
        except Exception as e:
            print(f"Email alert worker error: {e}")


def post_and_index_emails(emails):
    """Post each email to the Emails topic as its own Telegram message and index it
    in EMAIL_INDEX, so any of them can be replied to directly. Shared by the hourly
    digest worker, /emails, and the fetch_emails tool, so replying works the same
    way no matter how the emails got surfaced. Returns how many were posted.
    """
    from telegram_topics import send_return_id
    posted = 0
    for e in emails:
        # parse_mode=None: raw email subjects/bodies aren't Markdown-safe
        # (stray * _ [ ] break Telegram's parser and silently drop the send)
        text = (
            f"📧 {e['subject']}\n"
            f"From: {e['from'][:80]}\n"
            f"To: {e['account']}\n"
            f"Date: {e['date']}\n\n"
            f"{e['body'][:1200]}\n\n"
            f"— Reply to this message to send a reply from {e['account']}."
        )
        tg_id = send_return_id(text, topic="emails", parse_mode=None)
        if tg_id:
            posted += 1
            EMAIL_INDEX[str(tg_id)] = {
                "account":     e["account"],
                "from_addr":   e["from"],
                "subject":     e["subject"],
                "body":        e["body"],
                "message_id":  e.get("message_id", ""),
                "references":  e.get("references", ""),
            }
    if posted:
        _save_email_index(EMAIL_INDEX)
    return posted


def _email_digest_worker():
    """Background thread: post every new email of the last hour to the Emails topic,
    one Telegram message each, so Harshith can reply to any of them directly.

    Runs alongside _email_alert_worker (which only pings on priority keyword matches) —
    this posts everything, priority or not.
    """
    while True:
        time.sleep(3600)
        try:
            from email_bot import fetch_recent_emails
            emails = fetch_recent_emails(hours=1)
            posted = post_and_index_emails(emails)
            print(f"[{datetime.now().strftime('%H:%M')}] Hourly email digest: {posted} email(s) posted.")
        except Exception as e:
            print(f"Email digest worker error: {e}")


def _linkedin_courses_worker():
    """Background thread: scan LinkedIn Learning for new courses every 6 hours."""
    import asyncio
    while True:
        time.sleep(6 * 3600)
        try:
            from linkedin_alerts import run_linkedin_courses
            asyncio.run(run_linkedin_courses())
            print(f"[{datetime.now().strftime('%H:%M')}] LinkedIn courses scan done.")
        except Exception as e:
            print(f"LinkedIn courses worker error: {e}")


def _resume_update_worker():
    """Background thread: run resume improvement analysis every Sunday at 9am."""
    import asyncio
    from datetime import timedelta
    while True:
        now = datetime.now()
        days_until_sunday = (6 - now.weekday()) % 7 or 7
        target = (now + timedelta(days=days_until_sunday)).replace(hour=9, minute=0, second=0, microsecond=0)
        time.sleep((target - now).total_seconds())
        try:
            from resume_updater import run_resume_update
            run_resume_update()
        except Exception as e:
            print(f"Resume update worker error: {e}")


def _networking_worker():
    """Background thread: run LinkedIn networking once per day at 10am."""
    import asyncio
    from datetime import timedelta
    while True:
        now = datetime.now()
        # Sleep until 10am today (or tomorrow if already past)
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        time.sleep((target - now).total_seconds())
        try:
            from linkedin_network import run_networking
            asyncio.run(run_networking())
            print(f"[{datetime.now().strftime('%H:%M')}] Daily networking run complete.")
        except Exception as e:
            print(f"Networking worker error: {e}")


def start_background_tasks():
    for worker in (
        _job_search_worker,
        _health_check_worker,
        _networking_worker,
        _resume_update_worker,
        _tech_alerts_worker,
        _linkedin_courses_worker,
        _email_alert_worker,
        _email_digest_worker,
    ):
        threading.Thread(target=worker, daemon=True).start()


def run():
    global _error_count, _last_error_alert

    print(f"[{datetime.now().strftime('%H:%M')}] Bot server started. Listening for messages...")
    startup_job_catchup()
    start_background_tasks()
    send_message("🤖 Jerviss is online! Type anything to chat, or use /help to see commands.")

    offset = None
    while True:
        try:
            updates = get_updates(offset)
            _error_count = 0  # Reset on successful poll
            for update in updates.get("result", []):
                offset = update["update_id"] + 1

                # Handle inline button taps
                if "callback_query" in update:
                    cb       = update.get("callback_query", {})
                    data     = cb.get("data", "")
                    cb_id    = cb.get("id")
                    try:
                        if data.startswith("qa_"):
                            # User tapped a years-of-experience button
                            answer   = data[3:]
                            qa_file  = Path.home() / ".apply_qa.json"
                            answered = False
                            if qa_file.exists():
                                try:
                                    qa = json.loads(qa_file.read_text())
                                    if qa.get("status") == "pending":
                                        label = qa.get("label", "field")
                                        qa["status"] = "answered"
                                        qa["answer"] = answer
                                        qa_file.write_text(json.dumps(qa))
                                        answered = True
                                        requests.post(
                                            f"{TELEGRAM_API}/answerCallbackQuery",
                                            json={"callback_query_id": cb_id, "text": f"✅ Got it — {answer} year(s)"}
                                        )
                                        send_message(
                                            f"✅ Got it! Filling `{label}` with: *{answer}*",
                                            src_chat_id=str(cb.get("message", {}).get("chat", {}).get("id", ""))
                                        )
                                except Exception as e:
                                    print(f"QA callback error: {e}")
                            if not answered:
                                requests.post(
                                    f"{TELEGRAM_API}/answerCallbackQuery",
                                    json={"callback_query_id": cb_id, "text": "No pending question right now."}
                                )
                        elif data.startswith(("fc_", "fco_", "fs_")):
                            from linkedin_feed import handle_feed_callback
                            handle_feed_callback(update)
                        elif data.startswith(("ru_yes_", "ru_no_")):
                            from resume_updater import handle_resume_callback
                            handle_resume_callback(update)
                        elif data.startswith(("em_send_", "em_cancel_")):
                            handle_email_callback(update)
                        else:
                            from linkedin_apply import handle_callback
                            handle_callback(update)
                    except Exception as e:
                        print(f"Callback error: {e}")
                    continue

                msg       = update.get("message", {})
                text      = msg.get("text", "").strip()
                thread_id = msg.get("message_thread_id")

                if not text or not is_authorized(msg):
                    continue

                src_chat_id = str(msg.get("chat", {}).get("id", ""))
                chat_title  = msg.get("chat", {}).get("title", "DM")
                print(f"[{datetime.now().strftime('%H:%M')}] chat={src_chat_id} ({chat_title}) thread={thread_id} | {text}")

                # Check for commands first
                if text.startswith("/"):
                    reply = handle_command(text)
                    if reply:
                        send_message(reply, thread_id=thread_id, src_chat_id=src_chat_id)
                        continue

                # Intercept a native Telegram "reply" to a fetched email -> draft + send an email reply
                _reply_to = msg.get("reply_to_message") or {}
                _reply_to_id = str(_reply_to.get("message_id", ""))
                if _reply_to_id:
                    print(f"[email-reply-check] reply_to_message_id={_reply_to_id} in_index={_reply_to_id in EMAIL_INDEX} known_ids={list(EMAIL_INDEX.keys())}")
                if _reply_to_id in EMAIL_INDEX and not text.startswith("/"):
                    try:
                        import uuid
                        from email_actions import polish_reply, DEFAULT_ACCOUNT
                        original = EMAIL_INDEX[_reply_to_id]
                        polished = polish_reply(original, text)
                        subject = original["subject"]
                        if not subject.lower().startswith("re:"):
                            subject = f"Re: {subject}"
                        draft_id = uuid.uuid4().hex[:10]
                        # References = prior chain + this message's own ID (RFC 2822 threading)
                        _references = f"{original.get('references', '')} {original.get('message_id', '')}".strip() or None
                        PENDING_EMAIL_DRAFTS[draft_id] = {
                            "from_account": original.get("account") or DEFAULT_ACCOUNT,
                            "to":           original.get("from_addr", ""),
                            "subject":      subject,
                            "body":         polished,
                            "in_reply_to":  original.get("message_id") or None,
                            "references":   _references,
                        }
                        _sent_ok = send_message(
                            f"✉️ Draft reply to {original.get('from_addr', '')}\n"
                            f"Subject: {subject}\n\n{polished}",
                            parse_mode=None, thread_id=thread_id, src_chat_id=src_chat_id,
                            reply_markup={"inline_keyboard": [[
                                {"text": "✅ Send",   "callback_data": f"em_send_{draft_id}"},
                                {"text": "❌ Cancel", "callback_data": f"em_cancel_{draft_id}"}
                            ]]}
                        )
                        print(f"[email-reply-check] draft_id={draft_id} send_message_ok={_sent_ok}")
                    except Exception as e:
                        print(f"[email-reply-check] EXCEPTION: {e!r}")
                        send_message(f"⚠️ Couldn't draft that reply: {e}", thread_id=thread_id, src_chat_id=src_chat_id)
                    continue

                # Intercept replies when linkedin_apply.py is waiting for a form field answer
                _qa_file = Path.home() / ".apply_qa.json"
                if _qa_file.exists() and not text.startswith("/"):
                    try:
                        _qa = json.loads(_qa_file.read_text())
                        _qa_status = _qa.get("status")
                        _qa_label  = _qa.get("label", "field")

                        if _qa_status == "pending":
                            # Subprocess is actively waiting — hand the answer over
                            _qa["status"] = "answered"
                            _qa["answer"] = text
                            _qa_file.write_text(json.dumps(_qa))
                            send_message(
                                f"✅ Got it! Filling `{_qa_label}` with: *{text}*",
                                thread_id=thread_id, src_chat_id=src_chat_id
                            )
                            continue

                        elif _qa_status == "waiting":
                            # Subprocess timed out but got a late reply — save to cache and retry
                            try:
                                from form_filler import _save_cached_answer
                                _save_cached_answer(_qa_label, text)
                            except Exception:
                                pass
                            try:
                                _qa_file.unlink(missing_ok=True)
                            except Exception:
                                pass
                            send_message(
                                f"✅ Got it! Saved your answer for `{_qa_label}`.\n"
                                f"🔄 Retrying the stalled application now...",
                                thread_id=thread_id, src_chat_id=src_chat_id
                            )
                            subprocess.Popen([PYTHON, str(BASE / "linkedin_apply.py"), "retry"])
                            continue
                    except Exception:
                        pass

                # Respond in any topic or DM
                try:
                    reply = ask_claude(text)
                    send_message(reply, thread_id=thread_id, src_chat_id=src_chat_id)
                except Exception as e:
                    send_message(f"⚠️ Error: {e}", thread_id=thread_id, src_chat_id=src_chat_id)

        except KeyboardInterrupt:
            print("Bot stopped.")
            break
        except Exception as e:
            _error_count += 1
            print(f"Error #{_error_count}: {e}")
            now = time.time()
            if _error_count >= 5 and (now - _last_error_alert) > _ERROR_COOLDOWN:
                try:
                    from telegram_topics import send_jobs
                    send_jobs(
                        f"⚠️ *Bot Issue Detected*\n"
                        f"{_error_count} consecutive errors. Last: `{e}`\n"
                        f"_{datetime.now().strftime('%H:%M, %d %b')}_"
                    )
                    _last_error_alert = now
                except Exception:
                    pass
            time.sleep(5)
            continue


if __name__ == "__main__":
    run()
