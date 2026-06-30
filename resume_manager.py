#!/usr/bin/env python3
"""
Resume Manager
- Receives PDF uploaded via Telegram, saves to all resume slots
- Extracts text and regenerates .txt variants via Claude
- Uploads resume PDF to LinkedIn Easy Apply default resume
"""

import asyncio
import json
import shutil
import requests
from pathlib import Path
from dotenv import dotenv_values

config = dotenv_values(Path.home() / ".env")

BASE        = Path(__file__).parent
RESUMES_DIR = BASE / "resumes"
SESSION_FILE = BASE / "linkedin_session.json"

TELEGRAM_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
ANTHROPIC_KEY  = config.get("ANTHROPIC_API_KEY")

# All PDF slots that linkedin_apply.py looks for
PDF_SLOTS = ["resume_cloud", "resume_ml", "resume_swe", "resume_devops"]


def download_telegram_file(file_id: str, dest_path: Path) -> bool:
    """Download a Telegram file by file_id to dest_path. Returns True on success."""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=15
        )
        resp.raise_for_status()
        tg_path = resp.json()["result"]["file_path"]
        data = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{tg_path}",
            timeout=30
        ).content
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return True
    except Exception as e:
        print(f"[resume_manager] Telegram download error: {e}")
        return False


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract plain text from a PDF. Returns empty string on failure."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages).strip()
    except ImportError:
        print("[resume_manager] pypdf not installed — run: pip install pypdf")
        return ""
    except Exception as e:
        print(f"[resume_manager] PDF text extraction error: {e}")
        return ""


def _regenerate_txt_variants(pdf_text: str):
    """Ask Claude to reformat raw PDF text into structured .txt resume variants."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    variants = {
        "resume_devops": "cloud/DevOps/infrastructure focus — emphasise AWS, Kubernetes, Docker, Terraform, CI/CD",
        "resume_fullstack": "full-stack/software engineering focus — emphasise languages, frameworks, APIs, databases",
    }

    for filename, focus in variants.items():
        prompt = f"""Reformat this resume with a {focus}.
Keep ONLY real information from the original — do not invent anything.
Use these section headers exactly: PROFESSIONAL SUMMARY, EXPERIENCE, EDUCATION, SKILLS
Use bullet points starting with • for experience/skills.

RESUME TEXT:
{pdf_text}

Return ONLY the formatted resume text."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        out = response.content[0].text.strip()
        (RESUMES_DIR / f"{filename}.txt").write_text(out + "\n", encoding="utf-8")
        print(f"[resume_manager] Updated {filename}.txt")


def handle_resume_upload(pdf_path: Path) -> tuple[bool, str]:
    """
    Full pipeline after a PDF lands on disk:
    1. Copy to all PDF resume slots
    2. Extract text and regenerate .txt variants
    Returns (success, status_message).
    """
    RESUMES_DIR.mkdir(exist_ok=True)

    # Distribute PDF to all slots
    for slot in PDF_SLOTS:
        dest = RESUMES_DIR / f"{slot}.pdf"
        shutil.copy2(pdf_path, dest)
        print(f"[resume_manager] Saved {dest.name}")

    # Extract text
    text = extract_text_from_pdf(pdf_path)
    if not text:
        return False, "Resume PDFs saved, but could not extract text (pypdf may not be installed — run `pip install pypdf`)."

    # Regenerate .txt variants
    try:
        _regenerate_txt_variants(text)
    except Exception as e:
        return False, f"Resume PDFs saved, but failed to regenerate .txt files: {e}"

    return True, "Resume PDFs saved to all slots and .txt variants updated."


async def upload_to_linkedin_profile(pdf_path: Path) -> tuple[bool, str]:
    """
    Upload the resume PDF to LinkedIn Easy Apply default resume settings.
    Opens a visible browser in case manual verification is needed.
    """
    from playwright.async_api import async_playwright

    cookies = []
    if SESSION_FILE.exists():
        data = json.loads(SESSION_FILE.read_text())
        cookies = data["cookies"] if isinstance(data, dict) and "cookies" in data else data

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        if cookies:
            await context.add_cookies(cookies)
        page = await context.new_page()

        await page.goto("https://www.linkedin.com/jobs/application-settings/", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Detect if we're on a login redirect
        if "login" in page.url or "authwall" in page.url:
            await browser.close()
            return False, "LinkedIn session expired. Run /login to re-authenticate, then try /updateprofile again."

        # Look for existing resume upload button / delete old one first
        try:
            delete_btn = await page.query_selector("button[aria-label*='Delete'], button[aria-label*='Remove']")
            if delete_btn:
                await delete_btn.click()
                await page.wait_for_timeout(1500)
        except Exception:
            pass

        # Upload new resume
        file_input = await page.query_selector("input[type='file'][accept*='pdf'], input[type='file']")
        if not file_input:
            await browser.close()
            return False, "Could not find resume upload field on LinkedIn settings page."

        await file_input.set_input_files(str(pdf_path))
        await page.wait_for_timeout(3000)

        # Click Save/Upload button
        saved = False
        for selector in [
            "button[aria-label*='Save']",
            "button[aria-label*='Upload']",
            "button[data-test-modal-close-btn]",
            "button[type='submit']",
        ]:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(2000)
                saved = True
                break

        await page.wait_for_timeout(2000)
        await browser.close()

        if saved:
            return True, "Resume uploaded to LinkedIn Easy Apply settings."
        return True, "Resume file set — verify in the browser that it saved correctly."


def run_upload_to_linkedin(pdf_path: Path) -> tuple[bool, str]:
    return asyncio.run(upload_to_linkedin_profile(pdf_path))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python resume_manager.py <path/to/resume.pdf>")
        sys.exit(1)
    path = Path(sys.argv[1])
    ok, msg = handle_resume_upload(path)
    print(f"{'OK' if ok else 'FAIL'}: {msg}")
    if ok:
        ok2, msg2 = run_upload_to_linkedin(RESUMES_DIR / "resume_swe.pdf")
        print(f"LinkedIn: {'OK' if ok2 else 'FAIL'}: {msg2}")
