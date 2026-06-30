#!/usr/bin/env python3
"""
Hourly Tech & LinkedIn Alerts
- Every hour: HackerNews + Dev.to + RSS for fresh tech news, courses, inventions
- Every 6 hours: LinkedIn Learning scan for new relevant courses
- Claude scores relevance; only 7+ items reach Telegram
- Tracks seen URLs in DB — no duplicates ever
"""

import requests
import sqlite3
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import dotenv_values
import anthropic

config        = dotenv_values(Path.home() / ".env")
ANTHROPIC_KEY = config.get("ANTHROPIC_API_KEY")
DB_PATH       = Path(__file__).parent / "applied_jobs.db"

# ── Topics Harshith cares about ───────────────────────────────────────────────

KEYWORDS = [
    "aws", "devops", "kubernetes", "docker", "terraform", "cloud",
    "python", ".net", "c#", "github actions", "ci/cd", "jenkins",
    "machine learning", "ai", "llm", "mlops", "generative ai",
    "fastapi", "microservices", "observability", "prometheus", "grafana",
    "security", "networking", "linux", "ireland tech", "new tool",
    "open source", "course", "certification", "invention", "research",
]

COURSE_KEYWORDS = [
    "aws", "devops", "cloud", "kubernetes", "python", "machine learning",
    "ai", "terraform", "docker", "ci/cd", "security", "mlops",
]

# ── DB helpers ────────────────────────────────────────────────────────────────

def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tech_alerts (
            url        TEXT PRIMARY KEY,
            title      TEXT,
            source     TEXT,
            sent_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def already_sent(url: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT url FROM tech_alerts WHERE url=?", (url,)).fetchone()
    conn.close()
    return row is not None


def mark_sent(url: str, title: str, source: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO tech_alerts (url, title, source) VALUES (?,?,?)",
        (url, title, source)
    )
    conn.commit()
    conn.close()


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_hackernews(hours_back=1) -> list[dict]:
    """Fetch HackerNews stories from the last N hours via Algolia API."""
    since  = int((datetime.now(timezone.utc) - timedelta(hours=hours_back)).timestamp())
    query  = "aws kubernetes python devops ai machine learning cloud terraform"
    url    = (
        f"https://hn.algolia.com/api/v1/search"
        f"?query={requests.utils.quote(query)}"
        f"&tags=story"
        f"&numericFilters=created_at_i>{since},points>5"
        f"&hitsPerPage=20"
    )
    try:
        r    = requests.get(url, timeout=10)
        hits = r.json().get("hits", [])
        items = []
        for h in hits:
            link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            if not already_sent(link):
                items.append({
                    "title":  h.get("title", ""),
                    "url":    link,
                    "source": "HackerNews",
                    "points": h.get("points", 0),
                    "type":   "news",
                })
        return items
    except Exception as e:
        print(f"HN error: {e}")
        return []


def fetch_devto(hours_back=1) -> list[dict]:
    """Fetch recent Dev.to articles tagged with relevant topics."""
    tags   = ["aws", "devops", "python", "kubernetes", "machinelearning", "cloud", "terraform", "ai"]
    items  = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    for tag in tags[:5]:  # limit to avoid rate limiting
        try:
            r    = requests.get(
                f"https://dev.to/api/articles?tag={tag}&per_page=5&top=1",
                timeout=8
            )
            for art in r.json():
                url       = art.get("url", "")
                published = art.get("published_at", "")
                if not url or already_sent(url):
                    continue
                # Check if published within the lookback window
                try:
                    pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if pub_dt < cutoff:
                        continue
                except Exception:
                    pass
                items.append({
                    "title":    art.get("title", ""),
                    "url":      url,
                    "source":   "Dev.to",
                    "points":   art.get("positive_reactions_count", 0),
                    "type":     "article",
                    "tag":      tag,
                })
        except Exception as e:
            print(f"Dev.to error ({tag}): {e}")
        time.sleep(0.5)

    return items


def fetch_rss_feeds(hours_back=1) -> list[dict]:
    """Fetch from curated RSS feeds for the past N hours."""
    try:
        import feedparser
    except ImportError:
        return []

    feeds = [
        ("AWS Blog",      "https://aws.amazon.com/blogs/aws/feed/"),
        (".NET Blog",     "https://devblogs.microsoft.com/dotnet/feed/"),
        ("Dev.to DevOps", "https://dev.to/feed/tag/devops"),
        ("GitHub Blog",   "https://github.blog/feed/"),
        ("InfoQ Cloud",   "https://feed.infoq.com/cloud/"),
    ]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    items  = []

    for source_name, feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                link  = entry.get("link", "")
                if not link or already_sent(link):
                    continue
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                text = (title + " " + entry.get("summary", "")).lower()
                if any(kw in text for kw in KEYWORDS):
                    items.append({
                        "title":  title,
                        "url":    link,
                        "source": source_name,
                        "points": 0,
                        "type":   "article",
                    })
        except Exception as e:
            print(f"RSS error ({source_name}): {e}")

    return items


async def fetch_linkedin_courses() -> list[dict]:
    """Scrape LinkedIn Learning for recently added courses in relevant topics."""
    import json as _json
    from playwright.async_api import async_playwright

    SESSION_FILE = Path(__file__).parent / "linkedin_session.json"
    if not SESSION_FILE.exists():
        return []

    courses = []
    try:
        cookies = _json.loads(SESSION_FILE.read_text())
        if isinstance(cookies, dict) and "cookies" in cookies:
            cookies = cookies["cookies"]

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            await context.add_cookies(cookies)
            page = await context.new_page()

            for keyword in COURSE_KEYWORDS[:4]:  # limit to 4 searches
                url = (
                    f"https://www.linkedin.com/learning/search"
                    f"?keywords={keyword.replace(' ', '%20')}"
                    f"&sortBy=RECENCY"
                    f"&entityType=COURSE"
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)

                    if "login" in page.url or "authwall" in page.url:
                        break

                    cards = await page.query_selector_all(".base-card")
                    for card in cards[:5]:
                        try:
                            title_el = await card.query_selector(".base-card__full-link")
                            if not title_el:
                                continue
                            title    = (await title_el.inner_text()).strip()
                            href     = await title_el.get_attribute("href") or ""
                            link     = href.split("?")[0] if href.startswith("http") else f"https://www.linkedin.com{href}"
                            if title and link and not already_sent(link):
                                courses.append({
                                    "title":  f"📚 Course: {title}",
                                    "url":    link,
                                    "source": "LinkedIn Learning",
                                    "points": 0,
                                    "type":   "course",
                                })
                        except Exception:
                            continue
                except Exception as e:
                    print(f"LinkedIn course error ({keyword}): {e}")

            await browser.close()
    except Exception as e:
        print(f"LinkedIn Learning error: {e}")

    return courses


# ── Claude scoring ────────────────────────────────────────────────────────────

def score_and_filter(items: list[dict], min_score=7) -> list[dict]:
    """Ask Claude to score each item 1-10 for relevance to Harshith's profile."""
    if not items:
        return []

    client  = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    catalog = "\n".join(
        f"{i}. [{a['source']}] {a['title']}"
        for i, a in enumerate(items)
    )

    prompt = f"""Score each item 1-10 for relevance to a junior software/DevOps engineer in Dublin, Ireland.
Profile: AWS, .NET Core, Python, Kubernetes, Docker, Terraform, CI/CD, ML/AI interest, job hunting.
High score (8-10): directly useful — new tool, course, AWS update, AI breakthrough, DevOps tip.
Low score (1-4): generic, beginner content unrelated to his stack, or off-topic.

Items:
{catalog}

Reply ONLY with a JSON array of integers matching the item count. Example: [8, 3, 9, 5, 7]"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        scores = json.loads(response.content[0].text.strip())
        return [item for item, score in zip(items, scores) if score >= min_score]
    except Exception as e:
        print(f"Claude scoring error: {e}")
        # Fallback: keyword match
        return [i for i in items if any(kw in i["title"].lower() for kw in KEYWORDS)]


# ── Send to Telegram ──────────────────────────────────────────────────────────

def send_alert(items: list[dict]):
    from telegram_topics import send_daily

    if not items:
        return

    now   = datetime.now().strftime("%H:%M, %d %b")
    lines = [f"🔔 *Tech & LinkedIn Alerts — {now}*\n"]

    type_emoji = {"course": "📚", "news": "📰", "article": "📝"}

    for item in items[:8]:
        emoji = type_emoji.get(item.get("type", "news"), "🔹")
        lines.append(f"{emoji} [{item['source']}] [{item['title']}]({item['url']})")
        mark_sent(item["url"], item["title"], item["source"])

    send_daily("\n".join(lines))


# ── Main runners ──────────────────────────────────────────────────────────────

def run_hourly():
    """Fetch tech news + articles from fast APIs and send relevant ones."""
    _init_db()
    items = fetch_hackernews(hours_back=1) + fetch_devto(hours_back=1) + fetch_rss_feeds(hours_back=1)
    print(f"Fetched {len(items)} new items")
    if not items:
        return
    filtered = score_and_filter(items, min_score=7)
    print(f"Scored: {len(filtered)} relevant items")
    send_alert(filtered)


async def run_linkedin_courses():
    """Scrape LinkedIn Learning for new courses and send relevant ones."""
    _init_db()
    courses  = await fetch_linkedin_courses()
    filtered = score_and_filter(courses, min_score=6)
    send_alert(filtered)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "courses":
        import asyncio
        asyncio.run(run_linkedin_courses())
    else:
        run_hourly()
