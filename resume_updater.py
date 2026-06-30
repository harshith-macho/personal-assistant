#!/usr/bin/env python3
"""
Resume Improvement Engine
- Reads saved job descriptions from the DB
- Uses Claude to spot skills/keywords missing from your resume
- Sends suggestions to Telegram with ✅/❌ approval buttons
- On approval, writes the suggestion into the correct resume section
"""

import anthropic
import sqlite3
import json
from pathlib import Path
from dotenv import dotenv_values
from resume_tailor import load_resume, save_resume, list_resumes

config        = dotenv_values(Path.home() / ".env")
ANTHROPIC_KEY = config.get("ANTHROPIC_API_KEY")
DB_PATH       = Path(__file__).parent / "applied_jobs.db"

SUGGESTION_TABLE = """
CREATE TABLE IF NOT EXISTS resume_suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_file TEXT,
    section     TEXT,
    suggestion  TEXT,
    reason      TEXT,
    status      TEXT DEFAULT 'pending',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


# ── DB helpers ────────────────────────────────────────────────────────────────

def _init_suggestions_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SUGGESTION_TABLE)
    conn.commit()
    conn.close()


def save_suggestion(resume_file, section, suggestion, reason) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO resume_suggestions (resume_file, section, suggestion, reason) VALUES (?,?,?,?)",
        (resume_file, section, suggestion, reason)
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_suggestion_status(suggestion_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE resume_suggestions SET status=? WHERE id=?",
        (status, suggestion_id)
    )
    conn.commit()
    conn.close()


def get_suggestion(suggestion_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, resume_file, section, suggestion, reason FROM resume_suggestions WHERE id=?",
        (suggestion_id,)
    ).fetchone()
    conn.close()
    if row:
        return {"id": row[0], "resume_file": row[1], "section": row[2],
                "suggestion": row[3], "reason": row[4]}
    return None


def fetch_recent_jds(limit=40) -> list[str]:
    """Pull the most recent job descriptions stored in the jobs table."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT description FROM jobs WHERE description IS NOT NULL AND description != '' "
            "ORDER BY found_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return [r[0] for r in rows]


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyze_and_suggest() -> list[dict]:
    """
    Read recent JDs + current resumes → ask Claude what's missing.
    Returns list of {resume_file, section, suggestion, reason}.
    """
    jds = fetch_recent_jds(limit=40)
    if not jds:
        return []

    resumes = list_resumes()
    if not resumes:
        return []

    combined_jds = "\n\n---\n\n".join(jds[:40])
    resume_text = "\n\n=====\n\n".join(
        f"[{name}]\n{text}" for name, text in resumes.items()
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = f"""You are a career coach analyzing job market trends.

Below are {len(jds)} recent job descriptions for roles in Dublin, Ireland that Harshith is applying to.
Also below are Harshith's current resumes.

Identify up to 8 specific, concrete improvements that would make his resume stronger for these roles.
Focus on:
- Skills or tools that appear frequently in JDs but are missing or underemphasized in the resume
- Phrasing or keywords recruiters are clearly looking for
- Sections that could be strengthened

JOB DESCRIPTIONS:
{combined_jds[:6000]}

CURRENT RESUMES:
{resume_text[:3000]}

Respond ONLY with a JSON array. Each item must have exactly these fields:
- "resume_file": one of {list(resumes.keys())} (which resume to update)
- "section": one of "PROFESSIONAL SUMMARY", "SKILLS", "EXPERIENCE", "PROJECTS"
- "suggestion": the exact line or bullet to add (concise, ready to paste in)
- "reason": one sentence explaining why (e.g. "Appears in 12 of 40 JDs")

Return raw JSON only, no markdown fences."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    try:
        suggestions = json.loads(raw)
        return suggestions if isinstance(suggestions, list) else []
    except Exception as e:
        print(f"Could not parse Claude suggestions: {e}\nRaw: {raw[:300]}")
        return []


# ── Apply suggestion to resume file ──────────────────────────────────────────

def apply_suggestion_to_resume(resume_file: str, section: str, suggestion: str) -> bool:
    """
    Append the suggestion line under the matching section header in the resume text file.
    Returns True on success.
    """
    try:
        content = load_resume(resume_file)
        lines   = content.splitlines()
        insert_at = None

        # Find the section header line
        for i, line in enumerate(lines):
            if line.strip().upper() == section.upper():
                insert_at = i + 1
                break

        if insert_at is None:
            # Section not found — append at end
            lines.append(f"\n{section}")
            lines.append(f"• {suggestion}")
        else:
            # Insert after the section header, before the next blank line or next section
            # Find the end of this section's content
            end = insert_at
            for j in range(insert_at, len(lines)):
                stripped = lines[j].strip()
                if stripped and stripped.isupper() and stripped != section.upper():
                    break
                end = j + 1
            # Insert at end of section
            lines.insert(end, f"• {suggestion}" if not suggestion.startswith("•") else suggestion)

        save_resume(resume_file, "\n".join(lines))
        return True
    except Exception as e:
        print(f"Could not apply suggestion: {e}")
        return False


# ── Main: run analysis and send to Telegram ───────────────────────────────────

def run_resume_update():
    from telegram_topics import send_jobs

    _init_suggestions_db()

    jd_count = len(fetch_recent_jds(limit=40))
    if jd_count < 5:
        send_jobs(
            f"📄 *Resume Update*\nNot enough job description data yet ({jd_count} collected). "
            f"Run `/autoapply` a few times first to build up JD history."
        )
        return

    send_jobs(f"📄 *Resume Update*\nAnalyzing {jd_count} job descriptions against your resume...")

    suggestions = analyze_and_suggest()
    if not suggestions:
        send_jobs("📄 *Resume Update*\nNo new improvements found — your resume looks well-matched to current JDs!")
        return

    for s in suggestions:
        resume_file = s.get("resume_file", "resume_devops")
        section     = s.get("section", "SKILLS")
        suggestion  = s.get("suggestion", "")
        reason      = s.get("reason", "")

        if not suggestion:
            continue

        row_id = save_suggestion(resume_file, section, suggestion, reason)

        markup = {"inline_keyboard": [[
            {"text": "✅ Add to resume", "callback_data": f"ru_yes_{row_id}"},
            {"text": "❌ Skip",          "callback_data": f"ru_no_{row_id}"},
        ]]}

        send_jobs(
            f"📄 *Resume Suggestion* — `{resume_file}`\n"
            f"*Section:* {section}\n"
            f"*Add:* `{suggestion}`\n"
            f"_{reason}_",
            reply_markup=markup
        )


def handle_resume_callback(update: dict):
    """Handle ✅/❌ button taps for resume suggestions."""
    from telegram_topics import send_jobs

    callback = update.get("callback_query", {})
    data     = callback.get("data", "")

    if data.startswith("ru_yes_"):
        suggestion_id = int(data[7:])
        s = get_suggestion(suggestion_id)
        if not s:
            return
        success = apply_suggestion_to_resume(s["resume_file"], s["section"], s["suggestion"])
        update_suggestion_status(suggestion_id, "applied" if success else "failed")
        if success:
            send_jobs(
                f"✅ *Added to `{s['resume_file']}`*\n"
                f"Section: {s['section']}\n"
                f"`{s['suggestion']}`"
            )
        else:
            send_jobs(f"⚠️ Could not update resume file — check `resumes/{s['resume_file']}.txt` manually.")

    elif data.startswith("ru_no_"):
        suggestion_id = int(data[6:])
        update_suggestion_status(suggestion_id, "skipped")


if __name__ == "__main__":
    run_resume_update()
