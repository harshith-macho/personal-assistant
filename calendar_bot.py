#!/usr/bin/env python3
"""
Google Calendar fetcher — gets today's and upcoming events.
"""

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

SCOPES  = ["https://www.googleapis.com/auth/calendar"]
BASE    = Path(__file__).parent
TOKEN   = BASE / "token.json"
CREDS_F = BASE / "credentials.json"


def get_calendar_service():
    creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def fetch_todays_events():
    service = get_calendar_service()

    now = datetime.now(LOCAL_TZ)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    events_result = service.events().list(
        calendarId="primary",
        timeMin=start_of_day.isoformat(),
        timeMax=end_of_day.isoformat(),
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    return events_result.get("items", [])


def fetch_upcoming_events(days=3):
    service = get_calendar_service()

    now = datetime.now(timezone.utc)
    until = now + timedelta(days=days)

    events_result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=until.isoformat(),
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    return events_result.get("items", [])


WORK_KEYWORDS = {"morning routine", "standup", "daily standup", "lunch", "lunch break", "irvine", "office"}
WORK_TITLE_EXACT = {"work"}  # matched as whole words only to avoid catching "workout"
SKIP_KEYWORDS = {"personal time", "commute", "gym", "workout", "shower", "freshen", "dinner",
                 "wind down", "learn", "side project", "relax"}

def _is_work_event(event):
    title = event.get("summary", "").lower()
    if any(kw in title for kw in WORK_KEYWORDS):
        return True
    words = set(title.replace("/", " ").split())
    return bool(words & WORK_TITLE_EXACT)

def _is_skip_event(event):
    title = event.get("summary", "").lower()
    return any(kw in title for kw in SKIP_KEYWORDS)


def _collapse_work_events(events):
    """Replace all work-block events with a single 'Work' line; return remaining events."""
    work = [e for e in events if _is_work_event(e)]
    other = [e for e in events if not _is_work_event(e)]

    if not work:
        return events

    starts = []
    ends = []
    for e in work:
        s = e["start"].get("dateTime", e["start"].get("date", ""))
        en = e["end"].get("dateTime", e["end"].get("date", ""))
        try:
            starts.append(datetime.fromisoformat(s).astimezone(LOCAL_TZ))
            ends.append(datetime.fromisoformat(en).astimezone(LOCAL_TZ))
        except Exception:
            pass

    if not starts:
        return events

    start_dt = min(starts)
    end_dt   = max(ends)
    label    = f"• {start_dt.strftime('%b %d')} {start_dt.strftime('%I:%M %p')} – {end_dt.strftime('%I:%M %p')} — 💻 Work"

    # Insert the collapsed row where the first work event was, then add other events
    result = [(e, False) for e in other]  # (event, is_work_label)
    result.append((label, True))
    result.sort(key=lambda x: x[0]["start"].get("dateTime", x[0]["start"].get("date", "")) if not x[1] else label[2:10])
    return result


def _event_start_dt(event):
    s = event["start"].get("dateTime", event["start"].get("date", ""))
    try:
        return datetime.fromisoformat(s).astimezone(LOCAL_TZ)
    except Exception:
        return datetime.min.replace(tzinfo=LOCAL_TZ)


def format_event(event):
    summary  = event.get("summary", "No title")
    start    = event["start"].get("dateTime", event["start"].get("date", ""))
    location = event.get("location", "")

    try:
        dt = datetime.fromisoformat(start)
        if dt.tzinfo is not None:
            dt = dt.astimezone(LOCAL_TZ)
        time_str = dt.strftime("%I:%M %p")
        date_str = dt.strftime("%b %d")
    except Exception:
        time_str = start
        date_str = ""

    loc = f" @ {location}" if location else ""
    return f"• {date_str} {time_str} — {summary}{loc}"


def _format_events_collapsed(events):
    """Format a list of events, collapsing work-block entries into one row and hiding personal routine."""
    work  = [e for e in events if _is_work_event(e)]
    other = [e for e in events if not _is_work_event(e) and not _is_skip_event(e)]
    lines = []

    if work:
        starts, ends = [], []
        for e in work:
            s  = e["start"].get("dateTime", e["start"].get("date", ""))
            en = e["end"].get("dateTime", e["end"].get("date", ""))
            try:
                starts.append(datetime.fromisoformat(s).astimezone(LOCAL_TZ))
                ends.append(datetime.fromisoformat(en).astimezone(LOCAL_TZ))
            except Exception:
                pass
        if starts:
            s0, e0 = min(starts), max(ends)
            lines.append(f"• {s0.strftime('%b %d')} {s0.strftime('%I:%M %p')} – {e0.strftime('%I:%M %p')} — 💻 Work")

    for e in sorted(other, key=_event_start_dt):
        lines.append(format_event(e))

    return lines


def get_calendar_summary():
    today_events    = fetch_todays_events()
    upcoming_events = fetch_upcoming_events(days=3)

    today = datetime.now(LOCAL_TZ).strftime("%A, %b %d")
    lines = [f"📅 *Calendar — {today}*\n"]

    if today_events:
        lines.append("*Today:*")
        lines.extend(_format_events_collapsed(today_events))
    else:
        lines.append("*Today:* No events scheduled")

    today_ids = {e["id"] for e in today_events}
    upcoming  = [e for e in upcoming_events if e["id"] not in today_ids]

    if upcoming:
        lines.append("\n*Coming up:*")
        lines.extend(_format_events_collapsed(upcoming))

    return "\n".join(lines)


if __name__ == "__main__":
    print(get_calendar_summary())
