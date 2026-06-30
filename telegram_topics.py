#!/usr/bin/env python3
"""Telegram group + topic routing helper used by all assistant scripts."""

import requests
from pathlib import Path
from dotenv import dotenv_values

config = dotenv_values(Path.home() / ".env")

TOKEN    = config.get("TELEGRAM_BOT_TOKEN")
GROUP_ID = config.get("TELEGRAM_GROUP_ID") or None
CHAT_ID  = config.get("TELEGRAM_CHAT_ID")  or None

TOPICS = {
    "chat":   int(config.get("TELEGRAM_TOPIC_CHAT",   0) or 0),
    "emails": int(config.get("TELEGRAM_TOPIC_EMAILS", 0) or 0),
    "jobs":   int(config.get("TELEGRAM_TOPIC_JOBS",   0) or 0),
    "stocks": int(config.get("TELEGRAM_TOPIC_STOCKS", 0) or 0),
    "daily":  int(config.get("TELEGRAM_TOPIC_DAILY",  0) or 0),
}

# Use personal chat when group is not configured
_TARGET = GROUP_ID or CHAT_ID


def send(text, topic="chat", parse_mode="Markdown", reply_markup=None):
    """Send a message to a group topic, or personal chat if no group is set."""
    payload = {
        "chat_id":    _TARGET,
        "text":       text,
        "parse_mode": parse_mode,
    }
    thread_id = TOPICS.get(topic, 0) if GROUP_ID else 0
    if thread_id:
        payload["message_thread_id"] = thread_id
    if reply_markup:
        import json
        payload["reply_markup"] = json.dumps(reply_markup)
    if payload.get("parse_mode") is None:
        payload.pop("parse_mode", None)
    resp = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload)
    if not resp.ok:
        print(f"Telegram error: {resp.status_code} {resp.text}")
    return resp.ok


def send_chat(text, **kwargs):   return send(text, "chat",   **kwargs)
def send_emails(text, **kwargs): return send(text, "emails", **kwargs)
def send_jobs(text, **kwargs):   return send(text, "jobs",   **kwargs)
def send_stocks(text, **kwargs): return send(text, "stocks", **kwargs)
def send_daily(text, **kwargs):  return send(text, "daily",  **kwargs)
