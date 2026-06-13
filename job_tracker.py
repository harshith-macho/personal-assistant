#!/usr/bin/env python3
"""
Job Application Tracker
- Tracks all applied jobs with stages: applied → phone_screen → interview → offer → rejected
- Daily 6pm summary to Telegram
- Commands: /mystatus, /update
"""

import sqlite3
import requests
from datetime import datetime
from pathlib import Path
from dotenv import dotenv_values

config = dotenv_values(Path.home() / ".env")
TELEGRAM_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = config.get("TELEGRAM_CHAT_ID")
DB_PATH        = Path(__file__).parent / "applied_jobs.db"

STAGES = ["applied", "phone_screen", "interview", "offer", "rejected", "withdrawn"]

STAGE_EMOJI = {
    "applied":      "📤",
    "phone_screen": "📞",
    "interview":    "🎯",
    "offer":        "🎉",
    "rejected":     "❌",
    "withdrawn":    "🚫",
    "pending":      "⏳",
    "skipped":      "⏭️",
}


def send_telegram(text, parse_mode="Markdown"):
    from telegram_topics import send_jobs
    send_jobs(text)


def get_all_applications():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, title, company, location, url, status, stage, notes, applied_at
        FROM jobs
        WHERE status IN ('applied', 'phone_screen', 'interview', 'offer', 'rejected', 'withdrawn')
        ORDER BY applied_at DESC
    """).fetchall()
    conn.close()
    return rows


def update_stage(job_id, new_stage, note=None):
    conn = sqlite3.connect(DB_PATH)
    if note:
        conn.execute(
            "UPDATE jobs SET stage=?, status=?, notes=? WHERE id=?",
            (new_stage, new_stage, note, job_id)
        )
    else:
        conn.execute(
            "UPDATE jobs SET stage=?, status=? WHERE id=?",
            (new_stage, new_stage, job_id)
        )
    conn.commit()
    conn.close()


def get_stage_counts():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT stage, COUNT(*) FROM jobs
        WHERE status NOT IN ('pending', 'skipped', 'approved', 'failed')
        GROUP BY stage
    """).fetchall()
    conn.close()
    return dict(rows)


def get_active_applications():
    """Jobs in active stages (not rejected/withdrawn)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, title, company, stage, applied_at
        FROM jobs
        WHERE status NOT IN ('pending', 'skipped', 'approved', 'rejected', 'withdrawn', 'failed')
        ORDER BY applied_at DESC
    """).fetchall()
    conn.close()
    return rows


def format_status_report():
    counts = get_stage_counts()
    active = get_active_applications()

    total = sum(counts.values())
    if total == 0:
        return "📋 *Application Tracker*\n\nNo applications yet. Use /findjobs to find jobs!"

    lines = ["📋 *Application Tracker*\n"]

    # Summary counts
    lines.append("*Summary:*")
    for stage in STAGES:
        count = counts.get(stage, 0)
        if count:
            emoji = STAGE_EMOJI.get(stage, "•")
            lines.append(f"{emoji} {stage.replace('_', ' ').title()}: {count}")
    lines.append(f"\n*Total applied: {total}*\n")

    # Active pipeline
    if active:
        lines.append("*Active Pipeline:*")
        for job in active[:10]:
            job_id, title, company, stage, applied_at = job
            emoji = STAGE_EMOJI.get(stage, "📤")
            date = applied_at[:10] if applied_at else "?"
            lines.append(f"{emoji} `{job_id[-6:]}` {title[:30]} @ {company[:20]} ({date})")

    return "\n".join(lines)


def send_daily_summary():
    report = format_status_report()
    counts = get_stage_counts()
    interviews = counts.get("interview", 0)
    offers = counts.get("offer", 0)

    if interviews or offers:
        report += f"\n\n🔥 *Action needed:* {interviews} interview(s), {offers} offer(s) pending!"

    send_telegram(report)


def handle_update_command(text):
    """
    Parse: /update <job_id_suffix> <stage> [note]
    Example: /update abc123 interview Great call with recruiter
    """
    parts = text.strip().split(None, 3)
    if len(parts) < 3:
        return ("Usage: `/update <job_id> <stage> [note]`\n\n"
                "Stages: `applied` `phone_screen` `interview` `offer` `rejected` `withdrawn`\n\n"
                "Example: `/update 633753 interview had a great screening call`")

    job_suffix = parts[1].lower()
    new_stage  = parts[2].lower().replace(" ", "_")
    note       = parts[3] if len(parts) > 3 else None

    if new_stage not in STAGES:
        return f"❌ Unknown stage `{new_stage}`.\nValid: {', '.join(STAGES)}"

    # Find job by partial ID match
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, company FROM jobs WHERE id LIKE ?",
        (f"%{job_suffix}%",)
    ).fetchall()
    conn.close()

    if not rows:
        return f"❌ No job found matching `{job_suffix}`"
    if len(rows) > 1:
        matches = "\n".join([f"`{r[0][-8:]}` — {r[1]} @ {r[2]}" for r in rows[:5]])
        return f"Multiple matches — be more specific:\n{matches}"

    job_id, title, company = rows[0]
    update_stage(job_id, new_stage, note)
    emoji = STAGE_EMOJI.get(new_stage, "•")
    msg = f"{emoji} Updated *{title}* @ {company}\nStage: `{new_stage}`"
    if note:
        msg += f"\nNote: _{note}_"
    return msg


if __name__ == "__main__":
    send_daily_summary()
