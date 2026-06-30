#!/usr/bin/env python3
"""
Smart Email Alerts
- Runs every 15 minutes via cron
- Instantly notifies on recruiter/interview/offer emails
- Skips already-seen emails using a local cache
"""

import imaplib
import email
import json
import requests
import anthropic
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import dotenv_values

config         = dotenv_values(Path.home() / ".env")
TELEGRAM_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = config.get("TELEGRAM_CHAT_ID")
ANTHROPIC_KEY  = config.get("ANTHROPIC_API_KEY")

GMAIL_ACCOUNTS = [
    (config.get(f"GMAIL_ADDRESS_{i}"), config.get(f"GMAIL_APP_PASSWORD_{i}"))
    for i in range(1, 4)
    if config.get(f"GMAIL_ADDRESS_{i}") and config.get(f"GMAIL_APP_PASSWORD_{i}")
]

SEEN_FILE = Path(__file__).parent / "seen_emails.json"

# Keywords that trigger an instant alert
PRIORITY_KEYWORDS = [
    "interview", "phone screen", "phone call", "video call",
    "hiring manager", "recruiter", "recruiting", "talent acquisition",
    "job offer", "offer letter", "congratulations", "next steps",
    "schedule a call", "schedule a meeting", "background check",
    "onboarding", "start date", "we'd like to", "we would like to",
    "move forward", "moving forward", "position you applied",
    "application status", "your application", "rejection", "not moving forward",
    "unfortunately", "other candidates",
]


def send_telegram(text):
    from telegram_topics import send_emails
    send_emails(text)


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    # Keep only last 1000 IDs to avoid bloat
    ids = list(seen)[-1000:]
    SEEN_FILE.write_text(json.dumps(ids))


def is_priority(subject, body):
    text = (subject + " " + body).lower()
    return any(kw in text for kw in PRIORITY_KEYWORDS)


def classify_with_claude(subject, sender, snippet):
    """Ask Claude to classify the email urgency and write a 1-line summary."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": f"""Classify this email for a job seeker. Reply with JSON only:
{{"urgent": true/false, "type": "interview_invite|offer|rejection|recruiter_outreach|application_update|other", "summary": "one line summary"}}

From: {sender}
Subject: {subject}
Body preview: {snippet[:400]}"""}]
    )
    try:
        import re
        match = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
        return json.loads(match.group()) if match else {"urgent": False, "type": "other", "summary": subject}
    except Exception:
        return {"urgent": False, "type": "other", "summary": subject}


def check_account(address, app_password, seen, new_seen, alerts):
    """Check one Gmail account for priority emails."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(address, app_password)
        mail.select("inbox")

        since = (datetime.now() - timedelta(hours=1)).strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE {since} UNSEEN)')

        ids = data[0].split() if data[0] else []
        for eid in ids[-30:]:
            try:
                _, msg_data = mail.fetch(eid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                msg_id  = msg.get("Message-ID", str(eid))
                subject = msg.get("Subject", "(no subject)")
                sender  = msg.get("From", "")

                if msg_id in seen:
                    continue
                new_seen.add(msg_id)

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            try:
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                break
                            except Exception:
                                pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                    except Exception:
                        pass

                try:
                    date_str = msg.get("Date", "")
                    msg_dt = parsedate_to_datetime(date_str)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
                    if msg_dt < cutoff:
                        continue
                except Exception:
                    pass

                if is_priority(subject, body):
                    result = classify_with_claude(subject, sender, body)
                    alerts.append((result, subject, sender, address))

            except Exception:
                continue

        mail.logout()
    except Exception as e:
        print(f"IMAP error ({address}): {e}")


def check_priority_emails():
    seen = load_seen()
    new_seen = set()
    alerts = []

    for address, app_password in GMAIL_ACCOUNTS:
        check_account(address, app_password, seen, new_seen, alerts)

    seen.update(new_seen)
    save_seen(seen)

    TYPE_EMOJI = {
        "interview_invite":    "🎯",
        "offer":               "🎉",
        "rejection":           "❌",
        "recruiter_outreach":  "👋",
        "application_update":  "📋",
        "other":               "📧",
    }

    for result, subject, sender, account in alerts:
        emoji = TYPE_EMOJI.get(result.get("type", "other"), "📧")
        urgency = "🚨 *URGENT* " if result.get("urgent") else ""
        msg = (f"{urgency}{emoji} *{result.get('type', 'Email').replace('_', ' ').title()}*\n\n"
               f"*To:* {account}\n"
               f"*From:* {sender[:50]}\n"
               f"*Subject:* {subject[:80]}\n\n"
               f"_{result.get('summary', '')}_")
        send_telegram(msg)
        print(f"Alert sent: {subject[:50]}")

    if not alerts:
        print(f"[{datetime.now().strftime('%H:%M')}] No priority emails found.")


if __name__ == "__main__":
    check_priority_emails()
