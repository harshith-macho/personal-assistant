#!/usr/bin/env python3
"""
LinkedIn Networking Bot
- Searches for HRs, recruiters, hiring managers, and peers in Ireland
- Sends personalized connection requests with notes
- Tracks sent requests in DB to avoid duplicates
- Daily limit: 15 requests (stays under LinkedIn's weekly cap)
"""

import asyncio
import json
import sqlite3
import anthropic as _anthropic
from datetime import datetime, date
from pathlib import Path
from dotenv import dotenv_values

config = dotenv_values(Path.home() / ".env")

DB_PATH       = Path(__file__).parent / "applied_jobs.db"
SESSION_FILE  = Path(__file__).parent / "linkedin_session.json"
ANTHROPIC_KEY = config.get("ANTHROPIC_API_KEY")

DAILY_LIMIT = 15  # LinkedIn flags accounts sending 20+/day

# Who to search for and how to message them
SEARCH_PROFILES = [
    ("recruiter software engineer Ireland",        "recruiter"),
    ("technical recruiter AWS cloud Dublin",       "recruiter"),
    ("IT recruiter technology Dublin Ireland",     "recruiter"),
    ("HR manager technology company Dublin",       "hr"),
    ("talent acquisition software Ireland",        "hr"),
    ("hiring manager software engineering Dublin", "hiring_manager"),
    ("DevOps engineer Dublin Ireland",             "peer"),
    ("cloud engineer AWS Dublin Ireland",          "peer"),
    ("software engineer .NET Dublin Ireland",      "peer"),
]

_ROLE_CONTEXT = {
    "recruiter":      "a recruiter or talent acquisition professional",
    "hr":             "an HR or people manager",
    "hiring_manager": "a hiring manager or engineering lead",
    "peer":           "a fellow software developer or engineer",
}


def _generate_connection_note(first_name: str, their_title: str, their_company: str, note_type: str) -> str:
    """Use Claude to write a personalised LinkedIn connection note under 280 chars."""
    role_context = _ROLE_CONTEXT.get(note_type, "a professional")
    try:
        client = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a LinkedIn connection note under 280 characters. "
                    f"From: Harshith, a software developer in Dublin, 2 years experience, "
                    f"stack: .NET Core, Python, AWS, Docker, DevOps, open to roles across Ireland. "
                    f"To: {first_name}, {role_context} at {their_company} (title: {their_title}). "
                    f"Be brief, genuine, no buzzwords, no hashtags, no flattery. "
                    f"Mention one relevant thing about their role or company if it makes sense. "
                    f"End naturally — don't say 'Would love to connect' as the only sentence."
                ),
            }],
        )
        note = msg.content[0].text.strip()
        return note[:280]
    except Exception as e:
        print(f"  Note generation error: {e}")
        # Fallback to a simple template
        fallbacks = {
            "recruiter":      f"Hi {first_name}, I'm a software developer in Dublin with AWS, .NET, and DevOps experience, actively looking for roles in Ireland. Happy to connect!",
            "hr":             f"Hi {first_name}, I'm a Dublin-based software developer exploring cloud and DevOps opportunities across Ireland. Would be great to connect.",
            "hiring_manager": f"Hi {first_name}, I'm a software developer in Dublin with AWS and .NET experience. Keen to connect and learn more about your team.",
            "peer":           f"Hi {first_name}, fellow developer in Dublin here — I work with AWS, .NET, and DevOps. Would be great to connect and share notes!",
        }
        return fallbacks.get(note_type, f"Hi {first_name}, I'm a software developer in Dublin. Would love to connect!")[:280]


# ── Database ──────────────────────────────────────────────────────────────────

def init_connections_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS connections (
            profile_url  TEXT PRIMARY KEY,
            name         TEXT,
            title        TEXT,
            company      TEXT,
            note_type    TEXT,
            status       TEXT DEFAULT 'sent',
            sent_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def already_connected(profile_url):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT profile_url FROM connections WHERE profile_url=?", (profile_url,)
    ).fetchone()
    conn.close()
    return row is not None


def save_connection(profile_url, name, title, company, note_type):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO connections (profile_url, name, title, company, note_type) VALUES (?,?,?,?,?)",
        (profile_url, name, title, company, note_type)
    )
    conn.commit()
    conn.close()


def get_sent_today():
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM connections WHERE DATE(sent_at)=?", (str(date.today()),)
    ).fetchone()[0]
    conn.close()
    return count


def get_connection_stats():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM connections WHERE DATE(sent_at)=?", (str(date.today()),)
    ).fetchone()[0]
    by_type = conn.execute(
        "SELECT note_type, COUNT(*) FROM connections GROUP BY note_type"
    ).fetchall()
    recent = conn.execute(
        "SELECT name, title, company, sent_at FROM connections ORDER BY sent_at DESC LIMIT 5"
    ).fetchall()
    conn.close()
    return total, today, dict(by_type), recent


# ── Session ───────────────────────────────────────────────────────────────────

async def load_session(context):
    if SESSION_FILE.exists():
        data = json.loads(SESSION_FILE.read_text())
        cookies = data["cookies"] if isinstance(data, dict) and "cookies" in data else data
        await context.add_cookies(cookies)
        return True
    return False


# ── Core networking ───────────────────────────────────────────────────────────

async def search_people(page, keywords, max_results=10):
    """Search LinkedIn people and return list of {name, title, company, profile_url}."""
    encoded = keywords.replace(" ", "%20")
    url = (
        f"https://www.linkedin.com/search/results/people/?"
        f"keywords={encoded}"
        f"&network=%5B%22S%22%2C%22O%22%5D"  # 2nd and 3rd degree only (not already connected)
        f"&origin=GLOBAL_SEARCH_HEADER"
    )
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # Check session still valid
    if "login" in page.url or "authwall" in page.url or "checkpoint" in page.url:
        raise RuntimeError("LinkedIn session expired during people search")

    people = []
    cards = await page.query_selector_all(".reusable-search__result-container")

    for card in cards[:max_results]:
        try:
            name_el    = await card.query_selector(".entity-result__title-text")
            title_el   = await card.query_selector(".entity-result__primary-subtitle")
            company_el = await card.query_selector(".entity-result__secondary-subtitle")
            link_el    = await card.query_selector("a.app-aware-link")

            if not name_el or not link_el:
                continue

            name        = (await name_el.inner_text()).strip().split("\n")[0]
            title       = (await title_el.inner_text()).strip() if title_el else ""
            company     = (await company_el.inner_text()).strip() if company_el else ""
            profile_url = (await link_el.get_attribute("href") or "").split("?")[0]

            if not profile_url or not name or name.lower() == "linkedin member":
                continue

            people.append({
                "name":        name,
                "title":       title,
                "company":     company,
                "profile_url": profile_url,
            })
        except Exception:
            continue

    return people


async def send_connection_request(page, person, note_type):
    """
    Navigate to the person's profile and send a connection request with a note.
    Returns True on success, False if already connected / button not found.
    """
    await page.goto(person["profile_url"], wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    first_name = person["name"].split()[0]
    note_text  = _generate_connection_note(first_name, person.get("title", ""), person.get("company", ""), note_type)

    # Look for Connect button on profile (may be inside a More dropdown)
    connect_btn = await page.query_selector("button[aria-label='Connect']")

    if not connect_btn:
        # Try the "More" dropdown
        more_btn = await page.query_selector("button[aria-label='More actions']")
        if more_btn:
            await more_btn.click()
            await page.wait_for_timeout(1000)
            connect_btn = await page.query_selector("div[aria-label='Connect']") or \
                          await page.query_selector("span:text('Connect')")

    if not connect_btn:
        print(f"  No Connect button for {person['name']} — already connected or Premium-only")
        return False

    await connect_btn.click()
    await page.wait_for_timeout(1500)

    # Click "Add a note" in the modal
    note_btn = (
        await page.query_selector("button[aria-label='Add a note']") or
        await page.query_selector("button:text('Add a note')")
    )
    if note_btn:
        await note_btn.click()
        await page.wait_for_timeout(1000)

        note_field = await page.query_selector("textarea#custom-message")
        if note_field:
            await note_field.fill(note_text[:300])
            await page.wait_for_timeout(500)

    # Send
    send_btn = (
        await page.query_selector("button[aria-label='Send now']") or
        await page.query_selector("button:text('Send')")
    )
    if send_btn:
        await send_btn.click()
        await page.wait_for_timeout(1000)
        print(f"  ✅ Request sent to {person['name']} ({person['title']} @ {person['company']})")
        return True

    # Close modal if send failed
    close = await page.query_selector("button[aria-label='Dismiss']")
    if close:
        await close.click()

    return False


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_networking(daily_limit=DAILY_LIMIT):
    """Search for professionals and send connection requests up to daily_limit."""
    from telegram_topics import send_jobs

    init_connections_db()

    sent_today = get_sent_today()
    if sent_today >= daily_limit:
        send_jobs(
            f"🤝 *Networking*\nAlready sent {sent_today} requests today (limit: {daily_limit}). "
            f"Will resume tomorrow."
        )
        return

    remaining = daily_limit - sent_today

    from playwright.async_api import async_playwright
    from linkedin_apply import _get_authenticated_browser
    async with async_playwright() as p:
        browser, context, page = await _get_authenticated_browser(p)

        sent       = 0
        skipped    = 0
        sent_names = []

        try:
            for keywords, note_type in SEARCH_PROFILES:
                if sent >= remaining:
                    break

                print(f"Searching: {keywords}")
                try:
                    people = await search_people(page, keywords, max_results=8)
                except RuntimeError as e:
                    send_jobs(f"⚠️ *Networking stopped*: {e}")
                    break

                for person in people:
                    if sent >= remaining:
                        break
                    if already_connected(person["profile_url"]):
                        continue

                    try:
                        success = await send_connection_request(page, person, note_type)
                        if success:
                            save_connection(
                                person["profile_url"], person["name"],
                                person["title"], person["company"], note_type
                            )
                            sent += 1
                            sent_names.append(f"• {person['name']} — {person['title']} @ {person['company']}")
                        else:
                            skipped += 1
                    except Exception as e:
                        print(f"  Error with {person['name']}: {e}")
                        skipped += 1

                    await asyncio.sleep(4)  # Pace requests — don't look like a bot

        finally:
            await browser.close()

    # Report to Telegram
    if sent == 0:
        send_jobs("🤝 *Networking run complete*\nNo new connection requests sent (all already connected or no Connect button).")
    else:
        names_list = "\n".join(sent_names[:10])
        send_jobs(
            f"🤝 *Networking done!*\n"
            f"Sent *{sent}* connection requests ({skipped} skipped)\n\n"
            f"{names_list}"
        )


def get_status_report():
    init_connections_db()
    total, today, by_type, recent = get_connection_stats()

    lines = [f"🤝 *LinkedIn Networking Status*\n"]
    lines.append(f"Total requests sent: *{total}*")
    lines.append(f"Sent today: *{today}* / {DAILY_LIMIT}\n")

    if by_type:
        lines.append("*By type:*")
        emoji = {"recruiter": "🎯", "hr": "🏢", "hiring_manager": "👔", "peer": "👥"}
        for t, count in by_type.items():
            lines.append(f"{emoji.get(t, '•')} {t.replace('_', ' ').title()}: {count}")

    if recent:
        lines.append("\n*Recent:*")
        for name, title, company, sent_at in recent:
            date_str = sent_at[:10] if sent_at else "?"
            lines.append(f"• {name} — {title[:30]} ({date_str})")

    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(run_networking())
