#!/usr/bin/env python3
"""
LinkedIn Auto-Poster
Post updates to LinkedIn via API or via Telegram bot command.
"""

import requests
import anthropic
from pathlib import Path
from dotenv import dotenv_values

from form_filler import PROFILE  # single source of truth for identity

config = dotenv_values(Path.home() / ".env")

ANTHROPIC_KEY  = config.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = config.get("TELEGRAM_CHAT_ID")

PERSON_URN     = PROFILE["linkedin_person_urn"]


def get_token():
    config = dotenv_values(Path.home() / ".env")
    return config.get("LINKEDIN_ACCESS_TOKEN", "").strip("'")


def post_to_linkedin(text):
    """Post a text update to LinkedIn."""
    token = get_token()
    payload = {
        "author": PERSON_URN,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE"
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }
    resp = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0"
        },
        json=payload
    )
    return resp.status_code == 201, resp.json()


def generate_post_with_claude(topic):
    """Use Claude to write a professional LinkedIn post."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Write a professional LinkedIn post for {PROFILE['full_name']}, a software developer
specializing in AWS, DevOps, and Python. The post should be about: {topic}

Guidelines:
- 150-200 words max
- Professional but conversational tone
- Add 3-4 relevant hashtags at the end
- No emojis overload — keep it clean
- Sound authentic, not corporate"""
        }]
    )
    return message.content[0].text


def send_telegram(message):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT, "text": message, "parse_mode": "Markdown"}
    )


def post_from_topic(topic):
    """Generate and post to LinkedIn, notify via Telegram."""
    print(f"Generating post about: {topic}")
    post_text = generate_post_with_claude(topic)
    print(f"\nGenerated post:\n{post_text}\n")

    success, resp = post_to_linkedin(post_text)
    if success:
        msg = f"✅ *Posted to LinkedIn!*\n\n{post_text}"
        print("Posted successfully!")
    else:
        msg = f"⚠️ *LinkedIn post failed:* {resp}"
        print(f"Failed: {resp}")

    send_telegram(msg)
    return success, post_text


if __name__ == "__main__":
    import sys
    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "building personal AI assistants with Python and Claude AI"
    post_from_topic(topic)
