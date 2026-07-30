#!/usr/bin/env python3
"""
Resume Auto-Tailor
- Fetches job description from DB cache (already scraped during find_jobs)
- Uses Claude to rewrite resume to beat ATS for that specific job
- Generates a clean, ATS-parseable PDF and returns its path
"""

from __future__ import annotations  # allow PEP-604 (str | None) annotations on Python 3.9

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


def save_resume(name: str, content: str) -> None:
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    path = RESUMES_DIR / f"{name}.txt"
    path.write_text(content.strip() + "\n", encoding="utf-8")


def list_resumes() -> dict[str, str]:
    if not RESUMES_DIR.exists():
        return {}
    return {
        path.stem: path.read_text(encoding="utf-8").strip()
        for path in sorted(RESUMES_DIR.glob("*.txt"))
    }


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
2. You may borrow the JD's exact WORDING for a skill Harshith already has (e.g. base resume says "containers", JD says "containerization" → write "containerization"). This is a rewording rule only — it never justifies adding a tool/technology/skill that isn't already in the base resume below, no matter how prominently the JD asks for it.
3. Skills section: include ONLY skills that already appear in HARSHITH'S BASE RESUME below. From that real list, put the ones the JD also asks for first; you may omit base-resume skills the JD doesn't care about. NEVER add a skill, tool, framework, or technology that is not in the base resume — if the JD wants something (e.g. a specific LLM framework, cloud service, or language) that isn't in the base resume, that gap simply isn't addressed on this resume; do not paper over it
4. Use EXACT section headers: PROFESSIONAL SUMMARY, EXPERIENCE, EDUCATION, TECHNICAL SKILLS
5. 100% factual — Harshith's real experience is a Griffith MSc coursework project where he got HANDS-ON EXPOSURE to these tools, not production-depth mastery. He is a near-beginner still studying toward his first AWS cert (started 2026-07-16). Do NOT invent employers, additional projects, teams, production outcomes, tools/technologies, or facts that aren't in the base resume. ALL dates (MSc: Sep 2025 - Sep 2026, Bachelor's: 2020 - 2024, project dates: Oct 2025 - Present) must be copied EXACTLY as they appear in the base resume below — never alter, recalculate, or guess a date
6. Do NOT inflate scope or depth beyond the base resume. Specifically avoid, unless the base resume already says it:
   - Any tool, framework, or technology named in the JD but absent from the base resume (e.g. don't claim LangChain, specific vector databases, or ML frameworks just because the job posting mentions them)
   - Claims of designing/architecting systems "for teams" or "across teams", standardizing practices, disaster recovery, or any enterprise/production framing
   - Invented sophistication: multi-stage pipelines with quality gates, reusable modules, state management strategy, alerting/capacity planning, etc. — if the base resume just says "used Terraform to provision infrastructure," keep it at that level of specificity, don't manufacture depth
   - Fabricated quantification ("reduced X by 40%", "managed 3 environments") — only quantify with numbers that are plausible AND traceable to something in the base resume; when in doubt, don't quantify
7. Every bullet must start with a strong action verb, but keep bullets to ONE line each describing what he actually did with the tool, not why it mattered to a business: Built, Used, Deployed, Configured, Implemented, Automated
8. Length: stay close to the base resume's length — roughly 400-550 words, one page. Do not pad to fill a second page. It is fine and expected for this resume to look like what it is: an early-career student resume, not a senior engineer's
   - Keep the single "Graduate Program Projects" entry as one entry; you may group bullets by tool area but do not multiply them into invented sub-initiatives
   - Add a "CERTIFICATIONS" section for the AWS Solutions Architect Associate study (mark clearly as in-progress, not completed)
   - Add a "RELEVANT COURSEWORK" section listing ALL modules from the "COMPLETED MODULES" list in the base resume below, ordered most-relevant-to-this-job first. These are real, already-passed modules — copy the names EXACTLY as written there, don't drop any, don't invent new ones
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
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()


# ── PDF generation (ATS-clean single-column) ─────────────────────────────────

class _ResumePDF(FPDF):
    ACCENT = (31, 61, 91)      # slate navy — name, section headers, bullet marks
    MUTED  = (110, 110, 110)   # contact line, dates, environment line
    TEXT   = (35, 35, 35)      # body text
    RULE   = (210, 214, 219)   # light hairline under section headers

    def __init__(self, compact: bool = False):
        super().__init__()
        self.compact = compact
        margin_t, margin_b = (11, 10) if compact else (13, 12)
        self.set_margins(16, margin_t, 16)
        self.set_auto_page_break(auto=True, margin=margin_b)

    @property
    def _line_h(self) -> float:
        return 4.8 if self.compact else 5.2

    @property
    def _small_line_h(self) -> float:
        return 4.4 if self.compact else 4.8

    @property
    def _gap(self) -> float:
        return 1.0 if self.compact else 1.5

    @property
    def _section_gap(self) -> float:
        return 1.3 if self.compact else 2.0

    @property
    def _rule_gap(self) -> float:
        return 2.5 if self.compact else 3.5

    _UNICODE_MAP = {
        "–": "-", "—": "-",           # en/em dash
        "‘": "'", "’": "'",           # curly single quotes
        "“": '"', "”": '"',           # curly double quotes
        "•": "-", "●": "-",           # bullets
        "…": "...",                        # ellipsis
        " ": " ",                          # nbsp
    }

    def _safe(self, text: str) -> str:
        for uni, ascii_eq in self._UNICODE_MAP.items():
            text = text.replace(uni, ascii_eq)
        return text.encode("latin-1", errors="replace").decode("latin-1")

    def name_line(self, text: str):
        self.set_font("Helvetica", "B", 19)
        self.set_text_color(*self.ACCENT)
        self.cell(0, 8, self._safe(text), new_x="LMARGIN", new_y="NEXT")

    def contact_line(self, text: str):
        self.set_font("Helvetica", "", 9.5)
        self.set_text_color(*self.MUTED)
        self.multi_cell(self.epw, self._small_line_h + 0.2, self._safe(text))
        self.set_x(self.l_margin)

    def header_rule(self):
        """Divider under the name/contact block, drawn once."""
        self.set_draw_color(*self.ACCENT)
        self.set_line_width(0.7)
        self.line(self.l_margin, self.get_y() + 1, self.l_margin + self.epw, self.get_y() + 1)
        self.ln(self._rule_gap)

    def header_line(self, text: str):
        self.set_font("Helvetica", "B", 10.5)
        self.set_text_color(*self.ACCENT)
        self.cell(0, 6, self._safe(text), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*self.RULE)
        self.set_line_width(0.4)
        self.line(self.get_x(), self.get_y(), self.get_x() + self.epw, self.get_y())
        self.ln(self._gap)

    def body_text(self, text: str, indent: float = 0):
        self.set_font("Helvetica", "", 9.5)
        self.set_text_color(*self.TEXT)
        if indent:
            self.set_x(self.get_x() + indent)
        self.multi_cell(self.epw - indent, self._line_h, self._safe(text))
        self.set_x(self.l_margin)  # multi_cell defaults to new_x=RIGHT, not left margin

    def entry_line(self, text: str):
        """Bold role/company on the left, muted date right-aligned on the same line."""
        m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", text)
        left, right = (m.group(1).strip(), m.group(2).strip()) if m else (text, "")

        self.set_font("Helvetica", "B", 10)
        left_safe = self._safe(left)
        right_safe = self._safe(right)
        right_w = self.get_string_width(right_safe) + 2 if right else 0
        left_w = self.epw - right_w

        line_h = self._line_h + 0.3
        if right and self.get_string_width(left_safe) <= left_w:
            self.set_text_color(20, 20, 20)
            self.cell(left_w, line_h, left_safe)
            self.set_font("Helvetica", "", 9.5)
            self.set_text_color(*self.MUTED)
            self.cell(right_w, line_h, right_safe, align="R", new_x="LMARGIN", new_y="NEXT")
        else:
            self.set_text_color(20, 20, 20)
            self.multi_cell(self.epw, line_h, self._safe(text))
            self.set_x(self.l_margin)

    def bullet_text(self, text: str):
        x0 = self.l_margin
        self.set_font("Helvetica", "B", 9.5)
        self.set_text_color(*self.ACCENT)
        self.set_x(x0 + 4)
        self.cell(4, self._line_h, "-", new_x="RIGHT", new_y="TOP")
        self.set_font("Helvetica", "", 9.5)
        self.set_text_color(*self.TEXT)
        self.set_x(x0 + 9)
        self.multi_cell(self.epw - 9, self._line_h, self._safe(text))
        self.set_x(self.l_margin)  # multi_cell defaults to new_x=RIGHT, not left margin

    def environment_line(self, text: str):
        self.set_font("Helvetica", "I", 8.5)
        self.set_text_color(*self.MUTED)
        self.multi_cell(self.epw, self._small_line_h, self._safe(text))
        self.set_x(self.l_margin)

    def ensure_space(self, min_height: float) -> bool:
        """Force a page break if fewer than min_height mm remain. Returns True if it broke."""
        if self.get_y() + min_height > self.h - self.b_margin:
            self.add_page()
            return True
        return False


def _render_pdf(lines: list[str], compact: bool) -> _ResumePDF:
    pdf = _ResumePDF(compact=compact)
    pdf.add_page()

    # Known section headers
    SECTIONS = {"PROFESSIONAL SUMMARY", "EXPERIENCE", "EDUCATION",
                 "TECHNICAL SKILLS", "SKILLS", "PROJECTS", "INTERNSHIP",
                 "PROFILE OVERVIEW", "CERTIFICATIONS", "RELEVANT COURSEWORK"}

    # First line(s) before any section = contact block
    in_contact = True
    first_section = True
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Detect contact block end
        if in_contact and stripped.upper() in SECTIONS:
            in_contact = False
            pdf.header_rule()

        if in_contact:
            if not stripped:
                i += 1
                continue
            if i == 0:
                pdf.name_line(stripped)
            else:
                pdf.contact_line(stripped)
            i += 1
            continue

        if not stripped:
            pdf.ln(pdf._gap)
            i += 1
            continue

        # Section header — keep at least one content line with it (avoid orphaned headers)
        if stripped.upper() in SECTIONS:
            broke = pdf.ensure_space(16 if not compact else 13)
            if not first_section and not broke:
                pdf.ln(pdf._section_gap)
            first_section = False
            pdf.header_line(stripped.upper())
            i += 1
            continue

        # Environment/tech-stack summary line
        if stripped.lower().startswith("environment:"):
            pdf.environment_line(stripped)
            i += 1
            continue

        # Bullet point
        if stripped.startswith("-") or stripped.startswith("•"):
            pdf.bullet_text(stripped.lstrip("-•").strip())
            i += 1
            continue

        # Experience/education line (role/company ... (dates) pattern)
        if re.search(r"\([^)]*\)\s*$", stripped) and any(
            k in stripped.lower() for k in ["20", "present", "griffith", "cmr", "college"]
        ):
            pdf.entry_line(stripped)
            i += 1
            continue

        # Regular paragraph text
        pdf.body_text(stripped)
        i += 1

    return pdf


def _generate_pdf(resume_text: str, output_path: Path) -> Path:
    lines = [l.rstrip() for l in resume_text.splitlines()]

    pdf = _render_pdf(lines, compact=False)
    if pdf.page_no() > 1:
        # Content spilled onto a near-empty extra page — retry with tighter
        # line-height/spacing before accepting a real multi-page resume.
        compact_pdf = _render_pdf(lines, compact=True)
        if compact_pdf.page_no() < pdf.page_no():
            pdf = compact_pdf

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

        # Save a copy under the naming convention + log to Excel
        try:
            from job_export import export_resume
            export_resume(job, pdf_path)
        except Exception as e:
            print(f"  [tailor] export error: {e}")
    except Exception as e:
        print(f"  [tailor] PDF generation error: {e}")

    # Store tailored text in DB
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE jobs SET tailored_resume=? WHERE id=?", (tailored_text[:9000], job_id))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return tailored_text, pdf_path
