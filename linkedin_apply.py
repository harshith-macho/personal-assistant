#!/usr/bin/env python3
"""
LinkedIn Easy Apply Bot
- Finds Easy Apply jobs matching Harshith's profile
- Sends job details to Telegram for approval
- Applies to approved jobs automatically
"""

import asyncio
import io
import json
import sqlite3
import sys
import requests
import time
from datetime import datetime
from pathlib import Path
from dotenv import dotenv_values
from playwright.async_api import async_playwright
import anthropic as _anthropic

# Force UTF-8 output so emoji in print() never crashes on Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

config = dotenv_values(Path.home() / ".env")
ANTHROPIC_KEY = config.get("ANTHROPIC_API_KEY")

TELEGRAM_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = config.get("TELEGRAM_CHAT_ID")
TELEGRAM_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ── Interest Profile ─────────────────────────────────────────────────────────
# Edit this block to change what jobs get auto-applied to.
# Raise AUTO_APPLY_SCORE_THRESHOLD to be more selective (8+ = strong match only).
# Lower it to cast a wider net (6 = any related role).
MY_INTERESTS = {
    "name":      "Harshith",
    "role":      "Junior Software / DevOps Engineer",
    "location":  "Ireland",
    "exp_years": 2,
    "stack":     [".NET Core", "Python", "AWS", "Kubernetes", "Docker", "Terraform",
                  "CI/CD", "FastAPI", "Machine Learning"],
    "good_fit":  ["Python", "AWS", "DevOps", "cloud", ".NET", "software developer",
                  "backend", "platform engineer", "ML engineer", "data engineer",
                  "site reliability", "full stack", "graduate", "junior", "entry level"],
    "bad_fit":   ["SAP", "Salesforce", "ServiceNow", "construction software",
                  "5+ years required", "EU passport required", "Stamp 4 required",
                  "sales", "finance", "C-level", "director", "senior manager"],
}
AUTO_APPLY_SCORE_THRESHOLD = 6  # jobs scoring below this are skipped entirely

# Job search keywords (drive the LinkedIn search queries)
JOB_KEYWORDS = [
    "Software Engineer Ireland",
    "Software Developer Dublin",
    "Python Developer Ireland",
    "Backend Developer Ireland",
    "DevOps Engineer Ireland",
    "Cloud Engineer Ireland",
    "AWS Engineer Ireland",
    "Data Engineer Ireland",
    "Platform Engineer Ireland",
    "Machine Learning Engineer Ireland",
    "Junior Software Engineer",
    "Graduate Software Engineer",
]

# Skills that indicate a well-matched role — used to flag high-priority jobs
STRONG_MATCH_SKILLS = [
    "python", "aws", "devops", "cloud engineer", "kubernetes", "docker",
    "machine learning", "ml engineer", "data engineer", "platform engineer",
    "backend engineer", "backend developer", "full stack", ".net", "terraform",
]

def _match_score(title):
    """Return (count, matched_keywords) for a job title."""
    text = title.lower()
    matched = [kw for kw in STRONG_MATCH_SKILLS if kw in text]
    return len(matched), matched


LINKEDIN_EMAIL    = config.get("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD = config.get("LINKEDIN_PASSWORD")

LOCATION     = "Ireland"
DB_PATH      = Path(__file__).parent / "applied_jobs.db"
SESSION_FILE = Path(__file__).parent / "linkedin_session.json"
RESUMES_DIR  = Path(__file__).parent / "resumes"

# Resume PDFs per job category — place files in the resumes/ folder
RESUME_FILES = {
    "cloud":  RESUMES_DIR / "resume_devops.pdf",   # DevOps / Cloud / Infrastructure
    "ml":     RESUMES_DIR / "resume_ml.pdf",       # ML / AI / Data
    "swe":    RESUMES_DIR / "resume_swe.pdf",       # Software Engineering (default)
}
# Fallback txt paths used when PDFs are missing
_RESUME_TXT_FILES = {
    "cloud":  RESUMES_DIR / "resume_devops.txt",
    "ml":     RESUMES_DIR / "resume_fullstack.txt",
    "swe":    RESUMES_DIR / "resume_fullstack.txt",
}

# Keywords that determine which resume to pick
_CLOUD_KEYWORDS = [
    "devops", "cloud", "aws", "infrastructure", "platform engineer",
    "sre", "site reliability", "kubernetes", "terraform", "devsecops",
]
_ML_KEYWORDS = [
    "machine learning", "ml engineer", "data scientist", "data engineer",
    "ai engineer", "nlp", "deep learning", "mlops",
]

def categorize_job(title):
    """Return 'cloud', 'ml', or 'swe' based on job title keywords."""
    t = title.lower()
    if any(k in t for k in _CLOUD_KEYWORDS):
        return "cloud"
    if any(k in t for k in _ML_KEYWORDS):
        return "ml"
    return "swe"

def get_resume_path(category):
    """Return the best resume path for the category (PDF preferred, txt fallback)."""
    # Try PDF first (preferred for file upload)
    for cat in (category, "swe"):
        p = RESUME_FILES.get(cat)
        if p and p.exists():
            return str(p)
    for p in RESUME_FILES.values():
        if p.exists():
            return str(p)
    # Fall back to txt files (used for text-field injection in ATS forms)
    for cat in (category, "swe", "cloud", "ml"):
        p = _RESUME_TXT_FILES.get(cat)
        if p and p.exists():
            return str(p)
    return None


class SessionExpiredError(Exception):
    pass


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id              TEXT PRIMARY KEY,
            title           TEXT,
            company         TEXT,
            location        TEXT,
            url             TEXT,
            status          TEXT DEFAULT 'pending',
            stage           TEXT DEFAULT 'pending',
            notes           TEXT,
            tailored_resume TEXT,
            recruiter       TEXT,
            found_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
            applied_at      DATETIME
        )
    """)
    # Add columns if upgrading from older schema
    for col, definition in [
        ("stage",           "TEXT DEFAULT 'pending'"),
        ("notes",           "TEXT"),
        ("tailored_resume", "TEXT"),
        ("recruiter",       "TEXT"),
        ("external_url",    "TEXT"),
        ("category",        "TEXT"),
        ("relevance_score", "INTEGER"),
        ("description",     "TEXT"),
        ("apply_method",    "TEXT"),
        ("pending_fields",  "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {definition}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def save_job(job):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO jobs (id, title, company, location, url) VALUES (?,?,?,?,?)",
            (job["id"], job["title"], job["company"], job["location"], job["url"])
        )
        conn.commit()
    except Exception:
        pass
    conn.close()


def update_job_status(job_id, status, category=None):
    conn = sqlite3.connect(DB_PATH)
    # Only advance stage to 'applied' on confirmed success
    # For failed/skipped/approved, keep stage as-is so tracker stays clean
    if status == "applied":
        conn.execute(
            "UPDATE jobs SET status=?, stage=?, applied_at=?, category=? WHERE id=?",
            (status, "applied", datetime.now().isoformat(), category, job_id)
        )
    else:
        conn.execute(
            "UPDATE jobs SET status=?, applied_at=?, category=? WHERE id=?",
            (status, datetime.now().isoformat(), category, job_id)
        )
    conn.commit()
    conn.close()


def mark_needs_answer(job_id, pending_fields: list):
    """Save job as needs_answer with the blocking fields so retry knows what to fill."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE jobs SET status='needs_answer', pending_fields=? WHERE id=?",
        (json.dumps(pending_fields), job_id)
    )
    conn.commit()
    conn.close()


def get_needs_answer_jobs() -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, company, location, url, category, relevance_score, pending_fields "
        "FROM jobs WHERE status='needs_answer'"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0], "title": r[1], "company": r[2],
            "location": r[3], "url": r[4], "category": r[5],
            "relevance_score": r[6],
            "pending_fields": json.loads(r[7] or "[]"),
        }
        for r in rows
    ]


def get_pending_jobs():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, company, location, url FROM jobs "
        "WHERE status='approved' AND (apply_method='auto' OR apply_method IS NULL)"
    ).fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "company": r[2], "location": r[3], "url": r[4]} for r in rows]


def already_seen(job_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return row is not None


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text, reply_markup=None):
    from telegram_topics import TOPICS, GROUP_ID, TOKEN as TG_TOKEN
    payload = {
        "chat_id": GROUP_ID,
        "text": text,
        "parse_mode": "Markdown",
        "message_thread_id": TOPICS["jobs"],
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload)


def _approve_job(job_id, apply_method):
    """Mark job approved with the given method. Returns (title, company) or None."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE jobs SET status='approved', apply_method=? WHERE id=?",
        (apply_method, job_id)
    )
    row = conn.execute("SELECT title, company FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.commit()
    conn.close()
    return row


def send_job_for_approval(job, score=None):
    kw_score, matched = _match_score(job["title"])
    is_strong = kw_score >= 1

    score_badge = f"  ⭐ `{score}/10`" if score else ""
    header = "🔥 *Strong Match!*\n\n" if is_strong else ""
    text = (
        f"{header}💼 *New Job Found*{score_badge}\n\n"
        f"*{job['title']}*\n"
        f"🏢 {job['company']}\n"
        f"📍 {job['location']}\n\n"
        f"[View Job]({job['url']})"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "🤖 Auto Apply", "callback_data": f"auto_{job['id']}"},
                {"text": "📎 I'll Apply",  "callback_data": f"iapply_{job['id']}"},
            ],
            [
                {"text": "❌ Skip", "callback_data": f"skip_{job['id']}"},
            ],
        ]
    }
    send_telegram(text, reply_markup=markup)


def handle_callback(update):
    callback = update.get("callback_query", {})
    data     = callback.get("data", "")
    msg_id   = callback.get("id")

    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": msg_id})

    if data.startswith("auto_"):
        job_id = data[5:]
        row = _approve_job(job_id, "auto")
        if row:
            send_telegram(f"🤖 Queued for *Auto Apply*: *{row[0]}* at {row[1]}\nTap /applyjobs when ready.")
        else:
            send_telegram("🤖 Queued for Auto Apply — tap /applyjobs when ready.")

    elif data.startswith("iapply_"):
        job_id = data[7:]
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE jobs SET status='manual', apply_method='manual' WHERE id=?", (job_id,)
        )
        row = conn.execute("SELECT title, company, url FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.commit()
        conn.close()
        if row:
            markup = {"inline_keyboard": [[{"text": "🌐 Open Job", "url": row[2]}]]}
            send_telegram(
                f"📎 *Saved for manual apply*\n*{row[0]}* at {row[1]}\nTap below to open and apply yourself.",
                reply_markup=markup
            )
        else:
            send_telegram("📎 Saved for manual apply.")

    elif data.startswith("skip_"):
        job_id = data[5:]
        update_job_status(job_id, "skipped")
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT title, company FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        if row:
            send_telegram(f"❌ Skipped: *{row[0]}* at {row[1]}")
        else:
            send_telegram("❌ Skipped.")

    elif data.startswith("apply_"):
        # backward compat — old 2-button cards
        job_id = data[6:]
        row = _approve_job(job_id, "auto")
        if row:
            send_telegram(f"🤖 Queued: *{row[0]}* at {row[1]}\nTap /applyjobs when ready.")
        else:
            send_telegram("🤖 Queued — tap /applyjobs when ready.")


# ── LinkedIn Playwright ───────────────────────────────────────────────────────

async def save_session(page):
    cookies = await page.context.cookies()
    SESSION_FILE.write_text(json.dumps(cookies))


async def load_session(context):
    if SESSION_FILE.exists():
        data = json.loads(SESSION_FILE.read_text())
        cookies = data["cookies"] if isinstance(data, dict) and "cookies" in data else data
        await context.add_cookies(cookies)
        return True
    return False


async def login_linkedin_visible():
    """Open a visible browser, auto-fill credentials, and save session."""
    send_telegram("🔐 Opening LinkedIn login browser — filling credentials automatically...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport=None,  # use maximized window size
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        page = await context.new_page()
        try:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Dismiss cookie / consent banners if present
            for selector in [
                "button[action-type='ACCEPT']",
                "button[data-tracking-control-name='public_cookies_accept-all']",
                "button:text('Accept')",
                "button:text('Accept all')",
                "button:text('Allow')",
            ]:
                try:
                    btn = await page.query_selector(selector)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            # Fill credentials via JavaScript — bypasses visibility/overlay issues
            filled = await page.evaluate(f"""() => {{
                const email = document.querySelector("input[type='email'], input#username, input[name='session_key']");
                const pwd   = document.querySelector("input[type='password'], input#password, input[name='session_password']");
                if (!email || !pwd) return false;
                // Trigger React/Vue synthetic events so LinkedIn's JS registers the values
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeInputValueSetter.call(email, {repr(LINKEDIN_EMAIL)});
                email.dispatchEvent(new Event('input', {{ bubbles: true }}));
                email.dispatchEvent(new Event('change', {{ bubbles: true }}));
                nativeInputValueSetter.call(pwd, {repr(LINKEDIN_PASSWORD)});
                pwd.dispatchEvent(new Event('input', {{ bubbles: true }}));
                pwd.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}""")

            if not filled:
                send_telegram("❌ Could not find email/password fields on LinkedIn login page — page structure may have changed.")
                return

            await page.wait_for_timeout(1000)

            # Click Sign In
            signin_btn = (
                await page.query_selector("button[type='submit']") or
                await page.query_selector("button[data-litms-control-urn*='login']") or
                page.get_by_role("button", name="Sign in").last
            )
            if hasattr(signin_btn, 'click'):
                await signin_btn.click(timeout=10000)
            else:
                await signin_btn.click(timeout=10000)
            await page.wait_for_timeout(4000)

            # Detect what LinkedIn is showing after sign-in attempt
            current_url = page.url
            page_text   = await page.evaluate("() => document.body.innerText")

            if "/feed" in current_url:
                # Logged in immediately — no challenge
                await save_session(page)
                send_telegram("✅ LinkedIn logged in successfully! Session saved.")
                return

            if "/login" in current_url and "password" in page_text.lower():
                send_telegram(
                    "❌ *LinkedIn login failed* — wrong email or password.\n"
                    "Check `LINKEDIN_EMAIL` and `LINKEDIN_PASSWORD` in `~/.env`."
                )
                return

            # LinkedIn is showing a security checkpoint
            challenge_type = "unknown"
            if "verification" in page_text.lower() or "verify" in page_text.lower():
                if "email" in page_text.lower():
                    challenge_type = "email_code"
                elif "phone" in page_text.lower() or "sms" in page_text.lower():
                    challenge_type = "phone_code"
                else:
                    challenge_type = "verification"
            elif "captcha" in page_text.lower() or "robot" in page_text.lower():
                challenge_type = "captcha"
            elif "checkpoint" in current_url:
                challenge_type = "checkpoint"

            messages = {
                "email_code":   "📧 *LinkedIn sent a verification code to your email.*\nOpen your email, find the code, and type it in the browser window that just opened. You have 5 minutes.",
                "phone_code":   "📱 *LinkedIn sent a verification code to your phone.*\nEnter the SMS code in the browser window that just opened. You have 5 minutes.",
                "captcha":      "🤖 *LinkedIn is showing a CAPTCHA.*\nSolve it in the browser window that just opened. You have 5 minutes.",
                "checkpoint":   "🔐 *LinkedIn security checkpoint appeared.*\nComplete the verification in the browser window that just opened. You have 5 minutes.",
                "verification": "🔐 *LinkedIn needs identity verification.*\nComplete it in the browser window that just opened. You have 5 minutes.",
                "unknown":      "⚠️ *LinkedIn showed an unexpected page after login.*\nCheck the browser window and complete any verification. You have 5 minutes.",
            }
            send_telegram(messages.get(challenge_type, messages["unknown"]))
            print(f"  Challenge type: {challenge_type} | URL: {current_url}")

            # Wait up to 5 minutes for the user to complete verification manually
            try:
                await page.wait_for_url("**/feed/**", timeout=300000)
                await save_session(page)
                send_telegram("✅ LinkedIn logged in! Session saved — job search is ready.")
                print("✅ Session refreshed after manual verification.")
            except Exception:
                send_telegram(
                    "⏰ *LinkedIn login timed out* (5 min).\n"
                    "Run `python linkedin_apply.py login` again when you're ready."
                )
                print("Login timed out.")

        except Exception as e:
            send_telegram(f"❌ LinkedIn login error: `{e}`")
            print(f"Login error: {e}")
        finally:
            await browser.close()


async def search_jobs(page, keyword):
    """Search LinkedIn Easy Apply jobs and return job cards."""
    url = (
        f"https://www.linkedin.com/jobs/search/?"
        f"keywords={keyword.replace(' ', '%20')}"
        f"&location={LOCATION.replace(' ', '%20').replace(',', '%2C')}"
        f"&f_LF=f_AL"  # Easy Apply only
        f"&sortBy=DD"
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    # Wait for job cards to render, then scroll to load more
    try:
        await page.wait_for_selector("a[href*='/jobs/view/']", timeout=12000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)
    await page.evaluate("window.scrollBy(0, 600)")
    await page.wait_for_timeout(2000)

    jobs = []
    seen_ids = set()

    links = await page.query_selector_all("a[href*='/jobs/view/']")

    for link in links[:20]:
        try:
            href  = await link.get_attribute("href") or ""
            title = (await link.inner_text()).strip()
            # Strip LinkedIn badge/status suffixes that bleed into the link text
            for _suffix in [" with verification", " · Promoted", " · New", "· Promoted", "· New"]:
                if title.endswith(_suffix):
                    title = title[: -len(_suffix)].strip()

            if not href or not title or len(title) < 3:
                continue

            # Extract numeric job ID from slug URL
            slug  = href.split("/jobs/view/")[1].split("?")[0].split("/")[0]
            # Last segment is the numeric ID: "dotnet-developer-at-tranzeal-4414123"
            parts  = slug.rsplit("-", 1)
            job_id = parts[-1] if parts[-1].isdigit() else slug

            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            # Get company + location from parent li
            parent = await link.evaluate_handle("el => el.closest('li')")
            parent_el = parent.as_element() if parent else None

            company  = "Unknown Company"
            location = ""
            if parent_el:
                # Authenticated (logged-in) LinkedIn SPA selectors
                company_el = (
                    await parent_el.query_selector(".artdeco-entity-lockup__subtitle") or
                    await parent_el.query_selector("div[class*='subtitle']") or
                    await parent_el.query_selector(".job-card-container__primary-description") or
                    await parent_el.query_selector(".base-search-card__subtitle")
                )
                if company_el:
                    company = (await company_el.inner_text()).strip()

                location_el = (
                    await parent_el.query_selector(".job-card-container__metadata-wrapper") or
                    await parent_el.query_selector(".job-search-card__location") or
                    await parent_el.query_selector(".base-search-card__metadata")
                )
                if location_el:
                    location = (await location_el.inner_text()).strip().split("\n")[0]

            full_url = f"https://www.linkedin.com{href}" if href.startswith("/") else href

            jobs.append({
                "id":       job_id,
                "title":    title,
                "company":  company,
                "location": location,
                "url":      full_url.split("?")[0],
            })

        except Exception:
            continue

    return jobs


_SUCCESS_SIGNALS = [
    "application submitted", "application received", "application complete",
    "thank you for applying", "thank you for your application",
    "we've received your application", "successfully submitted",
    "you have applied", "application was submitted", "your application has been",
    "thanks for applying", "received your application",
]

def _is_success_page(body: str) -> bool:
    b = body.lower()
    return any(s in b for s in _SUCCESS_SIGNALS)


async def _ats_click_next(page) -> str | None:
    """
    Click the next available action button on an external ATS form.
    Returns 'submit', 'next', 'review', or None if nothing found.
    """
    result = await page.evaluate("""() => {
        const priority = [
            ['Submit application', 'submit'],
            ['Submit Application', 'submit'],
            ['Submit', 'submit'],
            ['Apply now', 'submit'],
            ['Apply Now', 'submit'],
            ['Complete application', 'submit'],
            ['Send application', 'submit'],
            ['Review', 'review'],
            ['Continue', 'next'],
            ['Next', 'next'],
            ['Next step', 'next'],
            ['Next Step', 'next'],
            ['Save and continue', 'next'],
            ['Save & Continue', 'next'],
        ];
        for (const [label, action] of priority) {
            for (const btn of document.querySelectorAll('button, input[type=submit], a[role=button]')) {
                const t = (btn.innerText || btn.value || '').trim();
                const a = btn.getAttribute('aria-label') || '';
                if (t === label || a === label) {
                    btn.click();
                    return action;
                }
            }
        }
        // Fuzzy: any visible button whose text contains submit/apply
        for (const btn of document.querySelectorAll('button, input[type=submit]')) {
            const t = (btn.innerText || btn.value || '').trim().toLowerCase();
            if ((t.includes('submit') || t.includes('apply')) && !btn.disabled) {
                btn.click();
                return 'submit';
            }
        }
        return null;
    }""")
    return result


async def try_ats_apply(context, external_url: str, job: dict) -> bool:
    """
    Universal external ATS auto-apply — works on any site (Greenhouse, Lever,
    Ashby, Workday, SmartRecruiters, iCIMS, custom sites).

    Strategy:
      1. Fill all recognisable fields with profile data.
      2. Upload resume.
      3. Ask user via Telegram for any required fields we can't answer.
      4. Click Next/Continue/Submit — loop up to 20 steps.
      5. Detect success page and return True.
    """
    from form_filler import fill_form_step, PROFILE

    url_lower = external_url.lower()
    is_greenhouse = "greenhouse.io" in url_lower or "boards.greenhouse" in url_lower
    is_lever      = "lever.co" in url_lower or "jobs.lever" in url_lower
    is_workday    = "myworkdayjobs.com" in url_lower or "wd3.myworkday" in url_lower or "workday.com" in url_lower
    is_smartrecruiters = "smartrecruiters.com" in url_lower
    is_icims      = "icims.com" in url_lower

    ats_name = (
        "Greenhouse" if is_greenhouse else
        "Lever"      if is_lever      else
        "Workday"    if is_workday    else
        "SmartRecruiters" if is_smartrecruiters else
        "iCIMS"      if is_icims      else
        "External"
    )
    print(f"  [{ats_name}] auto-apply: {external_url}")

    # Notify user so they know what's happening (and can answer Telegram questions)
    send_telegram(
        f"🌐 *Applying on {ats_name}*\n"
        f"*{job['title']}* @ {job['company']}\n"
        f"If I ask you questions below, please reply quickly so the form doesn't time out."
    )

    category    = categorize_job(job["title"])
    resume_path = get_resume_path(category)

    page = await context.new_page()
    try:
        await page.goto(external_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # ── Greenhouse: seed known fields by ID before generic filler runs ──
        if is_greenhouse:
            await _fill_field(page, "input#first_name", "Harshith")
            await _fill_field(page, "input#last_name",  "Mittapally")
            await _fill_field(page, "input#email",      "harshithreddy200811@gmail.com")
            await _fill_field(page, "input#phone",      PROFILE.get("phone", ""))

        # ── Lever: seed known name/email/phone fields ──
        if is_lever:
            await _fill_field(page, "input[name='name']",  "Harshith Mittapally")
            await _fill_field(page, "input[name='email']", "harshithreddy200811@gmail.com")
            await _fill_field(page, "input[name='phone']", PROFILE.get("phone", ""))

        resume_uploaded = False
        last_page_hash = ""
        stuck_steps    = 0

        for step in range(20):
            await page.wait_for_timeout(1500)

            # Detect if page content stopped changing (stuck in a loop)
            cur_hash = await page.evaluate("() => document.body.innerText.slice(0, 300)")
            if cur_hash == last_page_hash:
                stuck_steps += 1
                if stuck_steps >= 3:
                    print(f"  [ATS] Page not advancing after {step} steps — stopping")
                    break
            else:
                stuck_steps = 0
            last_page_hash = cur_hash

            # Upload resume on first step that has a file input
            if resume_path and not resume_uploaded:
                file_input = await page.query_selector("input[type='file']")
                if file_input:
                    try:
                        await file_input.set_input_files(resume_path)
                        await page.wait_for_timeout(1500)
                        resume_uploaded = True
                        print(f"  [{ats_name}] Uploaded resume: {Path(resume_path).name}")
                    except Exception as e:
                        print(f"  [{ats_name}] Resume upload error: {e}")

            # Fill all fields on this step (known rules + Telegram Q&A for unknowns)
            await fill_form_step(page, job=job)
            await page.wait_for_timeout(800)

            # Check if we're already on a success page (some sites auto-advance)
            body = await page.evaluate("() => document.body.innerText")
            if _is_success_page(body):
                print(f"  ✅ [{ats_name}] Applied: {job['title']} @ {job['company']}")
                return True

            # Click next action button
            clicked = await _ats_click_next(page)
            print(f"  [{ats_name}] Step {step}: clicked={clicked}")

            if clicked == "submit":
                await page.wait_for_timeout(3000)
                body = await page.evaluate("() => document.body.innerText")
                if _is_success_page(body):
                    print(f"  ✅ [{ats_name}] Applied: {job['title']} @ {job['company']}")
                    return True
                # Some sites show a thank-you redirect — check URL too
                if any(s in page.url.lower() for s in ["success", "confirm", "thank", "submitted"]):
                    print(f"  ✅ [{ats_name}] Applied (URL redirect): {job['title']}")
                    return True
                print(f"  [{ats_name}] Submit clicked but no confirmation detected")
                break

            if clicked is None:
                # No button found — check if we're stuck or already done
                if _is_success_page(body):
                    return True
                print(f"  [{ats_name}] No action button found — stopping")
                break

            # "next" or "review" — continue loop
            await page.wait_for_timeout(2000)

    except Exception as e:
        print(f"  [{ats_name}] Error: {e}")
    finally:
        await page.close()

    return False


async def _fill_field(page, selector: str, value: str):
    """Fill a field if it exists and is empty."""
    if not value:
        return
    try:
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            current = await el.input_value()
            if not current.strip():
                await el.fill(value)
    except Exception:
        pass


async def _is_session_alive(page):
    """Return True if the current LinkedIn session is still valid."""
    url = page.url
    return "linkedin.com" in url and "login" not in url and "authwall" not in url and "checkpoint" not in url


async def _find_linkedin_apply_button(page):
    """
    Multi-strategy apply button finder for LinkedIn job pages.
    Waits for the button to render (LinkedIn SPA can be slow in headless mode).
    Returns the element or None.
    """
    # Strategy 1: wait up to 8s for any known selector to appear
    css_selectors = [
        "button[aria-label='Easy Apply to this job']",
        "button[aria-label*='Easy Apply']",
        "button[aria-label*='Apply to']",
        "button[aria-label*='Apply on']",
        "button[aria-label*='company website']",
        ".jobs-apply-button",
        ".jobs-apply-button--top-card",
        "button.artdeco-button--primary[data-job-id]",
        "a[aria-label*='Apply']",
    ]
    for sel in css_selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=8000, state="visible")
            if el:
                return el
        except Exception:
            pass

    # Strategy 2: scroll down 400px — triggers LinkedIn's sticky apply bar
    await page.evaluate("window.scrollBy(0, 400)")
    await page.wait_for_timeout(1500)
    for sel in css_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return el
        except Exception:
            pass

    # Strategy 3: full JS scan — find any visible button/link whose text or aria-label
    # contains apply-related keywords (catches any label variation LinkedIn uses)
    el_handle = await page.evaluate_handle("""() => {
        const applyKws = ['easy apply', 'apply now', 'apply to', 'apply on', 'apply with'];
        const candidates = [
            ...document.querySelectorAll('button'),
            ...document.querySelectorAll('a[role="button"]'),
            ...document.querySelectorAll('a'),
        ];
        for (const el of candidates) {
            if (!el.offsetParent && el.offsetHeight === 0) continue;  // hidden
            const text  = (el.innerText  || '').toLowerCase().trim();
            const label = (el.getAttribute('aria-label') || '').toLowerCase();
            if (applyKws.some(k => text.includes(k) || label.includes(k))) return el;
            // Exact text "Apply" is also valid
            if (text === 'apply' || label === 'apply') return el;
        }
        return null;
    }""")
    if el_handle:
        el = el_handle.as_element()
        if el:
            return el

    # Strategy 4: scroll back to top (LinkedIn sometimes puts button in sticky top bar)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(1000)
    el_handle2 = await page.evaluate_handle("""() => {
        for (const el of document.querySelectorAll('button')) {
            if (!el.offsetParent && el.offsetHeight === 0) continue;
            const t = (el.innerText || '').toLowerCase().trim();
            const a = (el.getAttribute('aria-label') || '').toLowerCase();
            if (t.includes('apply') || a.includes('apply')) return el;
        }
        return null;
    }""")
    if el_handle2:
        el = el_handle2.as_element()
        if el:
            return el

    return None


async def apply_to_job(page, job, resume_path=None, extra_answers=None):
    """Apply to a LinkedIn-hosted job. Returns (success: bool, external_url: str|None)."""
    try:
        await page.goto(job["url"], wait_until="load", timeout=30000)
        await page.wait_for_timeout(3000)

        # Detect session expiry / CAPTCHA wall before wasting time looking for apply button
        if not await _is_session_alive(page):
            print(f"  Session expired on job page — aborting batch")
            raise SessionExpiredError("LinkedIn session expired mid-run")

        # If LinkedIn served a verification or empty page, bail early
        page_text = await page.evaluate("() => document.body.innerText")
        if len(page_text.strip()) < 200 or "Join now" in page_text or "Sign in" in page_text:
            print(f"  Job page looks like a login wall for {job['title']}")
            raise SessionExpiredError("LinkedIn showing login wall on job page")

        apply_btn = await _find_linkedin_apply_button(page)

        if not apply_btn:
            all_btns = await page.evaluate(
                "() => Array.from(document.querySelectorAll('button, a[role=button]'))"
                ".map(b => (b.getAttribute('aria-label') || b.innerText || '').trim())"
                ".filter(t => t && t.length < 80).slice(0, 25)"
            )
            print(f"  No apply button for {job['title']}. All buttons: {all_btns}")
            markup = {"inline_keyboard": [[{"text": "🔍 Open Job", "url": job['url']}]]}
            send_telegram(
                f"⚠️ *Apply button not found*\n"
                f"*{job['title']}* at {job['company']}\n"
                f"Buttons on page: `{str(all_btns[:10])}`\n"
                f"Tap to open and apply manually 👇",
                reply_markup=markup
            )
            return False, None

        is_easy_apply = "easy" in ((await apply_btn.get_attribute("aria-label")) or "").lower()
        print(f"  {'Easy Apply' if is_easy_apply else 'Apply'} button found for {job['title']}, clicking...")

        # Listen for a new tab — non-Easy Apply buttons open the company site in a popup
        new_tab_url = None
        new_tab_holder = []

        def _on_new_page(p):
            new_tab_holder.append(p)

        page.context.once("page", _on_new_page)
        await apply_btn.click()
        await page.wait_for_timeout(4000)

        if new_tab_holder:
            try:
                new_tab = new_tab_holder[0]
                await new_tab.wait_for_load_state("domcontentloaded", timeout=5000)
                new_tab_url = new_tab.url
                await new_tab.close()
            except Exception:
                pass

        await page.wait_for_timeout(1000)

        # New tab opened → third-party application site
        if new_tab_url and "linkedin.com" not in new_tab_url:
            print(f"  Opened in new tab: {new_tab_url}")
            return False, new_tab_url

        # Same-page redirect away from LinkedIn
        if "linkedin.com" not in page.url:
            print(f"  Redirected to: {page.url}")
            return False, page.url

        # Wait up to 5s for LinkedIn's Easy Apply modal to appear
        # LinkedIn uses .artdeco-modal (design system), not role="dialog"
        _MODAL_SELECTORS = [
            ".artdeco-modal",
            ".jobs-easy-apply-modal",
            "[aria-modal='true']",
            "[data-test-modal]",
            '[role="dialog"]',
        ]
        modal_sel = None
        for sel in _MODAL_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=10000, state="visible")
                modal_sel = sel
                break
            except Exception:
                pass

        if not modal_sel:
            all_btns = await page.evaluate(
                "() => Array.from(document.querySelectorAll('button'))"
                ".map(b => (b.getAttribute('aria-label') || b.innerText || '').trim())"
                ".filter(t => t && t.length < 80).slice(0, 15)"
            )
            print(f"  No Easy Apply modal for {job['title']}. Buttons: {all_btns}")
            send_telegram(
                f"⚠️ *Easy Apply modal didn't open*\n"
                f"*{job['title']}* at {job['company']}\n"
                f"Buttons visible: `{all_btns[:8]}`\n"
                f"Session may have expired — try /login if this keeps happening."
            )
            return False, None
        print(f"  Modal found via: {modal_sel}")

        # Click through all Easy Apply steps using LinkedIn's pre-filled data
        from form_filler import fill_form_step, find_required_unfilled, fill_by_asking_user
        review_count = 0
        resume_uploaded = False
        last_url_hash = ""
        stuck_count   = 0

        for step in range(30):
            print(f"  Step {step}: start")
            await page.wait_for_timeout(1500)

            # Upload resume if this step has a file input and we haven't uploaded yet
            if resume_path and not resume_uploaded:
                try:
                    file_input = await page.query_selector('[role="dialog"] input[type="file"]')
                    if file_input:
                        await file_input.set_input_files(resume_path)
                        await page.wait_for_timeout(3000)  # wait for dynamic fields to render
                        resume_uploaded = True
                        print(f"  [resume] Uploaded: {Path(resume_path).name}")
                except Exception as e:
                    print(f"  Resume upload error: {e}")

            # Fill any empty required fields on this step before clicking Next
            await fill_form_step(page, job=job)
            await page.wait_for_timeout(500)

            # After hitting Review twice without Submit, scroll inside the dialog
            if review_count >= 2:
                await page.evaluate("""() => {
                    const d = document.querySelector('[role="dialog"]');
                    if (d) d.scrollTop = d.scrollHeight;
                }""")
                await page.wait_for_timeout(500)

            # LinkedIn Easy Apply buttons have specific aria-labels — match those directly
            # (no need to scope to modal; these labels don't exist in the nav)
            clicked = None
            for aria_label, action in [
                ("Submit application",       "submit"),
                ("Review your application",  "review"),
                ("Continue to next step",    "next"),
                ("Done",                     "done"),
            ]:
                try:
                    btn = page.get_by_role("button", name=aria_label, exact=True)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click()
                        clicked = action
                        break
                except Exception:
                    pass

            # Fallback: search inside the modal for any button matching keywords
            if clicked is None:
                clicked = await page.evaluate(f"""() => {{
                    const modal = document.querySelector('{modal_sel}');
                    if (!modal) return null;
                    const kws = [
                        ['submit application', 'submit'],
                        ['review', 'review'],
                        ['continue to next step', 'next'],
                        ['next', 'next'],
                        ['continue', 'next'],
                        ['done', 'done'],
                    ];
                    for (const btn of modal.querySelectorAll('button')) {{
                        if (btn.disabled) continue;
                        const t = (btn.innerText || '').trim().toLowerCase();
                        const a = (btn.getAttribute('aria-label') || '').toLowerCase();
                        for (const [kw, act] of kws) {{
                            if (t === kw || a.includes(kw)) {{ btn.click(); return act; }}
                        }}
                    }}
                    // Last resort: rightmost enabled button in modal footer
                    const footer = modal.querySelector('footer, .artdeco-modal__actionbar, [class*="footer"], [class*="action"]');
                    const scope = footer || modal;
                    const btns = Array.from(scope.querySelectorAll('button')).filter(b => !b.disabled && b.offsetParent);
                    if (btns.length > 0) {{ btns[btns.length - 1].click(); return 'next'; }}
                    return null;
                }}""")

            print(f"  Step {step}: clicked={clicked}")
            if clicked == 'review':
                review_count += 1

            if clicked == 'submit':
                await page.wait_for_timeout(2000)
                print(f"  Applied to {job['title']} at {job['company']}")
                return True, None

            if clicked == 'done':
                print(f"  Applied to {job['title']} at {job['company']}")
                return True, None

            if clicked is None:
                all_btns = await page.evaluate(f"""() => {{
                    const d = document.querySelector('{modal_sel}') || document;
                    return Array.from(d.querySelectorAll('button'))
                        .map(b => (b.getAttribute('aria-label') || b.innerText || '').trim())
                        .filter(t => t).slice(0, 15);
                }}""")
                print(f"  Step {step}: no action button in modal — {all_btns}")
                break

            # Stuck detection: if the modal didn't advance after clicking Next, check what's blocking
            await page.wait_for_timeout(800)
            cur_hash = await page.evaluate("() => document.querySelector('[role=dialog]')?.innerText?.slice(0,120) || ''")
            if cur_hash == last_url_hash and clicked in ("next", "review"):
                stuck_count += 1
                if stuck_count >= 2:
                    blocking = await find_required_unfilled(page)
                    if blocking:
                        # Ask you in Telegram for the missing fields and retry once
                        asked = await fill_by_asking_user(page, blocking, job["title"], job["company"])
                        if asked:
                            stuck_count = 0
                            last_url_hash = ""
                            continue
                        # No reply in time — save for retry when you answer later
                        mark_needs_answer(job["id"], blocking)
                        send_telegram(
                            f"⏰ *Waiting for your reply*\n\n"
                            f"*{job['title']}* at {job['company']}\n"
                            f"Required fields I couldn't fill:\n"
                            + "\n".join(f"• `{f}`" for f in blocking[:8])
                            + f"\n\nReply here with your answer — I'll retry the application automatically once I get it."
                        )
                    break
            else:
                stuck_count = 0
            last_url_hash = cur_hash

        return False, None

    except Exception as e:
        import traceback
        print(f"  [APPLY ERROR] {job['title']}: {e}")
        print(traceback.format_exc())
        return False, None


def _generate_recruiter_note(first_name: str, job_title: str, company: str) -> str:
    """Use Claude to write a short, genuine LinkedIn connection note. Max 300 chars."""
    try:
        client = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a short LinkedIn connection note (under 280 characters, no hashtags) "
                    f"from Harshith, a software developer in Dublin with 2 years of experience in "
                    f".NET Core, Python, AWS, Docker, and DevOps. He just applied for the "
                    f"'{job_title}' role at {company}. Address it to {first_name}. "
                    f"Sound genuine and brief — no buzzwords, no flattery. Just a human note."
                ),
            }],
        )
        note = msg.content[0].text.strip()
        return note[:280]
    except Exception as e:
        print(f"  Note generation error: {e}")
        return (
            f"Hi {first_name}, I just applied for the {job_title} role at {company}. "
            f"I'm a software developer based in Dublin with experience in AWS, .NET, and DevOps. "
            f"Would love to connect!"
        )[:280]


async def find_and_connect_recruiter(page, job):
    """Search for recruiter/hiring manager at the company and send a connection request."""
    try:
        company_slug = job["company"].lower().replace(" ", "%20")
        search_url = (
            f"https://www.linkedin.com/search/results/people/?"
            f"keywords=recruiter+{company_slug}&origin=GLOBAL_SEARCH_HEADER"
        )
        await page.goto(search_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Find first person result with Connect button
        cards = await page.query_selector_all(".reusable-search__result-container")
        for card in cards[:5]:
            try:
                connect_btn = await card.query_selector("button[aria-label*='Connect']")
                name_el     = await card.query_selector(".entity-result__title-text")
                if not connect_btn or not name_el:
                    continue

                name       = (await name_el.inner_text()).strip().split("\n")[0]
                first_name = name.split()[0]
                await connect_btn.click()
                await page.wait_for_timeout(1500)

                # Add a Claude-generated personalised note
                note_btn = await page.query_selector("button[aria-label='Add a note']")
                if note_btn:
                    await note_btn.click()
                    await page.wait_for_timeout(1000)
                    note_field = await page.query_selector("textarea#custom-message")
                    if note_field:
                        note_text = _generate_recruiter_note(first_name, job["title"], job["company"])
                        await note_field.fill(note_text)

                send_btn = await page.query_selector("button[aria-label='Send now']")
                if send_btn:
                    await send_btn.click()
                    await page.wait_for_timeout(1000)
                    return name

                # Close modal if send failed
                close = await page.query_selector("button[aria-label='Dismiss']")
                if close:
                    await close.click()

            except Exception:
                continue

        return None
    except Exception as e:
        print(f"  Recruiter search error: {e}")
        return None


# ── JD Relevance Scoring ──────────────────────────────────────────────────────

def score_jobs_batch(jobs: list) -> dict:
    """Batch-score job titles with Claude. Returns {job_id: score 1-10}."""
    if not jobs:
        return {}
    client  = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    catalog = "\n".join(f"{i}. {j['title']} at {j['company']}" for i, j in enumerate(jobs))
    good = ", ".join(MY_INTERESTS["good_fit"])
    bad  = ", ".join(MY_INTERESTS["bad_fit"])
    stack = ", ".join(MY_INTERESTS["stack"])
    prompt  = f"""Score each job 1-10 for {MY_INTERESTS['name']}, a {MY_INTERESTS['role']} in {MY_INTERESTS['location']} ({MY_INTERESTS['exp_years']} yrs exp).
Stack: {stack}.

High score (8-10): Strong match — {good}.
Medium score (5-7): Related but partial — other languages, slightly senior.
Low score (1-4): Poor fit — {bad}.

Jobs:
{catalog}

Reply ONLY with a JSON array of integers (one score per job, same order). Example: [8, 3, 9, 5, 7]"""
    try:
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        scores = json.loads(resp.content[0].text.strip())
        return {jobs[i]["id"]: scores[i] for i in range(min(len(jobs), len(scores)))}
    except Exception as e:
        print(f"Batch scoring error: {e}")
        return {j["id"]: AUTO_APPLY_SCORE_THRESHOLD for j in jobs}  # fallback: pass all


async def scrape_jd_from_page(page, job_url: str) -> str:
    """Navigate to a LinkedIn job page and return the job description text."""
    try:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        if "login" in page.url or "authwall" in page.url:
            raise SessionExpiredError("Redirected to login while scraping JD")

        # Try clicking "Show more" to expand truncated descriptions
        try:
            for expand_sel in [
                "button[aria-label*='more']",
                "button[aria-label*='See more']",
                "button.jobs-description__footer-button",
                "button:has-text('Show more')",
                "button:has-text('See more')",
            ]:
                btn = await page.query_selector(expand_sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(600)
                    break
        except Exception:
            pass

        # Strategy 1: known CSS selectors
        _jd_selectors = [
            "#job-details", ".jobs-description__content",
            ".jobs-description-content__text", ".jobs-box__html-content",
            ".description__text", "[data-job-description]",
            ".jobs-details__main-content", ".job-description",
        ]
        text = ""
        for sel in _jd_selectors:
            el = await page.query_selector(sel)
            if el:
                t = (await el.inner_text()).strip()
                if len(t) > len(text):
                    text = t

        # Strategy 2: full page body text (always works)
        if len(text) < 200:
            body = await page.evaluate("() => document.body.innerText")
            # Skip the top nav (first ~300 chars) and take a 4000-char window
            text = body[300:4300].strip()
            print(f"  [JD] Used body fallback ({len(text)} chars)")
        else:
            print(f"  [JD] Found via selector ({len(text)} chars)")

        if text and len(text.strip()) > 100:
            return text.strip()[:3000]

    except SessionExpiredError:
        raise
    except Exception as e:
        print(f"JD scrape error: {e}")
    return ""


def score_job_by_jd(title: str, company: str, jd: str) -> int:
    """Score a single job 1-10 using its full description. Returns int."""
    client = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    good  = ", ".join(MY_INTERESTS["good_fit"])
    bad   = ", ".join(MY_INTERESTS["bad_fit"])
    stack = ", ".join(MY_INTERESTS["stack"])
    prompt = f"""Score this job 1-10 for {MY_INTERESTS['name']} ({MY_INTERESTS['role']}, {MY_INTERESTS['location']}, {MY_INTERESTS['exp_years']} yrs exp).
Stack: {stack}.

Job: {title} at {company}
Description:
{jd[:2000]}

Scoring:
9-10: Perfect fit — {good}, entry/junior/mid level, visa sponsorship available or not mentioned
7-8: Good fit, most skills match
5-6: Partial fit, different but related stack or slightly senior
1-4: Poor fit — {bad}

Reply with a single integer only."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=8,
            messages=[{"role": "user", "content": prompt}]
        )
        return int(resp.content[0].text.strip())
    except Exception as e:
        print(f"JD scoring error: {e}")
        return 7  # pass if scoring fails


def _save_relevance_score(job_id: str, score: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE jobs SET relevance_score=? WHERE id=?", (score, job_id))
    conn.commit()
    conn.close()


def _save_jd(job_id: str, jd: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE jobs SET description=? WHERE id=?", (jd[:4000], job_id))
    conn.commit()
    conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

_HEADLESS_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1280,800",
    "--disable-features=IsolateOrigins,site-per-process",
]
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


async def _get_authenticated_browser(p):
    """Return (browser, context, page) with a valid LinkedIn session, auto-relogin if expired."""
    async def _make_browser():
        b = await p.chromium.launch(headless=False, args=_HEADLESS_ARGS)
        c = await b.new_context(user_agent=_USER_AGENT, viewport={"width": 1280, "height": 800})
        await c.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        pg = await c.new_page()
        return b, c, pg

    browser, context, page = await _make_browser()
    session_loaded = await load_session(context)

    if not session_loaded:
        await browser.close()
        await login_linkedin_visible()
        browser, context, page = await _make_browser()
        await load_session(context)
        return browser, context, page

    await page.goto("https://www.linkedin.com/feed")
    await page.wait_for_timeout(2000)
    if "login" in page.url or "authwall" in page.url:
        print("Session expired — re-logging in...")
        await browser.close()
        await login_linkedin_visible()
        browser, context, page = await _make_browser()
        await load_session(context)

    return browser, context, page


async def find_jobs():
    """Find new Easy Apply jobs and send to Telegram for approval."""
    init_db()
    new_jobs = 0

    async with async_playwright() as p:
        browser, context, page = await _get_authenticated_browser(p)

        # Pass 1: collect all unique new jobs across keywords
        candidates = {}
        for keyword in JOB_KEYWORDS:
            print(f"Searching: {keyword}...")
            jobs = await search_jobs(page, keyword)
            for job in jobs:
                if not already_seen(job["id"]) and job["id"] not in candidates:
                    candidates[job["id"]] = job

        if not candidates:
            await browser.close()
            send_telegram("💼 *Job Search Complete*\nNo new jobs found.")
            return

        candidate_list = list(candidates.values())
        print(f"Collected {len(candidate_list)} new candidates — scoring titles...")

        # Pass 2: batch-score all titles; rough pre-filter at threshold-1 to reduce JD scrapes
        title_scores  = score_jobs_batch(candidate_list)
        title_cutoff  = max(1, AUTO_APPLY_SCORE_THRESHOLD - 1)
        passed_title  = [j for j in candidate_list if title_scores.get(j["id"], 0) >= title_cutoff]
        skipped_count = len(candidate_list) - len(passed_title)

        print(f"Title filter: {len(passed_title)} pass, {skipped_count} skipped (score < {title_cutoff})")
        if skipped_count > 0:
            send_telegram(
                f"🔍 *Job Search*\nFound {len(candidate_list)} new jobs.\n"
                f"Filtered *{skipped_count}* irrelevant roles (score < {title_cutoff}).\n"
                f"Scraping JDs for *{len(passed_title)}* candidates..."
            )

        # Pass 3: scrape JD + final score for each title-passing job
        for job in passed_title:
            title_score = title_scores.get(job["id"], AUTO_APPLY_SCORE_THRESHOLD)
            save_job(job)

            jd = await scrape_jd_from_page(page, job["url"])
            if jd:
                _save_jd(job["id"], jd)
                final_score = score_job_by_jd(job["title"], job["company"], jd)
            else:
                final_score = title_score
                print(f"  [warn] No JD scraped for {job['title']} — using title score ({title_score})")

            _save_relevance_score(job["id"], final_score)

            if final_score < AUTO_APPLY_SCORE_THRESHOLD:
                print(f"  Skipped (score {final_score}/10): {job['title']} at {job['company']}")
                update_job_status(job["id"], "skipped")
                continue

            print(f"  Sending (score {final_score}/10): {job['title']} at {job['company']}")
            send_job_for_approval(job, score=final_score)
            new_jobs += 1
            await asyncio.sleep(1)

        await browser.close()

    if new_jobs == 0:
        send_telegram("💼 *Job Search Complete*\nNo relevant jobs found (all scored below 7/10).")
    else:
        send_telegram(f"💼 Found *{new_jobs} relevant jobs*! Review above and tap ✅ Apply or ❌ Skip.")


async def apply_approved():
    """Apply to all Telegram-approved jobs."""
    init_db()
    approved = get_pending_jobs()

    if not approved:
        send_telegram("📋 No approved jobs to apply to yet. Use /findjobs first, then tap ✅ on jobs you want.")
        return

    send_telegram(f"🚀 Applying to *{len(approved)} approved jobs*...")

    async with async_playwright() as p:
        browser, context, page = await _get_authenticated_browser(p)

        applied = 0
        for job in approved:
            # 1. Tailor resume for this specific job (uses cached JD, no browser needed)
            category    = categorize_job(job["title"])
            resume_path = get_resume_path(category)  # fallback static PDF
            try:
                from resume_tailor import tailor_for_job
                tailored_text, tailored_pdf = tailor_for_job(job)
                if tailored_pdf:
                    resume_path = tailored_pdf  # use ATS-tailored PDF
                    send_telegram(
                        f"📄 *ATS-Tailored Resume*\n"
                        f"*{job['title']}* @ {job['company']}\n"
                        f"_{Path(tailored_pdf).name}_"
                    )
            except Exception as e:
                print(f"  Resume tailor error: {e}")

            print(f"  Category: {category} | Resume: {Path(resume_path).name if resume_path else 'none'}")
            try:
                success, external_url = await apply_to_job(page, job, resume_path=resume_path)
                status = "applied" if success else "failed"
            except SessionExpiredError:
                send_telegram(
                    f"⚠️ *LinkedIn session expired mid-run*\n"
                    f"Stopped after {applied} applications.\n"
                    f"Run `python linkedin_apply.py login` to re-authenticate, then try again."
                )
                await browser.close()
                return
            except BaseException as e:
                print(f"  Apply error for {job['title']}: {e}")
                status = "failed"
                success = False
                external_url = None
            update_job_status(job["id"], status, category=category)

            if success:
                applied += 1
                send_telegram(f"✅ Applied: *{job['title']}* at {job['company']} `[{category}]`")

                # 3. Recruiter outreach
                try:
                    recruiter = await find_and_connect_recruiter(page, job)
                    if recruiter:
                        conn = sqlite3.connect(DB_PATH)
                        conn.execute(
                            "UPDATE jobs SET recruiter=? WHERE id=?",
                            (recruiter, job["id"])
                        )
                        conn.commit()
                        conn.close()
                        send_telegram(f"🤝 Connection request sent to recruiter at *{job['company']}*")
                except Exception as e:
                    print(f"  Recruiter outreach error: {e}")
            elif external_url:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE jobs SET external_url=? WHERE id=?", (external_url, job["id"]))
                conn.commit()
                conn.close()
                # Try universal ATS auto-apply before falling back to manual
                try:
                    ats_success = await try_ats_apply(context, external_url, job)
                except Exception as e:
                    print(f"  ATS error: {e}")
                    ats_success = False
                if ats_success:
                    applied += 1
                    update_job_status(job["id"], "applied", category=category)
                    send_telegram(f"✅ Applied via ATS: *{job['title']}* at {job['company']} `[{category}]`")
                else:
                    update_job_status(job["id"], "needs_manual", category=category)
                    score_info = f" ⭐{job.get('relevance_score', '')}" if job.get("relevance_score") else ""
                    markup = {"inline_keyboard": [[{"text": "🌐 Apply Manually Now", "url": external_url}]]}
                    send_telegram(
                        f"👆 *Manual Apply Needed*\n\n"
                        f"*{job['title']}*{score_info}\n"
                        f"🏢 {job['company']}\n"
                        f"📍 {job.get('location', 'Ireland')}\n"
                        f"🏷 `[{category}]`\n\n"
                        f"Auto-apply couldn't complete this one. Tap below to finish it yourself 👇",
                        reply_markup=markup
                    )
            else:
                send_telegram(f"⚠️ Could not apply to *{job['title']}* at {job['company']} — no apply button found.")

            await asyncio.sleep(5)

        await browser.close()

    send_telegram(f"🎉 Done! Applied to *{applied}/{len(approved)}* jobs.")


def poll_approvals():
    """Poll Telegram for ✅/❌ button taps."""
    offset = None
    print("Polling for approvals (30 seconds)...")
    end = time.time() + 30

    while time.time() < end:
        params = {"timeout": 5, "allowed_updates": ["callback_query"]}
        if offset:
            params["offset"] = offset
        resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=10)
        if resp.ok:
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                handle_callback(update)
        time.sleep(2)


async def auto_apply():
    """Find jobs and apply immediately — no Telegram approval step."""
    init_db()

    async with async_playwright() as p:
        browser, context, page = await _get_authenticated_browser(p)

        # Phase 1: collect all unique new jobs
        candidates = {}
        for keyword in JOB_KEYWORDS:
            jobs = await search_jobs(page, keyword)
            for job in jobs:
                if not already_seen(job["id"]) and job["id"] not in candidates:
                    candidates[job["id"]] = job

        if not candidates:
            send_telegram("💼 *Auto Apply*\nNo new jobs found.")
            await browser.close()
            return

        candidate_list = list(candidates.values())

        # Title scoring — batch-score all titles in one Claude call (fast, no browser needed)
        title_scores = score_jobs_batch(candidate_list)
        new_jobs = []
        for job in candidate_list:
            score = title_scores.get(job["id"], AUTO_APPLY_SCORE_THRESHOLD)
            save_job(job)
            _save_relevance_score(job["id"], score)
            if score < AUTO_APPLY_SCORE_THRESHOLD:
                print(f"  Skipped (score {score}/10): {job['title']}")
                update_job_status(job["id"], "skipped")
                continue
            update_job_status(job["id"], "approved")
            job["relevance_score"] = score
            new_jobs.append(job)

        skipped_count = len(candidate_list) - len(new_jobs)
        send_telegram(
            f"💼 *Auto Apply*\nFound *{len(candidate_list)} new jobs*.\n"
            f"Filtered *{skipped_count}* irrelevant (score < {AUTO_APPLY_SCORE_THRESHOLD}).\n"
            f"Applying to *{len(new_jobs)}* matching roles..."
        )

        if not new_jobs:
            await browser.close()
            send_telegram(f"💼 *Auto Apply*\nAll candidates scored below {AUTO_APPLY_SCORE_THRESHOLD}/10 — nothing applied.")
            return

        send_telegram(f"🚀 *{len(new_jobs)} relevant jobs* (score >= 7) — applying now...")

        # Phase 2: tailor + apply
        applied = 0
        for job in new_jobs:
            category    = categorize_job(job["title"])
            resume_path = get_resume_path(category)  # fallback static PDF
            try:
                from resume_tailor import tailor_for_job
                _, tailored_pdf = tailor_for_job(job)
                if tailored_pdf:
                    resume_path = tailored_pdf
            except Exception as e:
                print(f"  Tailor error: {e}")
            print(f"  Category: {category} | Resume: {Path(resume_path).name if resume_path else 'none'}")
            try:
                success, external_url = await apply_to_job(page, job, resume_path=resume_path)
                status = "applied" if success else "failed"
            except SessionExpiredError as e:
                send_telegram(
                    f"⚠️ *LinkedIn session expired mid-run*\n"
                    f"Stopped after {applied} applications.\n"
                    f"Run `python linkedin_apply.py login` to re-authenticate, then try again."
                )
                await browser.close()
                return
            except BaseException as e:
                print(f"  Apply error for {job['title']}: {e}")
                status = "failed"
                success = False
                external_url = None
            update_job_status(job["id"], status, category=category)

            score_tag = f" ⭐{job.get('relevance_score', '')}" if job.get("relevance_score") else ""
            if success:
                applied += 1
                send_telegram(f"✅ Applied: *{job['title']}* at {job['company']} `[{category}]{score_tag}`")
                try:
                    recruiter = await find_and_connect_recruiter(page, job)
                    if recruiter:
                        conn = sqlite3.connect(DB_PATH)
                        conn.execute("UPDATE jobs SET recruiter=? WHERE id=?", (recruiter, job["id"]))
                        conn.commit()
                        conn.close()
                        send_telegram(f"🤝 Recruiter outreach sent at *{job['company']}*")
                except Exception as e:
                    print(f"  Recruiter error: {e}")
            elif external_url:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE jobs SET external_url=? WHERE id=?", (external_url, job["id"]))
                conn.commit()
                conn.close()
                try:
                    ats_success = await try_ats_apply(context, external_url, job)
                except Exception as e:
                    print(f"  ATS error: {e}")
                    ats_success = False
                if ats_success:
                    applied += 1
                    update_job_status(job["id"], "applied", category=category)
                    send_telegram(f"✅ Applied via ATS: *{job['title']}* at {job['company']} `[{category}]{score_tag}`")
                else:
                    update_job_status(job["id"], "needs_manual", category=category)
                    score_info = f" ⭐{job.get('relevance_score', '')}" if job.get("relevance_score") else ""
                    markup = {"inline_keyboard": [[{"text": "🌐 Apply Manually Now", "url": external_url}]]}
                    send_telegram(
                        f"👆 *Manual Apply Needed*\n\n"
                        f"*{job['title']}*{score_info}\n"
                        f"🏢 {job['company']}\n"
                        f"📍 {job.get('location', 'Ireland')}\n"
                        f"🏷 `[{category}]`\n\n"
                        f"Auto-apply couldn't complete this one. Tap below to finish it yourself 👇",
                        reply_markup=markup
                    )
            else:
                send_telegram(f"⚠️ Skipped *{job['title']}* at {job['company']} — no apply button found.")

            await asyncio.sleep(5)

        await browser.close()
    send_telegram(f"🎉 Done! *{applied}/{len(new_jobs)}* applications submitted.")


async def retry_needs_answer():
    """Re-attempt all jobs marked needs_answer (cached answers are now available)."""
    init_db()
    jobs = get_needs_answer_jobs()
    if not jobs:
        send_telegram("No stalled applications to retry.")
        return

    send_telegram(f"🔄 Retrying *{len(jobs)}* stalled application(s)...")

    async with async_playwright() as p:
        browser, context, page = await _get_authenticated_browser(p)

        for job in jobs:
            category    = job.get("category") or categorize_job(job["title"])
            resume_path = get_resume_path(category)
            score_tag   = f" ⭐{job['relevance_score']}" if job.get("relevance_score") else ""
            try:
                success, external_url = await apply_to_job(page, job, resume_path=resume_path)
            except BaseException as e:
                print(f"  Retry error for {job['title']}: {e}")
                success, external_url = False, None

            if success:
                update_job_status(job["id"], "applied", category=category)
                send_telegram(f"✅ Applied: *{job['title']}* at {job['company']}{score_tag}")
            elif external_url:
                update_job_status(job["id"], "external")
                markup = {"inline_keyboard": [[{"text": "🌐 Apply on Site", "url": external_url}]]}
                score_info = f" ⭐{job.get('relevance_score', '')}" if job.get("relevance_score") else ""
                send_telegram(
                    f"🔗 *External Application*\n\n"
                    f"*{job['title']}*{score_info}\n"
                    f"🏢 {job['company']}\n"
                    f"📍 {job.get('location', 'Ireland')}\n"
                    f"🏷 `[{category}]`\n\n"
                    f"LinkedIn Easy Apply not available — tap below to apply on their site 👇",
                    reply_markup=markup
                )
            else:
                update_job_status(job["id"], "failed")
                markup = {"inline_keyboard": [[{"text": "🌐 Apply Manually", "url": job["url"]}]]}
                send_telegram(
                    f"❌ *Still couldn't apply*\n\n"
                    f"*{job['title']}* at {job['company']}\n"
                    f"Please apply manually 👇",
                    reply_markup=markup
                )

            await asyncio.sleep(3)

        await browser.close()


async def do_login():
    """Open visible browser, auto-fill credentials and save session."""
    await login_linkedin_visible()


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "find"

    if cmd == "find":
        asyncio.run(find_jobs())
    elif cmd == "apply":
        asyncio.run(apply_approved())
    elif cmd == "autoapply":
        asyncio.run(auto_apply())
    elif cmd == "retry":
        asyncio.run(retry_needs_answer())
    elif cmd == "poll":
        poll_approvals()
    elif cmd == "login":
        asyncio.run(do_login())
