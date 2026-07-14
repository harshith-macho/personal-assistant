#!/usr/bin/env python3
"""
LinkedIn Job Alert Monitor
Fetches LinkedIn job alert emails from Gmail, summarizes via Claude, sends to Telegram.
"""

import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
import requests
import anthropic
import re
from pathlib import Path
from dotenv import dotenv_values

from form_filler import PROFILE  # single source of truth for identity

config = dotenv_values(Path.home() / ".env")

GMAIL_ADDRESS  = config.get("GMAIL_ADDRESS")
GMAIL_APP_PASS = config.get("GMAIL_APP_PASSWORD")
ANTHROPIC_KEY  = config.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = config.get("TELEGRAM_CHAT_ID")


def fetch_linkedin_job_emails(hours=24):
    """Fetch LinkedIn job alert emails from the last N hours."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
    mail.select("inbox")

    since_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%d-%b-%Y")
    _, msg_ids = mail.search(None, f'(FROM "linkedin" SINCE "{since_date}")')

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    jobs = []

    for mid in msg_ids[0].split():
        _, data = mail.fetch(mid, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])

        # Filter by time
        date_str = msg.get("Date", "")
        try:
            email_dt = parsedate_to_datetime(date_str)
            if email_dt.tzinfo is None:
                email_dt = email_dt.replace(tzinfo=timezone.utc)
            if email_dt < cutoff:
                continue
        except Exception:
            pass

        subject_raw, enc = decode_header(msg["Subject"] or "")[0]
        subject = subject_raw.decode(enc or "utf-8") if isinstance(subject_raw, bytes) else (subject_raw or "")

        # Only job alert emails
        job_keywords = ["developer", "engineer", "architect", "devops", "aws", "python",
                        "software", ".net", "full stack", "backend", "cloud", "job"]
        if not any(k in subject.lower() for k in job_keywords):
            continue

        # Extract body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode(errors="ignore")
                    break
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")

        jobs.append({
            "subject": subject,
            "date": date_str,
            "body": body[:2000]
        })

    mail.logout()
    return jobs


def summarize_jobs(jobs):
    """Summarize job alerts via Claude."""
    if not jobs:
        return "No new job alerts in the last 24 hours."

    job_text = ""
    for i, j in enumerate(jobs, 1):
        job_text += f"\n--- Job {i} ---\nSubject: {j['subject']}\n{j['body']}\n"

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""You are a job search assistant for {PROFILE['first_name']}, a software developer.
Extract and summarize the job opportunities from these LinkedIn alert emails.
For each job mention: Job title, Company, Location, Salary (if mentioned), and a one-line summary.
Group similar roles together. Highlight any remote or high-paying roles.
Keep it concise and scannable for Telegram.

{job_text}"""
        }]
    )
    return message.content[0].text


def send_telegram(message):
    from telegram_topics import send_jobs
    return send_jobs(message)


def get_job_summary():
    """Used by bot_server for /jobs command."""
    jobs = fetch_linkedin_job_emails(hours=24)
    return summarize_jobs(jobs)


def run():
    now = datetime.now().strftime("%b %d, %Y %I:%M %p")
    print(f"[{now}] Fetching LinkedIn job alerts...")

    jobs = fetch_linkedin_job_emails(hours=24)
    print(f"Found {len(jobs)} job alerts.")

    summary = summarize_jobs(jobs)
    message = f"💼 *LinkedIn Job Alerts* — {now}\n\n{summary}"

    if send_telegram(message):
        print("Job alerts sent to Telegram.")
    else:
        print("Failed to send.")


if __name__ == "__main__":
    run()
