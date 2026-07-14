#!/usr/bin/env python3
"""
LinkedIn Easy Apply form auto-filler.
Runs on each step of the application modal and fills any empty required fields.
Unknown required fields are asked to the user one-by-one via Telegram.
"""

from __future__ import annotations  # allow PEP-604 (str | None) annotations on Python 3.9

import asyncio
import json
import sys
import time
import requests
from pathlib import Path
from dotenv import dotenv_values

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

config = dotenv_values(Path.home() / ".env")

# ── Applicant profile — edit these or set in ~/.env ──────────────────────────

PROFILE = {
    # ── Identity: single source of truth for who the bot acts as ──
    # Used across linkedin_apply, linkedin_post, linkedin_network, linkedin_feed,
    # linkedin_jobs and tech_digest. Override any of these via ~/.env.
    "first_name":          config.get("APPLICANT_FIRST_NAME", "Harshith"),
    "last_name":           config.get("APPLICANT_LAST_NAME",  "Mittapally"),
    "full_name":           config.get("APPLICANT_FULL_NAME",  "Harshith Mittapally"),
    "email":               config.get("APPLICANT_EMAIL",      "harshithreddy200811@gmail.com"),
    "headline":            config.get("APPLICANT_HEADLINE",
                                      "software developer in Dublin with 2 years of experience "
                                      "in .NET Core, Python, AWS, Docker, and DevOps"),
    "skills_short":        config.get("APPLICANT_SKILLS",     ".NET Core, AWS, Kafka, and Kubernetes"),
    "linkedin_person_urn": config.get("LINKEDIN_PERSON_URN",  "urn:li:person:1W-w0LYy9S"),
    "phone":               config.get("PHONE_NUMBER",        "+353899879815"),
    "city":                config.get("APPLICANT_CITY",      "Dublin"),
    "country":             config.get("APPLICANT_COUNTRY",   "Ireland"),
    "linkedin_url":        config.get("LINKEDIN_URL",        ""),
    "github_url":          config.get("GITHUB_URL",          ""),
    "website":             config.get("WEBSITE_URL",         ""),
    "salary":              config.get("SALARY_EXPECTATION",  "50000"),
    "years_exp_total":     config.get("YEARS_EXP_TOTAL",     "2"),
    "years_exp_python":    config.get("YEARS_EXP_PYTHON",    "2"),
    "years_exp_aws":       config.get("YEARS_EXP_AWS",       "1"),
    "years_exp_dotnet":    config.get("YEARS_EXP_DOTNET",    "2"),
    "notice_period":       config.get("NOTICE_PERIOD",       "2 weeks"),
    "work_authorized":     True,   # Legally authorized to work in Ireland (Stamp 1G)
    # Set REQUIRE_SPONSORSHIP=yes in ~/.env when Stamp 1G expires (~2 years)
    "require_sponsorship": config.get("REQUIRE_SPONSORSHIP", "no").lower() == "yes",
    "gender":              "Prefer not to say",
    "ethnicity":           "Prefer not to say",
    "disability":          "No",
    "veteran":             "No",
}

# ── Label → value matching ────────────────────────────────────────────────────

def _match_text_value(label: str) -> str | None:
    """Return a fill value for a text/number input based on its label."""
    t = label.lower()

    if any(k in t for k in ["phone", "mobile", "contact number"]):
        return PROFILE["phone"]
    if "city" in t and "country" not in t:
        return PROFILE["city"]
    if "country" in t:
        return PROFILE["country"]
    if "linkedin" in t and "url" in t:
        return PROFILE["linkedin_url"]
    if any(k in t for k in ["github", "portfolio", "website", "personal url"]):
        return PROFILE["github_url"] or PROFILE["website"]
    if any(k in t for k in ["salary", "compensation", "ctc", "desired pay", "expected pay",
                             "base pay", "annual pay", "package"]):
        return PROFILE["salary"]
    if any(k in t for k in ["notice", "available from", "start date"]):
        return PROFILE["notice_period"]

    # Years of experience — covers "How many years...", "Years of experience with X"
    if any(k in t for k in ["year", "how long", "how many"]) and \
       any(k in t for k in ["experience", "worked with", "using", "work with", "have with"]):
        if "python" in t:
            return PROFILE["years_exp_python"]
        if any(k in t for k in ["aws", "cloud", "amazon web"]):
            return PROFILE["years_exp_aws"]
        if any(k in t for k in [".net", "dotnet", "c#", "csharp"]):
            return PROFILE["years_exp_dotnet"]
        if any(k in t for k in ["docker", "kubernetes", "k8s", "terraform",
                                  "linux", "ubuntu", "ci/cd", "jenkins", "git"]):
            return "2"
        if any(k in t for k in ["kafka", "ansible", "dynatrace", "grafana",
                                  "prometheus", "helm", "elk", "splunk"]):
            return "1"
        if any(k in t for k in ["ai agent", "llm", "langchain", "openai",
                                  "generative", "rag", "fine-tun"]):
            return "1"
        return PROFILE["years_exp_total"]   # anything else → total exp

    # Visa / work status
    if any(k in t for k in ["visa", "immigration", "permit", "stamp"]):
        return "Stamp 1G"

    return None


def _match_radio_answer(question: str) -> str | None:
    """
    Return 'yes' or 'no' for a radio-button question, or None if unknown.
    """
    q = question.lower()

    # Work authorization — always Yes
    if any(k in q for k in [
        "authorized to work", "right to work", "eligible to work",
        "legally permitted", "work permit", "work authorization",
        "legally authorized",
    ]):
        return "yes"

    # Sponsorship — always No
    if any(k in q for k in [
        "sponsorship", "require visa", "need sponsorship",
        "employer sponsorship",
    ]):
        return "no"

    # Positive capability questions — Yes
    if any(k in q for k in [
        "comfortable", "willing to", "able to", "available to",
        "agree to", "consent", "confirm",
    ]):
        return "yes"

    return None


def _match_select_value(label: str) -> str | None:
    """Return a dropdown option hint for a <select> field."""
    t = label.lower()
    if "country" in t:
        return "ireland"
    if any(k in t for k in ["gender", "sex"]):
        return "prefer"
    if any(k in t for k in ["ethnicity", "race"]):
        return "prefer"
    if any(k in t for k in ["disability", "disabled"]):
        return "no"
    if any(k in t for k in ["veteran", "military"]):
        return "no"
    # Years of experience dropdowns — very common on LinkedIn Easy Apply
    if any(k in t for k in ["year", "how long", "how many"]) and \
       any(k in t for k in ["experience", "worked with", "using", "work with", "have with"]):
        if "python" in t:
            return PROFILE["years_exp_python"]
        if any(k in t for k in ["aws", "amazon web", "cloud"]):
            return PROFILE["years_exp_aws"]
        if any(k in t for k in [".net", "dotnet", "c#", "csharp"]):
            return PROFILE["years_exp_dotnet"]
        if any(k in t for k in ["docker", "kubernetes", "k8s", "terraform",
                                  "linux", "ubuntu", "ci/cd", "jenkins", "git"]):
            return "2"
        if any(k in t for k in ["kafka", "ansible", "dynatrace", "grafana",
                                  "prometheus", "helm", "elk", "splunk"]):
            return "1"
        if any(k in t for k in ["ai agent", "llm", "langchain", "openai",
                                  "generative", "rag", "fine-tun"]):
            return "1"
        return PROFILE["years_exp_total"]   # anything else → total exp
    # Notice period / availability
    if any(k in t for k in ["notice", "available", "start date", "when can"]):
        return PROFILE["notice_period"]
    return None


# ── Main filler ───────────────────────────────────────────────────────────────

async def fill_form_step(page, extra_answers: dict | None = None, job: dict | None = None) -> list[str]:
    """
    Scan the current Easy Apply step for unfilled fields and fill them.
    Unknown required fields are asked to the user via Telegram one by one.
    Returns a list of "(label: value)" strings for logging.
    """
    filled = []

    try:
        # ── 1. Text / number / tel / email inputs ─────────────────────────────
        inputs = await page.query_selector_all(
            "input[type='text'], input[type='tel'], input[type='number'], input[type='email']"
        )
        for inp in inputs:
            try:
                # Skip hidden or disabled fields
                if not await inp.is_visible() or not await inp.is_enabled():
                    continue
                current = (await inp.input_value()).strip()
                inp_type = (await inp.get_attribute("type") or "text").lower()
                # Don't skip number inputs with "0" — that's the blank default
                if current and not (inp_type == "number" and current == "0"):
                    continue  # already filled

                label = await _get_label(page, inp)
                value = _match_text_value(label)
                if value:
                    await inp.fill(str(value))
                    filled.append(f"{label}: {value}")
            except Exception:
                continue

        # ── 2. Radio buttons (Yes / No questions) ────────────────────────────
        # Group radios by their shared name attribute
        radio_names_js = """() => {
            const names = new Set();
            document.querySelectorAll('input[type="radio"]').forEach(r => {
                if (r.name) names.add(r.name);
            });
            return Array.from(names);
        }"""
        radio_names = await page.evaluate(radio_names_js)

        for name in radio_names:
            try:
                # Find the question label for this radio group
                first_radio = await page.query_selector(f"input[type='radio'][name='{name}']")
                if not first_radio:
                    continue

                # Already answered?
                any_checked = await page.evaluate(
                    f"() => !!document.querySelector('input[type=\"radio\"][name=\"{name}\"]:checked')"
                )
                if any_checked:
                    continue

                question = await _get_radio_group_label(page, name)
                answer   = _match_radio_answer(question)
                if not answer:
                    continue

                # Find the radio option whose label contains 'yes' or 'no'
                radios = await page.query_selector_all(f"input[type='radio'][name='{name}']")
                for radio in radios:
                    radio_id    = await radio.get_attribute("id") or ""
                    radio_label = await page.query_selector(f"label[for='{radio_id}']")
                    label_text  = (await radio_label.inner_text()).strip().lower() if radio_label else ""

                    if answer in label_text or label_text.startswith(answer):
                        await radio.click()
                        filled.append(f"{question}: {answer}")
                        break
            except Exception:
                continue

        # ── 3. Select / dropdowns ─────────────────────────────────────────────
        selects = await page.query_selector_all("select")
        for sel in selects:
            try:
                if not await sel.is_visible() or not await sel.is_enabled():
                    continue

                current = await sel.input_value()
                if current and current != "Select an option" and current != "":
                    continue

                label = await _get_label(page, sel)
                hint  = _match_select_value(label)
                if not hint:
                    continue

                # Find an option whose text or value contains the hint (numeric prefix match)
                options = await sel.query_selector_all("option")
                best_val = None
                for opt in options:
                    opt_text = (await opt.inner_text()).strip().lower()
                    opt_val  = (await opt.get_attribute("value") or "").lower()
                    if not opt_text or opt_text == "select an option":
                        continue
                    if hint in opt_text or hint in opt_val or opt_text.startswith(hint):
                        best_val = await opt.get_attribute("value")
                        break
                if best_val is not None:
                    await sel.select_option(value=best_val)
                    # Dispatch change event so React/LinkedIn picks up the value
                    await sel.dispatch_event("change")
                    filled.append(f"{label}: {best_val}")
            except Exception:
                continue

        # ── 3b. Combobox / typeahead inputs (LinkedIn custom dropdowns) ────────
        comboboxes = await page.query_selector_all("input[role='combobox']")
        for cb in comboboxes:
            try:
                if not await cb.is_visible() or not await cb.is_enabled():
                    continue
                current = (await cb.input_value()).strip()
                if current:
                    continue
                label = await _get_label(page, cb)
                hint  = _match_select_value(label) or _match_text_value(label)
                if not hint:
                    continue
                # Type the hint to open the dropdown listbox
                await cb.fill(hint)
                await page.wait_for_timeout(600)
                # Click the first matching option in the listbox
                clicked_opt = await page.evaluate(f"""() => {{
                    const lbs = document.querySelectorAll('[role="listbox"], [role="option"]');
                    for (const el of lbs) {{
                        const t = (el.innerText || '').toLowerCase();
                        if (t.includes('{hint.lower()}') || t.startsWith('{hint[0].lower()}')) {{
                            el.click();
                            return t;
                        }}
                    }}
                    // fallback: click first non-empty option
                    const opts = document.querySelectorAll('[role="option"]');
                    if (opts.length > 0) {{ opts[0].click(); return opts[0].innerText; }}
                    return null;
                }}""")
                if clicked_opt:
                    filled.append(f"{label}: {clicked_opt}")
            except Exception:
                continue

        # ── 4. Textareas (cover letter / additional info) ─────────────────────
        textareas = await page.query_selector_all("textarea")
        for ta in textareas:
            try:
                if not await ta.is_visible() or not await ta.is_enabled():
                    continue
                current = (await ta.input_value()).strip()
                if current:
                    continue
                label = await _get_label(page, ta)

                # Provided answer (from /answer retry) takes priority
                filled_by_answer = False
                if extra_answers:
                    for field_label, answer in extra_answers.items():
                        if field_label.lower() in label.lower() or label.lower() in field_label.lower():
                            await ta.fill(answer[:2000])
                            filled.append(f"{label}: [provided answer]")
                            filled_by_answer = True
                            break

                if not filled_by_answer:
                    if any(k in label.lower() for k in ["cover letter", "additional info", "summary", "message", "tell us"]):
                        note = (
                            "I am a software developer based in Dublin, open to opportunities "
                            "across Ireland. I have experience in AWS, .NET Core, Docker, Kubernetes, "
                            "and CI/CD pipelines. I am passionate about cloud engineering and DevOps "
                            "and am actively seeking roles anywhere in Ireland. "
                            "Available to start within 2 weeks."
                        )
                        await ta.fill(note)
                        filled.append(f"{label}: [cover note]")
            except Exception:
                continue

    except Exception as e:
        print(f"  [form_filler] Error: {e}")

    # Ask user for any required fields still empty after rule-based filling
    try:
        still_unfilled = await find_required_unfilled(page)
        if still_unfilled:
            job_title = (job or {}).get("title", "this job")
            company   = (job or {}).get("company", "this company")
            print(f"  [form_filler] Unknown required fields → asking user: {still_unfilled}")
            asked_filled = await fill_by_asking_user(page, still_unfilled, job_title, company)
            filled.extend(asked_filled)
    except Exception as e:
        print(f"  [form_filler] Ask-user error: {e}")

    if filled:
        print(f"  [form_filler] Filled: {filled}")

    return filled


# ── Telegram Q&A system ──────────────────────────────────────────────────────

# Shared file used to pass questions/answers between this subprocess and bot_server.py
QA_FILE = Path.home() / ".apply_qa.json"

# Persistent cache so the same field is never asked twice
ANSWERS_CACHE_FILE = Path.home() / ".field_answers_cache.json"


def _get_cached_answer(label: str) -> str | None:
    """Return a cached answer for this field label, or None if not cached."""
    key = label.lower().strip()
    try:
        if ANSWERS_CACHE_FILE.exists():
            cache = json.loads(ANSWERS_CACHE_FILE.read_text())
            return cache.get(key)
    except Exception:
        pass
    return None


def _save_cached_answer(label: str, answer: str):
    """Persist the answer so it's reused for this field in future applications."""
    key = label.lower().strip()
    try:
        cache = {}
        if ANSWERS_CACHE_FILE.exists():
            cache = json.loads(ANSWERS_CACHE_FILE.read_text())
        cache[key] = answer
        ANSWERS_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _is_years_question(label: str) -> bool:
    t = label.lower()
    return (
        any(k in t for k in ["year", "how long", "how many"]) and
        any(k in t for k in ["experience", "worked with", "using", "work with", "have with"])
    )


def _send_telegram_question(label: str, job_title: str, company: str, question_number: int, total: int) -> int | None:
    """Send a question to the Telegram Jobs topic. Returns the sent message_id."""
    token    = config.get("TELEGRAM_BOT_TOKEN", "")
    group_id = config.get("TELEGRAM_GROUP_ID", "")
    topic_id = config.get("TELEGRAM_TOPIC_JOBS", "")
    if not token or not group_id:
        return None

    is_years = _is_years_question(label)

    text = (
        f"❓ *Application Form* ({question_number}/{total})\n"
        f"*{job_title}* @ *{company}*\n\n"
        f"Field: `{label}`\n\n"
        + ("👇 *Tap a number below:*" if is_years else "👇 *Type your answer as a plain message in this chat*")
    )

    payload = {"chat_id": group_id, "text": text, "parse_mode": "Markdown"}
    if topic_id:
        payload["message_thread_id"] = int(topic_id)

    if is_years:
        import json as _json
        payload["reply_markup"] = _json.dumps({
            "inline_keyboard": [
                [{"text": str(i), "callback_data": f"qa_{i}"} for i in range(0, 5)],
                [{"text": str(i), "callback_data": f"qa_{i}"} for i in range(5, 9)],
                [{"text": "9+", "callback_data": "qa_9"}],
            ]
        })

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=5
        )
        return resp.json().get("result", {}).get("message_id")
    except Exception:
        return None


async def ask_user_via_telegram(label: str, job_title: str, company: str,
                                 question_number: int = 1, total: int = 1,
                                 timeout: int = 120) -> str:
    """
    Ask the user one question via Telegram and wait for their reply.
    Uses ~/.apply_qa.json as a handshake file with bot_server.py.
    Returns the user's answer, or "" on timeout.
    """
    # Check cache first — don't ask the same field twice
    cached = _get_cached_answer(label)
    if cached is not None:
        print(f"  [form_filler] Cached answer for '{label}': {cached}")
        return cached

    # Send the Telegram message first so we get the message_id
    msg_id = _send_telegram_question(label, job_title, company, question_number, total)

    # Write pending question (include msg_id so bot_server can match button taps)
    QA_FILE.write_text(json.dumps({
        "status":   "pending",
        "label":    label,
        "job":      f"{job_title} at {company}",
        "asked_at": time.time(),
        "msg_id":   msg_id,
    }))

    # Poll until answered or timeout
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2)
        try:
            if QA_FILE.exists():
                data = json.loads(QA_FILE.read_text())
                if data.get("status") == "answered":
                    answer = data.get("answer", "").strip()
                    try:
                        QA_FILE.unlink(missing_ok=True)
                    except Exception:
                        pass
                    if answer:
                        _save_cached_answer(label, answer)
                    return answer
        except Exception:
            pass

    # Timed out — keep the file as "waiting" so bot_server can pick up a late reply
    try:
        QA_FILE.write_text(json.dumps({
            "status":   "waiting",
            "label":    label,
            "job":      f"{job_title} at {company}",
            "asked_at": time.time(),
        }))
    except Exception:
        pass
    print(f"  [form_filler] Timed out waiting for answer to '{label}'")
    return ""


async def fill_by_asking_user(page, unfilled_labels: list[str],
                               job_title: str, company: str) -> list[str]:
    """Fill unfilled required fields: auto-fill with known rules first, then ask Telegram."""
    if not unfilled_labels:
        return []

    filled   = []
    ask_user = []

    # Pass 1: try pattern rules again — catches fields that loaded dynamically after the
    # initial fill_form_step scan (e.g. "years of experience" appearing post-resume-upload)
    for label in unfilled_labels:
        auto = _match_text_value(label) or _match_select_value(label)
        if auto:
            success = await _fill_field_by_label(page, label, auto)
            if success:
                print(f"  [form_filler] Auto-filled (late): {label}: {auto}")
                filled.append(f"{label}: {auto}")
                continue
        ask_user.append(label)

    # Pass 2: only ask Telegram for fields that genuinely couldn't be matched
    for i, label in enumerate(ask_user, start=1):
        answer = await ask_user_via_telegram(label, job_title, company, i, len(ask_user))
        if not answer:
            continue
        success = await _fill_field_by_label(page, label, answer)
        if success:
            filled.append(f"{label}: {answer}")

    return filled


async def _fill_field_by_label(page, label: str, value: str) -> bool:
    """Find a form field matching label and fill it with value. Returns True if filled."""
    lbl   = label.lower()
    val   = str(value)

    # Text / number / tel / email inputs
    try:
        for inp in await page.query_selector_all(
            "input[type='text'], input[type='number'], input[type='tel'], input[type='email']"
        ):
            try:
                if not await inp.is_visible() or not await inp.is_enabled():
                    continue
                if (await inp.input_value()).strip():
                    continue
                inp_lbl = (await _get_label(page, inp)).lower()
                if lbl in inp_lbl or inp_lbl in lbl or _fuzzy_match(lbl, inp_lbl):
                    await inp.fill(val)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Textareas
    try:
        for ta in await page.query_selector_all("textarea"):
            try:
                if not await ta.is_visible() or not await ta.is_enabled():
                    continue
                if (await ta.input_value()).strip():
                    continue
                ta_lbl = (await _get_label(page, ta)).lower()
                if lbl in ta_lbl or ta_lbl in lbl or _fuzzy_match(lbl, ta_lbl):
                    await ta.fill(val[:2000])
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Dropdowns
    try:
        for sel in await page.query_selector_all("select"):
            try:
                if not await sel.is_visible() or not await sel.is_enabled():
                    continue
                sel_lbl = (await _get_label(page, sel)).lower()
                if not (lbl in sel_lbl or sel_lbl in lbl or _fuzzy_match(lbl, sel_lbl)):
                    continue
                cur = await sel.input_value()
                if cur and cur not in ("", "Select an option"):
                    continue
                val_lower = val.lower()
                for opt in await sel.query_selector_all("option"):
                    opt_text = (await opt.inner_text()).strip().lower()
                    opt_val  = (await opt.get_attribute("value") or "").lower()
                    if val_lower in opt_text or opt_text.startswith(val_lower[:4]):
                        await sel.select_option(value=await opt.get_attribute("value"))
                        return True
            except Exception:
                continue
    except Exception:
        pass

    # Radio buttons
    try:
        radio_names = await page.evaluate("""() => {
            const s = new Set();
            document.querySelectorAll('input[type="radio"]').forEach(r => { if (r.name) s.add(r.name); });
            return Array.from(s);
        }""")
        val_lower = val.lower()
        for name in radio_names:
            try:
                if await page.evaluate(f'() => !!document.querySelector(\'input[type="radio"][name="{name}"]:checked\')'):
                    continue
                q = (await _get_radio_group_label(page, name)).lower()
                if not (lbl in q or q in lbl or _fuzzy_match(lbl, q)):
                    continue
                for radio in await page.query_selector_all(f"input[type='radio'][name='{name}']"):
                    rid = await radio.get_attribute("id") or ""
                    lel = await page.query_selector(f"label[for='{rid}']")
                    lt  = (await lel.inner_text()).strip().lower() if lel else ""
                    if val_lower in lt or lt.startswith(val_lower[:3]):
                        await radio.click()
                        return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _fuzzy_match(a: str, b: str) -> bool:
    """True if any word > 4 chars in a appears in b."""
    return any(word in b for word in a.split() if len(word) > 4)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_label(page, element) -> str:
    """Get the label text for a form element via id→for, aria-label, or placeholder."""
    try:
        el_id = await element.get_attribute("id")
        if el_id:
            label_el = await page.query_selector(f"label[for='{el_id}']")
            if label_el:
                return (await label_el.inner_text()).strip()

        aria = await element.get_attribute("aria-label")
        if aria:
            return aria.strip()

        placeholder = await element.get_attribute("placeholder")
        if placeholder:
            return placeholder.strip()
    except Exception:
        pass
    return ""


async def find_required_unfilled(page) -> list[str]:
    """Return labels of required fields still empty after auto-fill."""
    unfilled = []
    try:
        inputs = await page.query_selector_all(
            "input[required], input[aria-required='true']"
        )
        for inp in inputs:
            try:
                if not await inp.is_visible() or not await inp.is_enabled():
                    continue
                inp_type = (await inp.get_attribute("type") or "text").lower()
                if inp_type in ("hidden", "file", "submit", "checkbox", "radio"):
                    continue
                if not (await inp.input_value()).strip():
                    label = await _get_label(page, inp)
                    if label:
                        unfilled.append(label)
            except Exception:
                continue

        textareas = await page.query_selector_all(
            "textarea[required], textarea[aria-required='true']"
        )
        for ta in textareas:
            try:
                if not await ta.is_visible() or not await ta.is_enabled():
                    continue
                if not (await ta.input_value()).strip():
                    label = await _get_label(page, ta)
                    if label:
                        unfilled.append(label)
            except Exception:
                continue

        selects = await page.query_selector_all(
            "select[required], select[aria-required='true']"
        )
        for sel in selects:
            try:
                if not await sel.is_visible() or not await sel.is_enabled():
                    continue
                val = await sel.input_value()
                if not val or val in ("Select an option", ""):
                    label = await _get_label(page, sel)
                    if label:
                        unfilled.append(label)
            except Exception:
                continue

    except Exception as e:
        print(f"  [form_filler] find_required_unfilled error: {e}")

    return unfilled


async def _get_radio_group_label(page, name: str) -> str:
    """Get the question text for a radio group by walking up the DOM."""
    try:
        label_js = f"""() => {{
            const radio = document.querySelector('input[type="radio"][name="{name}"]');
            if (!radio) return '';
            let el = radio.parentElement;
            for (let i = 0; i < 6; i++) {{
                if (!el) break;
                const legend = el.querySelector('legend, [data-test-form-element-label]');
                if (legend) return legend.innerText.trim();
                const label = el.querySelector('label');
                if (label && label.htmlFor !== radio.id) return label.innerText.trim();
                el = el.parentElement;
            }}
            return '';
        }}"""
        return await page.evaluate(label_js)
    except Exception:
        return ""
