#!/usr/bin/env python3
"""
Outbound email actions: SMTP sending + Claude-drafted reply text.
Reuses the same Gmail App Password credentials email_bot.py already uses for IMAP.
"""

import smtplib
from email.message import EmailMessage
from pathlib import Path
import anthropic
from dotenv import dotenv_values
from email_bot import GMAIL_ACCOUNTS
from form_filler import PROFILE

config        = dotenv_values(Path.home() / ".env")
ANTHROPIC_KEY = config.get("ANTHROPIC_API_KEY")

ACCOUNTS        = dict(GMAIL_ACCOUNTS)  # address -> app password
DEFAULT_ACCOUNT = GMAIL_ACCOUNTS[0][0] if GMAIL_ACCOUNTS else None


def send_via_smtp(from_address, to_address, subject, body, in_reply_to=None, references=None):
    """Send a plain-text email via Gmail SMTP using the same App Password as IMAP."""
    app_password = ACCOUNTS.get(from_address)
    if not app_password:
        raise ValueError(f"No app password configured for {from_address}")

    msg = EmailMessage()
    msg["From"]    = from_address
    msg["To"]      = to_address
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_address, app_password)
        smtp.send_message(msg)


def polish_reply(original, rough_text):
    """Turn Harshith's rough reply text into a proper email body continuing the thread."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = f"""You are drafting an email reply on behalf of {PROFILE['full_name']}.

Original email:
From: {original.get('from_addr', '')}
Subject: {original.get('subject', '')}
{original.get('body', '')[:800]}

{PROFILE['first_name']}'s rough reply (may be shorthand/notes — keep any specific facts, dates, or numbers exactly as given, don't invent details):
"{rough_text}"

Write a clear, professional but not stiff email reply body based on this. Sign off as {PROFILE['first_name']}.
Reply with ONLY the email body text — no subject line, no commentary, no quotation marks around it."""
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()
