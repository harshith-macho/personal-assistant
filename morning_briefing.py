#!/usr/bin/env python3
"""
Morning Briefing Bot — runs at 8am PST daily.
Sends a full day overview: calendar events + overnight emails summary.
"""

import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta, timezone
import requests
import anthropic
from pathlib import Path
from dotenv import dotenv_values
from calendar_bot import get_calendar_summary

config = dotenv_values(Path.home() / ".env")

GMAIL_ADDRESS  = config.get("GMAIL_ADDRESS", "akshayreddy2022@gmail.com")
GMAIL_APP_PASS = config.get("GMAIL_APP_PASSWORD")
ANTHROPIC_KEY  = config.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = config.get("TELEGRAM_CHAT_ID")


def fetch_overnight_emails():
    """Fetch unread emails from the last 12 hours (overnight)."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
    mail.select("inbox")

    since_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
    _, msg_ids = mail.search(None, f'(SINCE "{since_date}" UNSEEN)')

    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    emails = []

    for mid in msg_ids[0].split():
        _, data = mail.fetch(mid, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])

        date_str = msg.get("Date", "")
        try:
            from email.utils import parsedate_to_datetime
            email_dt = parsedate_to_datetime(date_str)
            if email_dt.tzinfo is None:
                email_dt = email_dt.replace(tzinfo=timezone.utc)
            if email_dt < cutoff:
                continue
        except Exception:
            pass

        subject_raw, enc = decode_header(msg["Subject"] or "No Subject")[0]
        subject = subject_raw.decode(enc or "utf-8") if isinstance(subject_raw, bytes) else (subject_raw or "No Subject")
        sender = msg.get("From", "Unknown")

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                    body = part.get_payload(decode=True).decode(errors="ignore")
                    break
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")

        emails.append({
            "subject": subject,
            "from": sender,
            "date": date_str,
            "body": body[:1500]
        })

    mail.logout()
    return emails


def summarize_emails(emails):
    if not emails:
        return "No unread emails overnight."

    email_text = ""
    for i, e in enumerate(emails, 1):
        email_text += f"\n--- Email {i} ---\nFrom: {e['from']}\nSubject: {e['subject']}\n{e['body']}\n"

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""You are a personal morning assistant. Summarize these overnight emails briefly.
For each, mention who it's from, what it's about, and if any action is needed.
Keep it under 250 words. Be concise and friendly.

{email_text}"""
        }]
    )
    return message.content[0].text


def run():
    from telegram_topics import send_daily, send_emails
    now = datetime.now().strftime("%A, %B %d %Y")
    print(f"Sending morning briefing for {now}...")

    emails        = fetch_overnight_emails()
    email_summary = summarize_emails(emails)
    calendar      = get_calendar_summary()

    # Calendar events → Daily topic
    send_daily(f"☀️ *Good Morning — {now}*\n\n{calendar}")

    # Email summary → Emails topic
    send_emails(f"📬 *Overnight Emails* ({len(emails)} unread)\n\n{email_summary}")

    print("Morning briefing sent!")





if __name__ == "__main__":
    run()
