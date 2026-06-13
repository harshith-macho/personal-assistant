#!/usr/bin/env python3
"""
LinkedIn Feed Reader & Engagement Bot
- Scrapes LinkedIn feed for high-quality posts
- Scores them via Claude (relevance + quality)
- Sends top posts to Telegram for approval
- On approval: auto-comment (Claude-generated) + optional connection request
"""

import asyncio
import json
import sqlite3
import requests
import re
import time
from datetime import datetime
from pathlib import Path
from dotenv import dotenv_values
from playwright.async_api import async_playwright
import anthropic

config = dotenv_values(Path.home() / ".env")

ANTHROPIC_KEY = config.get("ANTHROPIC_API_KEY")
TELEGRAM_API  = f"https://api.telegram.org/bot{config.get('TELEGRAM_BOT_TOKEN')}"

DB_PATH      = Path(__file__).parent / "applied_jobs.db"
SESSION_FILE = Path(__file__).parent / "linkedin_session.json"

MIN_SCORE    = 7   # Only surface posts scoring 7+/10
MAX_PER_SCAN = 10  # Max posts sent to Telegram per scan


# ── Database ──────────────────────────────────────────────────────────────────

def init_feed_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feed_posts (
            id           TEXT PRIMARY KEY,
            author_name  TEXT,
            author_url   TEXT,
            post_text    TEXT,
            post_url     TEXT,
            score        INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'pending',
            comment_text TEXT,
            found_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_feed_post(post):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO feed_posts
               (id, author_name, author_url, post_text, post_url, score)
               VALUES (?,?,?,?,?,?)""",
            (post["id"], post["author_name"], post["author_url"],
             post["post_text"], post["post_url"], post.get("score", 0))
        )
        conn.commit()
    except Exception:
        pass
    conn.close()


def already_seen_post(post_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id FROM feed_posts WHERE id=?", (post_id,)).fetchone()
    conn.close()
    return row is not None


def update_post_status(post_id, status, comment_text=None):
    conn = sqlite3.connect(DB_PATH)
    if comment_text:
        conn.execute(
            "UPDATE feed_posts SET status=?, comment_text=? WHERE id=?",
            (status, comment_text, post_id)
        )
    else:
        conn.execute("UPDATE feed_posts SET status=? WHERE id=?", (status, post_id))
    conn.commit()
    conn.close()


def get_post_by_id(post_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, author_name, author_url, post_text, post_url FROM feed_posts WHERE id=?",
        (post_id,)
    ).fetchone()
    conn.close()
    if row:
        return {"id": row[0], "author_name": row[1], "author_url": row[2],
                "post_text": row[3], "post_url": row[4]}
    return None


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text, reply_markup=None, topic="chat"):
    from telegram_topics import TOPICS, GROUP_ID, TOKEN as TG_TOKEN
    payload = {
        "chat_id":           GROUP_ID,
        "text":              text,
        "parse_mode":        "Markdown",
        "message_thread_id": TOPICS.get(topic, TOPICS["chat"]),
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload)


def send_post_for_approval(post):
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Comment + Connect", "callback_data": f"fc_{post['id']}"},
            {"text": "💬 Comment only",       "callback_data": f"fco_{post['id']}"},
            {"text": "❌ Skip",               "callback_data": f"fs_{post['id']}"},
        ]]
    }
    preview = post["post_text"][:300] + ("..." if len(post["post_text"]) > 300 else "")
    text = (
        f"📰 *LinkedIn Post* — Score: {post.get('score', '?')}/10\n\n"
        f"👤 *{post['author_name']}*\n\n"
        f"_{preview}_\n\n"
        f"[View Post]({post['post_url']})"
    )
    send_telegram(text, reply_markup=markup, topic="chat")


# ── Claude ────────────────────────────────────────────────────────────────────

HIRING_KEYWORDS = [
    "we are hiring", "we're hiring", "immediate joiner", "looking for candidates",
    "job opening", "open position", "apply now", "send your resume", "send resume",
    "dm me your resume", "tag someone", "referral", "urgent requirement",
    "looking for a ", "hiring for", "positions available", "vacancies"
]


def score_post_with_claude(post):
    """Score a post 1-10. Job/hiring posts auto-score 0."""
    text_lower = post["post_text"].lower()
    if any(kw in text_lower for kw in HIRING_KEYWORDS):
        return 0  # Skip instantly without calling Claude

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": f"""Score this LinkedIn post 1-10 for a software engineer interested in:
.NET Core, AWS, DevOps, Kubernetes, Kafka, cloud-native architecture, ML/AI, fintech, career growth.

Post by {post['author_name']}:
{post['post_text'][:500]}

Reply with ONLY a number 1-10.
Score 8-10 = deep technical insight, lessons learned, architecture decisions, real experience.
Score 5-7 = useful but generic, surface-level tips, moderately relevant.
Score 1-4 = promotional content, job postings, recruiters advertising roles, irrelevant topics.
IMPORTANT: Score 1 if this is a job posting, hiring announcement, or recruiter message."""
            }]
        )
        return int(response.content[0].text.strip().split()[0])
    except Exception:
        return 5


def generate_comment(post):
    """Generate a thoughtful, relevant comment using Claude."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"""Write a short, genuine LinkedIn comment (2-3 sentences) for this post.
The commenter is Akshay, a software engineer with expertise in .NET Core, AWS, Kafka, and Kubernetes.
Be specific to the post content. Add real value or a relevant insight. Sound human.
Do NOT start with "Great post!" or "Excellent!" — be direct and thoughtful.

Post by {post['author_name']}:
{post['post_text'][:600]}

Return ONLY the comment text."""
        }]
    )
    return response.content[0].text.strip()


# ── LinkedIn Playwright ───────────────────────────────────────────────────────

async def load_session(context):
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text())
            # Support both raw cookies array and Playwright storage_state format
            cookies = data["cookies"] if isinstance(data, dict) else data
            await context.add_cookies(cookies)
            return True
        except Exception:
            pass
    return False


SEARCH_QUERIES = [
    ".NET Core AWS microservices cloud",
    "Kubernetes DevOps Terraform engineering",
    "software engineer career growth fintech",
    "Apache Kafka event-driven architecture",
    "machine learning AI cloud native",
]


def parse_posts_from_body(body_text, profile_map):
    """
    Parse LinkedIn search results body text into post dicts.
    LinkedIn renders post sections separated by 'Feed post' headings.
    profile_map: dict of author_name → profile_url collected from <a href='/in/...'>
    """
    posts = []
    sections = body_text.split("Feed post")
    for section in sections[1:]:
        lines = [l.strip() for l in section.strip().split("\n") if l.strip()]
        if not lines:
            continue
        author_name = lines[0]

        # Skip LinkedIn system/company noise
        if any(x in author_name.lower() for x in ["linkedin", "promoted", "suggested"]):
            continue

        # Find where post text starts: after the "time • " or "Follow"/"Connect" line
        text_start = 2
        for i, line in enumerate(lines[:8]):
            if "•" in line or line in ("Follow", "Connect", "Following", "Message"):
                text_start = i + 1
                break

        # Collect post text until engagement markers
        stop_words = {"Like", "Comment", "Repost", "Send", "reactions", "comments", "reposts"}
        text_lines = []
        for line in lines[text_start:]:
            if line in stop_words or re.match(r'^\d+\s*(reaction|comment|repost)', line, re.I):
                break
            if line in ("…more", "… more", "Show more"):
                break
            text_lines.append(line)

        post_text = " ".join(text_lines).strip()
        if not post_text or len(post_text) < 60:
            continue

        # Use author name to look up profile URL
        author_url = ""
        for name_key, url in profile_map.items():
            if author_name.lower() in name_key.lower() or name_key.lower() in author_name.lower():
                author_url = url
                break

        # Create a stable ID from author + text hash
        post_id = str(abs(hash(author_name + post_text[:100])))[:15]

        if already_seen_post(post_id):
            continue

        posts.append({
            "id":          post_id,
            "author_name": author_name,
            "author_url":  author_url,
            "post_text":   post_text,
            "post_url":    "",  # filled in when we find activity link
        })

    return posts


async def scrape_feed_posts(page):
    """Search LinkedIn for relevant posts and parse results."""
    posts     = []
    seen_ids  = set()

    for query in SEARCH_QUERIES:
        url = (
            f"https://www.linkedin.com/search/results/content/"
            f"?keywords={query.replace(' ', '%20')}"
            f"&datePosted=%22past-week%22"
        )
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        # Scroll to load more results
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(1200)

        # Collect profile URLs from anchor tags
        profile_map = {}
        all_links = await page.query_selector_all("a")
        for lnk in all_links:
            try:
                href = await lnk.get_attribute("href") or ""
                text = (await lnk.inner_text()).strip().split("\n")[0]
                if "/in/" in href and text and len(text) > 2:
                    full = href if href.startswith("http") else f"https://www.linkedin.com{href}"
                    profile_map[text] = full.split("?")[0]
            except Exception:
                continue

        # Collect activity URNs from data-urn attributes (more reliable than href)
        activity_map = {}
        urn_els = await page.query_selector_all("[data-urn*='activity']")
        for el in urn_els:
            urn = await el.get_attribute("data-urn") or ""
            m   = re.search(r'activity:(\d+)', urn)
            if m:
                activity_map[m.group(1)] = f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"
        # Also check href links as fallback
        for lnk in all_links:
            try:
                href = await lnk.get_attribute("href") or ""
                if "/feed/update/urn" in href:
                    m = re.search(r'activity:(\d+)', href)
                    if m:
                        activity_map[m.group(1)] = href.split("?")[0]
            except Exception:
                continue

        body_text = await page.inner_text("body")
        new_posts = parse_posts_from_body(body_text, profile_map)

        # Attach activity URLs — assign sequentially to posts in order found
        url_list = list(activity_map.values())
        for i, post in enumerate(new_posts):
            if not post["post_url"] and i < len(url_list):
                post["post_url"] = url_list[i]

        for post in new_posts:
            if post["id"] not in seen_ids:
                seen_ids.add(post["id"])
                posts.append(post)

        await asyncio.sleep(2)

    return posts


async def resolve_post_url(page, post):
    """
    Get the direct permalink for a post.
    Uses data-urn attributes on the author's activity page — these are always
    present even when LinkedIn obfuscates class names and hides href links.
    """
    if post.get("post_url"):
        return post["post_url"]
    if not post.get("author_url") or "/in/" not in post["author_url"]:
        return None
    try:
        activity_url = post["author_url"].rstrip("/") + "/recent-activity/all/"
        await page.goto(activity_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        # data-urn attributes contain activity IDs even with obfuscated class names
        urns = await page.evaluate("""() => {
            const els = document.querySelectorAll('[data-urn*="activity"]');
            return Array.from(els).map(el => el.getAttribute('data-urn')).filter(Boolean);
        }""")

        if urns:
            # Try to match the right post by checking body text order
            # Default to the most recent (first) post
            first_urn = urns[0]
            match = re.search(r'activity:(\d+)', first_urn)
            if match:
                return f"https://www.linkedin.com/feed/update/urn:li:activity:{match.group(1)}/"

    except Exception as e:
        print(f"  resolve_post_url error: {e}")
    return None


async def comment_on_post(page, post, comment_text):
    """Navigate to post permalink and submit a comment via browser clipboard paste."""
    try:
        post_url = await resolve_post_url(page, post)
        if not post_url:
            print(f"  No URL found for post by {post['author_name']}")
            return False

        await page.goto(post_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        await page.evaluate("window.scrollBy(0, 500)")
        await page.wait_for_timeout(1000)

        # Open the comment editor — button label is "Comment" in LinkedIn's UI
        comment_btn = await page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                if (btn.innerText.trim() === 'Comment' && !btn.disabled) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")
        if not comment_btn:
            print(f"  No Comment button found for post by {post['author_name']}")
            return False
        await page.wait_for_timeout(2000)

        # Find contenteditable editor
        editor = page.locator('[contenteditable="true"]').first
        if not await editor.is_visible():
            print(f"  Comment editor not visible for post by {post['author_name']}")
            return False
        await editor.click()
        await page.wait_for_timeout(500)

        # Paste via browser clipboard — the only method that triggers React's EditorState update
        await page.evaluate(f"navigator.clipboard.writeText({json.dumps(comment_text)})")
        await page.keyboard.press('Meta+v')
        await page.wait_for_timeout(1500)

        # Verify text landed
        content = await editor.inner_text()
        if not content.strip():
            print(f"  Paste failed for {post['author_name']} — editor empty")
            return False

        # Click the submit "Comment" button inside the form
        # It sits alongside Emoji/GIF/Photo buttons, 6 levels above the editor
        clicked = await page.evaluate("""() => {
            const editor = document.querySelector('[contenteditable="true"]');
            if (!editor) return false;
            let el = editor;
            for (let i = 0; i < 12; i++) {
                el = el.parentElement;
                if (!el) break;
                for (const btn of el.querySelectorAll('button')) {
                    const txt = btn.innerText.trim();
                    const lbl = btn.getAttribute('aria-label') || '';
                    if ((txt === 'Comment' || lbl === 'Comment') && !btn.disabled) {
                        btn.click();
                        return true;
                    }
                }
            }
            return false;
        }""")

        await page.wait_for_timeout(2500)
        if clicked:
            print(f"  ✅ Commented on post by {post['author_name']}")
            return True

        # Fallback: Ctrl+Enter
        await page.keyboard.press("Control+Return")
        await page.wait_for_timeout(2000)
        print(f"  ✅ Commented (Ctrl+Enter) on post by {post['author_name']}")
        return True

    except Exception as e:
        print(f"  ❌ Comment error: {e}")
        return False


async def connect_with_author(page, post):
    """Navigate to author profile and send a connection request."""
    if not post.get("author_url") or "/in/" not in post["author_url"]:
        return False
    try:
        await page.goto(post["author_url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Find Connect button (direct or inside More dropdown)
        connect_btn = await page.query_selector("button[aria-label*='Connect']")
        if not connect_btn:
            more_btn = await page.query_selector("button[aria-label*='More actions']")
            if more_btn:
                await more_btn.click()
                await page.wait_for_timeout(1000)
                connect_btn = await page.query_selector("div[aria-label*='Connect']")

        if not connect_btn:
            print(f"  No Connect button for {post['author_name']} — already connected or not available")
            return False

        await connect_btn.click()
        await page.wait_for_timeout(1500)

        # Add a personalized note
        note_btn = await page.query_selector("button[aria-label='Add a note']")
        if note_btn:
            await note_btn.click()
            await page.wait_for_timeout(1000)
            note_field = await page.query_selector("textarea#custom-message")
            if note_field:
                first_name = post["author_name"].split()[0]
                note = (
                    f"Hi {first_name}, I came across your recent post and found it really insightful. "
                    f"I'm a software engineer working with .NET Core, AWS, and cloud-native systems. "
                    f"Would love to connect!"
                )
                await note_field.fill(note[:300])

        send_btn = (
            await page.query_selector("button[aria-label='Send now']") or
            await page.query_selector("button[aria-label='Send invitation']")
        )
        if send_btn:
            await send_btn.click()
            await page.wait_for_timeout(1500)
            print(f"  ✅ Connection sent to {post['author_name']}")
            return True

        # Dismiss modal if send failed
        close = await page.query_selector("button[aria-label='Dismiss']")
        if close:
            await close.click()
        return False

    except Exception as e:
        print(f"  ❌ Connect error: {e}")
        return False


# ── Action Handler (called from bot_server callback) ─────────────────────────

async def handle_feed_action(post_id, action):
    """Execute comment + optional connect after Telegram approval."""
    post = get_post_by_id(post_id)
    if not post:
        send_telegram(f"⚠️ Post `{post_id}` not found.", topic="chat")
        return

    send_telegram(f"✍️ Generating comment for *{post['author_name']}'s* post...", topic="chat")
    comment_text = generate_comment(post)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            permissions=["clipboard-read", "clipboard-write"],
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        await load_session(context)

        # Step 1 — Comment
        commented = await comment_on_post(page, post, comment_text)
        if commented:
            update_post_status(post_id, "commented", comment_text)
            send_telegram(
                f"💬 *Commented on {post['author_name']}'s post:*\n\n_{comment_text}_",
                topic="chat"
            )
        else:
            send_telegram(
                "⚠️ Couldn't post comment — LinkedIn form may have changed. Try manually.",
                topic="chat"
            )

        # Step 2 — Connect (only if requested)
        if action == "comment_connect" and post.get("author_url"):
            await asyncio.sleep(3)
            connected = await connect_with_author(page, post)
            if connected:
                update_post_status(post_id, "connected", comment_text)
                send_telegram(
                    f"🤝 Connection request sent to *{post['author_name']}*",
                    topic="chat"
                )
            else:
                send_telegram(
                    f"ℹ️ Could not connect with *{post['author_name']}* — may already be connected.",
                    topic="chat"
                )

        await browser.close()


def handle_feed_callback(update):
    """Route fc_/fco_/fs_ callback queries from bot_server."""
    callback = update.get("callback_query", {})
    data     = callback.get("data", "")
    msg_id   = callback.get("id")

    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": msg_id})

    if data.startswith("fc_"):
        post_id = data[3:]
        asyncio.run(handle_feed_action(post_id, "comment_connect"))

    elif data.startswith("fco_"):
        post_id = data[4:]
        asyncio.run(handle_feed_action(post_id, "comment_only"))

    elif data.startswith("fs_"):
        post_id = data[3:]
        update_post_status(post_id, "skipped")
        send_telegram(f"⏭️ Skipped.", topic="chat")


# ── Entry point ───────────────────────────────────────────────────────────────

async def scan_feed():
    """Scrape feed → score with Claude → send top posts to Telegram."""
    init_feed_db()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            permissions=["clipboard-read", "clipboard-write"],
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        await load_session(context)

        # Quick session check
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        if "login" in page.url or "authwall" in page.url:
            send_telegram("⚠️ LinkedIn session expired — re-login needed.", topic="chat")
            await browser.close()
            return

        print("Searching LinkedIn for relevant posts...")
        posts = await scrape_feed_posts(page)
        await browser.close()

    if not posts:
        send_telegram("📰 *LinkedIn Feed*\nNo new posts found.", topic="chat")
        return

    print(f"Scoring {len(posts)} posts with Claude...")
    scored = []
    for post in posts:
        score = score_post_with_claude(post)
        post["score"] = score
        if score >= MIN_SCORE:
            scored.append(post)
            save_feed_post(post)

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:MAX_PER_SCAN]

    if not top:
        send_telegram(
            f"📰 *LinkedIn Feed*\nScanned {len(posts)} posts — none scored {MIN_SCORE}+/10 today.",
            topic="chat"
        )
        return

    send_telegram(
        f"📰 *LinkedIn Feed Scan Complete*\n"
        f"Found *{len(top)} quality posts* (score {MIN_SCORE}+) out of {len(posts)} scanned.\n"
        f"Tap ✅ to comment + connect, 💬 to comment only, ❌ to skip. 👇",
        topic="chat"
    )

    for post in top:
        send_post_for_approval(post)
        time.sleep(1)


if __name__ == "__main__":
    asyncio.run(scan_feed())
