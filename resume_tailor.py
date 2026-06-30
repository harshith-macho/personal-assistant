#!/usr/bin/env python3
"""
Resume Auto-Tailor
- Fetches job description from DB cache (already scraped during find_jobs)
- Uses Claude to rewrite resume to beat ATS for that specific job
- Generates a clean, ATS-parseable PDF and returns its path
"""

import anthropic
import sqlite3
import re
from pathlib import Path
from dotenv import dotenv_values
from fpdf import FPDF

config        = dotenv_values(Path.home() / ".env")
ANTHROPIC_KEY = config.get("ANTHROPIC_API_KEY")

RESUMES_DIR = Path(__file__).parent / "resumes"
DB_PATH     = Path(__file__).parent / "applied_jobs.db"


# ── Resume file loading ───────────────────────────────────────────────────────

def load_resume(name: str) -> str:
    path = RESUMES_DIR / f"{name}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    fallback = RESUMES_DIR / "resume_devops.txt"
    return fallback.read_text(encoding="utf-8").strip() if fallback.exists() else ""


def _pick_resume_name(job_title: str, jd: str) -> str:
    text = (job_title + " " + jd).lower()
    if any(k in text for k in ["machine learning", "ml engineer", "data scientist",
                                 "ai engineer", "llm", "nlp", "deep learning", "mlops"]):
        return "resume_fullstack"
    return "resume_devops"


# ── JD from DB (cached during find_jobs) ─────────────────────────────────────

def _get_cached_jd(job_id: str) -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute("SELECT description FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


# ── ATS-focused Claude tailoring ─────────────────────────────────────────────

_ATS_PROMPT = """You are an expert ATS resume optimizer. Rewrite Harshith's resume to maximise its score for this specific job.

JOB: {job_title} at {company}

JOB DESCRIPTION:
{jd}

HARSHITH'S BASE RESUME:
{resume}

ATS OPTIMISATION RULES — follow every one:
1. Mirror the EXACT job title in the summary line (e.g. "seeking a DevOps Engineer role")
2. Copy keywords VERBATIM from the JD — if they write "containerization" don't say "containers"
3. Every bullet must start with a strong action verb: Architected, Deployed, Automated, Implemented, Designed, Optimised, Reduced, Increased
4. Quantify where plausible: "Deployed 5+ microservices", "Reduced pipeline runtime by 40%", "Managed 3 AWS environments"
5. Skills section: list ONLY skills mentioned in the JD, most relevant first
6. Use EXACT section headers: PROFESSIONAL SUMMARY, EXPERIENCE, EDUCATION, TECHNICAL SKILLS
7. 100% factual — Harshith's real experience is from Griffith MSc projects; do NOT invent employers or dates
8. Max 450 words total — ATS scanners prefer concise resumes
9. Plain text only — no markdown symbols, no bullet chars, use a plain hyphen (-) for bullets
10. Contact line: Harshith Mittapally | harshithreddy200811@gmail.com | +353899879815 | Dublin, Ireland

Return ONLY the resume text. No intro, no commentary."""


def tailor_with_claude(job_title: str, company: str, jd: str, resume_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = _ATS_PROMPT.format(
        job_title=job_title, company=company,
        jd=jd[:2500] if jd else "Not available — tailor based on job title only.",
        resume=resume_text,
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()


# ── PDF generation (ATS-clean single-column) ─────────────────────────────────

class _ResumePDF(FPDF):
    def __init__(self):
        super().__init__()
        self.set_margins(18, 18, 18)
        self.set_auto_page_break(auto=True, margin=15)

    def _safe(self, text: str) -> str:
        return text.encode("latin-1", errors="replace").decode("latin-1")

    def header_line(self, text: str):
        self.set_font("Helvetica", "B", 10.5)
        self.set_text_color(0, 0, 0)
        self.cell(0, 6, self._safe(text), ln=True)
        self.set_draw_color(80, 80, 80)
        self.set_line_width(0.3)
        self.line(self.get_x(), self.get_y(), self.get_x() + self.epw, self.get_y())
        self.ln(1)

    def body_text(self, text: str, indent: float = 0):
        self.set_font("Helvetica", "", 9.5)
        self.set_text_color(30, 30, 30)
        if indent:
            self.set_x(self.get_x() + indent)
        self.multi_cell(self.epw - indent, 5, self._safe(text))

    def bold_text(self, text: str):
        self.set_font("Helvetica", "B", 9.5)
        self.set_text_color(0, 0, 0)
        self.multi_cell(self.epw, 5, self._safe(text))


def _generate_pdf(resume_text: str, output_path: Path) -> Path:
    pdf = _ResumePDF()
    pdf.add_page()

    lines = [l.rstrip() for l in resume_text.splitlines()]

    # Known section headers
    SECTIONS = {"PROFESSIONAL SUMMARY", "EXPERIENCE", "EDUCATION",
                 "TECHNICAL SKILLS", "SKILLS", "PROJECTS", "INTERNSHIP",
                 "PROFILE OVERVIEW", "CERTIFICATIONS"}

    # First line(s) before any section = contact block
    in_contact = True
    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect contact block end
        if in_contact and line.strip().upper() in SECTIONS:
            in_contact = False

        if in_contact:
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            if i == 0:
                # Name line — bigger and bold
                pdf.set_font("Helvetica", "B", 13)
                pdf.set_text_color(0, 0, 0)
                pdf.cell(0, 7, pdf._safe(stripped), ln=True)
            else:
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(60, 60, 60)
                pdf.multi_cell(pdf.epw, 5, pdf._safe(stripped))
            i += 1
            continue

        stripped = line.strip()
        if not stripped:
            pdf.ln(2)
            i += 1
            continue

        # Section header
        if stripped.upper() in SECTIONS:
            pdf.ln(3)
            pdf.header_line(stripped.upper())
            i += 1
            continue

        # Bullet point
        if stripped.startswith("-") or stripped.startswith("•"):
            bullet_text = stripped.lstrip("-•").strip()
            pdf.set_font("Helvetica", "", 9.5)
            pdf.set_text_color(30, 30, 30)
            x0 = pdf.get_x()
            pdf.set_x(x0 + 4)
            pdf.cell(4, 5, pdf._safe("–"), ln=False)
            pdf.set_x(x0 + 9)
            pdf.multi_cell(pdf.epw - 9, 5, pdf._safe(bullet_text))
            i += 1
            continue

        # Experience line (role/company — dates pattern)
        if re.search(r"(–|—|\|)", stripped) and any(
            k in stripped.lower() for k in ["20", "present", "griffith", "cmr", "college"]
        ):
            pdf.bold_text(stripped)
            i += 1
            continue

        # Regular paragraph text
        pdf.body_text(stripped)
        i += 1

    pdf.output(str(output_path))
    return output_path


# ── Public API ────────────────────────────────────────────────────────────────

def tailor_for_job(job: dict, session_file: str = "") -> tuple[str, str | None]:
    """
    Tailor resume for a job. Returns (tailored_text, pdf_path).
    Uses JD from DB cache (scraped during find_jobs); no browser needed.
    """
    job_id    = job.get("id", "")
    job_title = job.get("title", "")
    company   = job.get("company", "")

    print(f"  [tailor] Tailoring for {job_title} @ {company}...")

    # Get JD from DB cache
    jd = _get_cached_jd(job_id)
    if not jd:
        print(f"  [tailor] No cached JD — using title only")

    resume_name = _pick_resume_name(job_title, jd)
    resume_text = load_resume(resume_name)

    try:
        tailored_text = tailor_with_claude(job_title, company, jd, resume_text)
    except Exception as e:
        print(f"  [tailor] Claude error: {e} — using base resume")
        tailored_text = resume_text

    # Generate tailored PDF
    pdf_path = None
    try:
        pdf_file = RESUMES_DIR / f"tailored_{job_id}.pdf"
        _generate_pdf(tailored_text, pdf_file)
        pdf_path = str(pdf_file)
        print(f"  [tailor] PDF saved: {pdf_file.name}")
    except Exception as e:
        print(f"  [tailor] PDF generation error: {e}")

    # Store tailored text in DB
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE jobs SET tailored_resume=? WHERE id=?", (tailored_text[:4000], job_id))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return tailored_text, pdf_path
