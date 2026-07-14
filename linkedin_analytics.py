#!/usr/bin/env python3
"""
LinkedIn Analytics — funnel & engagement reporting.

Reads the shared SQLite DB (`applied_jobs.db`) and turns the raw state captured
by the other modules into a scannable report:

  • Applications funnel  — from the `jobs` table (found → applied → response → offer)
  • Relevance scoring    — average score + distribution for applied jobs
  • Networking           — from the `connections` table (linkedin_network.py)
  • Feed engagement      — from the `feed_posts` table (linkedin_feed.py)
  • Top companies applied to

Every table is queried defensively — a table that a feature has not created
yet simply produces a "no data" line instead of an error.

Usage:
    from linkedin_analytics import get_analytics_report
    text = get_analytics_report()          # Markdown for Telegram

    python linkedin_analytics.py           # print to stdout
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "applied_jobs.db"

# Stages that mean a human on the other side responded / advanced the application.
ADVANCED_STAGES = ("phone_screen", "interview", "offer")


def _connect():
    # read-only; never create the DB just to report on it
    if not DB_PATH.exists():
        return None
    return sqlite3.connect(DB_PATH)


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _pct(part, whole):
    return f"{(100.0 * part / whole):.0f}%" if whole else "—"


# ── Sections ──────────────────────────────────────────────────────────────────

def _applications_section(conn):
    if not _table_exists(conn, "jobs"):
        return ["*💼 Applications:* no data yet"]

    total   = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    applied = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]
    skipped = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='skipped'"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='failed'"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','approved')"
    ).fetchone()[0]
    needs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('needs_answer','needs_manual')"
    ).fetchone()[0]

    placeholders = ",".join("?" * len(ADVANCED_STAGES))
    advanced = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE stage IN ({placeholders})", ADVANCED_STAGES
    ).fetchone()[0]
    offers = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE stage='offer'"
    ).fetchone()[0]
    rejected = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE stage='rejected'"
    ).fetchone()[0]

    lines = [
        "*💼 Applications Funnel*",
        f"• Seen: *{total}*  |  Applied: *{applied}* ({_pct(applied, total)})",
        f"• Skipped (low score): {skipped}  |  Failed: {failed}",
        f"• Awaiting approval: {pending}  |  Needs answer: {needs}",
        f"• Responses (screen+): *{advanced}* ({_pct(advanced, applied)} of applied)",
        f"• Offers: {offers}  |  Rejected: {rejected}",
    ]
    return lines


def _scoring_section(conn):
    if not _table_exists(conn, "jobs"):
        return []
    row = conn.execute(
        "SELECT AVG(relevance_score), MIN(relevance_score), MAX(relevance_score) "
        "FROM jobs WHERE relevance_score IS NOT NULL AND applied_at IS NOT NULL"
    ).fetchone()
    avg, lo, hi = row if row else (None, None, None)
    if avg is None:
        return []
    buckets = conn.execute(
        """SELECT
               SUM(CASE WHEN relevance_score >= 8 THEN 1 ELSE 0 END),
               SUM(CASE WHEN relevance_score BETWEEN 6 AND 7 THEN 1 ELSE 0 END),
               SUM(CASE WHEN relevance_score <= 5 THEN 1 ELSE 0 END)
           FROM jobs WHERE relevance_score IS NOT NULL AND applied_at IS NOT NULL"""
    ).fetchone()
    strong, mid, low = (b or 0 for b in buckets)
    return [
        "",
        "*🎯 Match Quality (applied jobs)*",
        f"• Avg score: *{avg:.1f}/10*  (range {lo}–{hi})",
        f"• Strong 8-10: {strong}  |  Mid 6-7: {mid}  |  Low ≤5: {low}",
    ]


def _networking_section(conn):
    if not _table_exists(conn, "connections"):
        return ["", "*🤝 Networking:* no data yet"]
    total = conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
    by_type = conn.execute(
        "SELECT note_type, COUNT(*) FROM connections GROUP BY note_type ORDER BY COUNT(*) DESC"
    ).fetchall()
    lines = ["", "*🤝 Networking*", f"• Requests sent: *{total}*"]
    if by_type:
        breakdown = "  |  ".join(f"{(t or 'other')}: {c}" for t, c in by_type)
        lines.append(f"• By type: {breakdown}")
    return lines


def _feed_section(conn):
    if not _table_exists(conn, "feed_posts"):
        return ["", "*📰 Feed:* no data yet"]
    total = conn.execute("SELECT COUNT(*) FROM feed_posts").fetchone()[0]
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM feed_posts GROUP BY status"
    ).fetchall())
    commented = by_status.get("commented", 0)
    connected = by_status.get("connected", 0)
    skipped   = by_status.get("skipped", 0)
    pending   = by_status.get("pending", 0)
    return [
        "",
        "*📰 Feed Engagement*",
        f"• Posts surfaced: *{total}*",
        f"• Commented: {commented}  |  Connected: {connected}",
        f"• Skipped: {skipped}  |  Pending approval: {pending}",
    ]


def _top_companies_section(conn):
    if not _table_exists(conn, "jobs"):
        return []
    rows = conn.execute(
        "SELECT company, COUNT(*) c FROM jobs WHERE applied_at IS NOT NULL "
        "GROUP BY company ORDER BY c DESC LIMIT 5"
    ).fetchall()
    if not rows:
        return []
    lines = ["", "*🏢 Top companies applied to*"]
    for company, c in rows:
        lines.append(f"• {company or 'Unknown'} — {c}")
    return lines


# ── Public API ────────────────────────────────────────────────────────────────

def get_analytics_report():
    """Return a Markdown analytics report for Telegram."""
    conn = _connect()
    if conn is None:
        return "📊 *LinkedIn Analytics*\n\nNo data yet — the database hasn't been created."
    try:
        lines = ["📊 *LinkedIn Analytics*", ""]
        lines += _applications_section(conn)
        lines += _scoring_section(conn)
        lines += _networking_section(conn)
        lines += _feed_section(conn)
        lines += _top_companies_section(conn)
        return "\n".join(lines)
    finally:
        conn.close()


def run():
    print(get_analytics_report())


if __name__ == "__main__":
    run()
