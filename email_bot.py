#!/usr/bin/env python3
"""
Personal Assistant Bot
Fetches Gmail emails + Google Calendar events, summarizes via Claude, sends to Telegram.
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

# Load credentials from ~/.env
config = dotenv_values(Path.home() / ".env")

ANTHROPIC_KEY   = config.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN  = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT   = config.get("TELEGRAM_CHAT_ID")

GMAIL_ACCOUNTS = [
    (config.get(f"GMAIL_ADDRESS_{i}"), config.get(f"GMAIL_APP_PASSWORD_{i}"))
    for i in range(1, 4)
    if config.get(f"GMAIL_ADDRESS_{i}") and config.get(f"GMAIL_APP_PASSWORD_{i}")
]


def fetch_recent_emails_from_account(address, app_password, hours=2):
    """Fetch emails from a single Gmail account received in the last N hours."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(address, app_password)
    mail.select("inbox")

    since_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
    _, msg_ids = mail.search(None, f'(SINCE "{since_date}")')

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
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
            "account": address,
            "subject": subject,
            "from": sender,
            "date": date_str,
            "body": body[:1500]
        })

    mail.logout()
    return emails


def fetch_recent_emails(hours=2):
    """Fetch emails from all configured Gmail accounts."""
    all_emails = []
    for address, app_password in GMAIL_ACCOUNTS:
        try:
            fetched = fetch_recent_emails_from_account(address, app_password, hours)
            all_emails.extend(fetched)
            print(f"  {address}: {len(fetched)} emails")
        except Exception as e:
            print(f"  {address}: failed — {e}")
    return all_emails


def summarize_with_claude(emails):
    """Send emails to Claude for summarization."""
    if not emails:
        return "No new emails in the last 2 hours."

    email_text = ""
    for i, e in enumerate(emails, 1):
        email_text += f"\n--- Email {i} ---\nAccount: {e.get('account', '')}\nFrom: {e['from']}\nSubject: {e['subject']}\nDate: {e['date']}\n{e['body']}\n"

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""You are a personal email assistant. Summarize the following emails clearly and concisely.
For each email, mention: who it's from, what it's about, and any action needed.
Keep the total summary under 300 words.

{email_text}"""
        }]
    )
    return message.content[0].text


def send_telegram(message):
    from telegram_topics import send_emails
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    ok = True
    for chunk in chunks:
        ok = send_emails(chunk, parse_mode=None) and ok
    return ok


def run():
    now = datetime.now().strftime("%b %d, %Y %I:%M %p")
    print(f"[{now}] Fetching emails...")

    emails = fetch_recent_emails(hours=2)
    print(f"Found {len(emails)} emails.")

    email_summary = summarize_with_claude(emails)

    print("Fetching calendar...")
    try:
        calendar_summary = get_calendar_summary()
    except Exception as e:
        calendar_summary = f"📅 Calendar unavailable: {e}"

    message = (
        f"📬 *Email Summary* — {now}\n\n{email_summary}"
        f"\n\n━━━━━━━━━━━━━━━\n\n{calendar_summary}"
    )

    if send_telegram(message):
        print("Summary sent to Telegram.")
    else:
        print("Failed to send to Telegram.")


if __name__ == "__main__":
    run()
