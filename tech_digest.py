#!/usr/bin/env python3
"""
Daily Tech Digest — fetches top articles from HackerNews + RSS feeds,
summarizes with Claude, and posts to the Daily Telegram topic.
Runs via cron at 8:30am Mon-Fri.
"""

import feedparser
import requests
import anthropic
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import dotenv_values

config = dotenv_values(Path.home() / ".env")
ANTHROPIC_KEY = config.get("ANTHROPIC_API_KEY")

KEYWORDS = [
    "aws", ".net", "c#", "kafka", "kubernetes", "terraform",
    "microservices", "fintech", "devops", "docker", "cloud native",
    "api", "distributed systems", "event-driven", "observability"
]

RSS_FEEDS = [
    ("AWS Blog",      "https://aws.amazon.com/blogs/aws/feed/"),
    (".NET Blog",     "https://devblogs.microsoft.com/dotnet/feed/"),
    ("Confluent",     "https://www.confluent.io/blog/feed/"),
    ("InfoQ Cloud",   "https://feed.infoq.com/cloud/"),
    ("Dev.to DevOps", "https://dev.to/feed/tag/devops"),
]


def fetch_hn_articles(hours_back=24):
    """Fetch relevant HackerNews stories from the past 24h via Algolia API."""
    query = "aws kubernetes kafka .net terraform microservices fintech"
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours_back)).timestamp())
    url = (
        f"https://hn.algolia.com/api/v1/search"
        f"?query={requests.utils.quote(query)}"
        f"&tags=story"
        f"&numericFilters=created_at_i>{since},points>10"
        f"&hitsPerPage=30"
    )
    try:
        r = requests.get(url, timeout=10)
        hits = r.json().get("hits", [])
        articles = []
        for h in hits:
            title = h.get("title", "")
            link  = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            points = h.get("points", 0)
            articles.append({"title": title, "url": link, "source": "HackerNews", "points": points})
        return articles
    except Exception as e:
        print(f"HN fetch error: {e}")
        return []


def fetch_rss_articles(hours_back=24):
    """Fetch recent articles from RSS feeds, filtered by keywords."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    articles = []
    for source_name, feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "")
                link  = entry.get("link", "")
                # Parse published date
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                # Keyword filter
                text = (title + " " + entry.get("summary", "")).lower()
                if any(kw in text for kw in KEYWORDS):
                    articles.append({"title": title, "url": link, "source": source_name, "points": 0})
        except Exception as e:
            print(f"RSS error ({source_name}): {e}")
    return articles


def pick_and_summarize(articles):
    """Use Claude to pick the top 7 articles and write a brief digest."""
    if not articles:
        return None

    # Deduplicate by title similarity
    seen, unique = set(), []
    for a in articles:
        key = a["title"][:40].lower()
        if key not in seen:
            seen.add(key)
            unique.append(a)

    article_list = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']} — {a['url']}"
        for i, a in enumerate(unique[:40])
    )

    today = datetime.now().strftime("%A, %B %d")
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": f"""You are a tech digest curator for Akshay Mittapally, a senior .NET/cloud engineer working with AWS, Kafka, Kubernetes, Terraform, and fintech systems.

From the articles below, pick the 5-7 MOST relevant and interesting ones for his profile. Skip generic beginner content, job postings, or off-topic articles.

For each selected article write ONE line:
• [emoji] **Title** — one sentence on why it matters. [source]

Then add a 2-line "💡 Today's takeaway" at the end with the most actionable insight across all articles.

Keep the whole digest under 400 words. Be specific, not generic.

Articles:
{article_list}"""
        }]
    )
    return response.content[0].text


def run():
    print("Fetching tech articles...")
    hn_articles  = fetch_hn_articles()
    rss_articles = fetch_rss_articles()
    all_articles = hn_articles + rss_articles
    print(f"  HN: {len(hn_articles)} | RSS: {len(rss_articles)} | Total: {len(all_articles)}")

    if not all_articles:
        print("No articles found.")
        return

    digest = pick_and_summarize(all_articles)
    if not digest:
        print("No relevant articles after filtering.")
        return

    today = datetime.now().strftime("%A, %b %d")
    message = f"📰 *Tech Digest — {today}*\n\n{digest}"

    from telegram_topics import send_daily
    ok = send_daily(message)
    print("Digest sent!" if ok else "Failed to send.")


if __name__ == "__main__":
    run()
