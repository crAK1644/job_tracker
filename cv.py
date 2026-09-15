"""Heuristic CV parser + job-tailored LaTeX CV builder. No LLM anywhere.

parse_cv reads a CV (PDF/txt/md), detects skills against skills.yaml, and writes
the git-ignored derived.yaml that overrides the allowlisted matching keys (see
tracker.DERIVED_KEYS). build_cv (Phase 4) tailors cv/master.yaml to one job post
and compiles it with xelatex.

The parser emits ALIASES, never canonical labels: tracker's Filter searches the
skill_weights keys and topic_must_match regexes LITERALLY over the job text, so
"amazon web services" and "ec2" must both land in the config, not the label
"aws". This is the crux the design review flagged (codex-sol-3).
"""

from __future__ import annotations

import os
import re
from datetime import date
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

import yaml

import tracker

SKILLS_PATH = Path(__file__).parent / "skills.yaml"
DERIVED_PATH = tracker.DERIVED_PATH

MIN_CHARS = 200      # pdftotext gave us almost nothing -> treat as failure
MIN_SKILLS = 3       # too few skills detected -> don't clobber a good derived

# Seniority is emitted only as a SOFT seniority_boost (in the allowlist). Hard
# seniority_reject_* stays hand-set in profile.yaml - "6 years of Java" is not
# 6 years total and a past Senior title is not the desired level, too fuzzy to
# gate on (codex-sol-7). These just tilt scoring toward the detected level.
LEVEL_PATTERNS = {
    "junior": [r"\bintern(ship)?\b|\bstaj(yer)?\b",
               r"new grad|newgrad|yeni mezun|fresh grad",
               r"\bjunior\b|\bjr\.?\b",
               r"\bentry[ -]level\b|\bgiris seviye\b|\bgiriş seviye\b",
               r"\bassociate\b"],
    "mid": [r"\bmid[ -]?level\b",
            r"\b[2-4]\s*-\s*[3-6]\s*(years|yil|yıl)\b"],
    "senior": [r"\bsenior\b|\bsr\.?\b",
               r"\blead\b|\bprincipal\b|\bstaff\b|\barchitect\b"],
}


def boundary_for(alias: str) -> re.Pattern:
    """Word-boundary regex for one alias. The uniform (?<!\\w)...(?!\\w) mishandles
    punctuation skills - .NET, C++, C# start/end in non-word chars - so the
    boundary is chosen per side: alphanumeric edge -> reject an adjacent
    alnum; punctuation edge -> reject adjacent alnum AND the same punctuation
    class, so "c++" doesn't swallow the third + of "c+++" and ".net" doesn't
    fire inside "asp.net" (which is its own alias)."""
    a = alias.strip()
    esc = re.escape(a)
    left = r"(?<![A-Za-z0-9])" if a[0].isalnum() else r"(?<![A-Za-z0-9+.#])"
    right = r"(?![A-Za-z0-9])" if a[-1].isalnum() else r"(?![A-Za-z0-9+.#])"
    return re.compile(left + esc + right, re.I)


def load_skills(skills_path=SKILLS_PATH) -> dict:
    d = yaml.safe_load(Path(skills_path).read_text(encoding="utf-8"))
    for canon, meta in d.items():
        if not (isinstance(meta, dict) and meta.get("aliases")
                and "domain" in meta and "weight" in meta):
            raise ValueError(f"skills.yaml: bad entry {canon!r}")
    return d


def read_cv_text(path) -> str:
    """CV text from .txt/.md directly, .pdf via poppler pdftotext. DOCX is not
    supported (no python-docx dependency) - the user exports a PDF."""
    path = Path(path)
    suf = path.suffix.lower()
    if suf in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suf == ".pdf":
        exe = shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
        if not Path(exe).exists():
            raise RuntimeError("pdftotext not found - install poppler")
        r = subprocess.run([exe, "-layout", str(path), "-"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"pdftotext failed (rc={r.returncode}): "
                               f"{r.stderr.strip()[:200]}")
        return r.stdout
    if suf in (".docx", ".doc"):
        raise RuntimeError("DOCX not supported - export your CV to PDF and retry")
    raise RuntimeError(f"unsupported CV format: {suf!r} (use .pdf/.txt/.md)")


def detect_skills(text: str, skills: dict) -> dict:
    """canonical -> sorted list of matched aliases. Word-boundary, case-
    insensitive; the boundary already stops "java" matching inside
    "javascript", so cross-canonical overlap needs no extra pass."""
    text_n = unicodedata.normalize("NFC", text)
    present: dict[str, set] = {}
    for canon, meta in skills.items():
        for alias in meta["aliases"]:
            if boundary_for(alias).search(text_n):
                present.setdefault(canon, set()).add(alias)
    return {c: sorted(v) for c, v in present.items()}


def dominant_domains(present: dict, skills: dict) -> list[str]:
    """Domains within 1 of the most-represented one (stable tie order). A clear
    leader (backend 6 vs cloud 1) excludes the singletons; a near-tie
    ("java backend aws" -> backend 2, cloud 1) keeps both, so a cloud-only job
    still passes the gate."""
    counts = Counter(skills[c]["domain"] for c in present)
    if not counts:
        return []
    maxc = max(counts.values())
    doms = [d for d, c in counts.items() if c >= maxc - 1]
    return sorted(doms, key=lambda d: (-counts[d], d))


# Month abbreviations (English + Turkish), keyed by the first three letters so
# "June"->"jun", "sept"->"sep", "mart"->"mar" all resolve. ponytail: 3-letter
# key is enough for every month in both languages; no full-name table needed.
_MONTHS = {
    "jan": 1, "oca": 1, "feb": 2, "şub": 2, "sub": 2, "mar": 3, "apr": 4,
    "nis": 4, "may": 5, "jun": 6, "haz": 6, "jul": 7, "tem": 7, "aug": 8,
    "ağu": 8, "agu": 8, "sep": 9, "eyl": 9, "oct": 10, "eki": 10, "nov": 11,
    "kas": 11, "dec": 12, "ara": 12,
}
_MON = r"(?:[A-Za-zçğşıöü]{3,9}\.?\s*)?"          # optional month word before a year
_PRESENT = r"present|current|now|ongoing|halen|devam\w*|güncel"
_RANGE = re.compile(
    rf"({_MON}20\d{{2}})\s*(?:[-–—]{{1,2}}|to)\s*({_MON}20\d{{2}}|{_PRESENT})",
    re.I,
)
_EXP_HEADING = re.compile(
    r"^\s*(experience|work experience|professional experience|employment|"
    r"work history|iş deneyimi|deneyim|çalışma)\b", re.I)
_SECTION_HEADING = re.compile(
    r"^\s*(education|projects?|technical skills|skills|languages|"
    r"certifications?|publications?|awards|references?|volunteer|interests|"
    r"eğitim|projeler|beceriler|yetenekler|sertifika|referans|dil)\b", re.I)


def _abs_month(token: str, is_end: bool) -> int | None:
    """A 'Sep 2022'/'2022' token -> an absolute month number (year*12+month).
    A year with no month counts as January (start) or December (end)."""
    ym = re.search(r"20\d{2}", token)
    if not ym:
        return None
    mon = re.search(r"[A-Za-zçğşıöü]{3,}", token)
    month = _MONTHS.get(mon.group().lower()[:3]) if mon else None
    if month is None:
        month = 12 if is_end else 1
    return int(ym.group()) * 12 + month


def _experience_section(text: str) -> str:
    """Text under the Experience heading up to the next section heading, or ''.
    Scoping to this section keeps education / project date ranges out of the
    years-of-experience estimate (codex-sol-7: seniority is a soft signal)."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _EXP_HEADING.match(ln)), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines))
                if _SECTION_HEADING.match(lines[i])), len(lines))
    return "\n".join(lines[start + 1:end])


def _experience_years(text: str) -> int:
    """Years of work experience from date ranges in the Experience section.
    Overlapping ranges are merged (union of months), so concurrent roles are
    not double-counted; a single span like 'Sep 2022 - Sep 2024' -> 2."""
    section = _experience_section(text)
    if not section:
        return 0
    today = date.today().year * 12 + date.today().month
    spans: list[tuple[int, int]] = []
    for start_tok, end_tok in _RANGE.findall(section):
        start = _abs_month(start_tok, is_end=False)
        end = today if re.match(_PRESENT, end_tok, re.I) else _abs_month(end_tok, is_end=True)
        if start is None or end is None:
            continue
        end = min(end, today)              # ignore future (e.g. expected grad) dates
        if end >= start:
            spans.append((start, end))
    if not spans:
        return 0
    spans.sort()
    # Month indices are inclusive on both ends (you work through the end month),
    # so a span covers (end - start + 1) months.
    months, cur_start, cur_end = 0, *spans[0]
    for s, e in spans[1:]:
        if s <= cur_end + 1:              # overlaps or is contiguous -> extend
            cur_end = max(cur_end, e)
        else:
            months += cur_end - cur_start + 1
            cur_start, cur_end = s, e
    months += cur_end - cur_start + 1
    return months // 12


def detect_seniority(text: str) -> tuple[str, int]:
    t = text.lower()
    yrs = [int(m) for m in re.findall(r"(\d{1,2})\+?\s*(?:years|yrs|yıl|yil)", t)]
    # Believe an explicit "N years" phrase or the date-range span, whichever is larger.
    maxyr = max([*yrs, _experience_years(text)], default=0)
    senior = bool(re.search(r"\b(senior|sr\.?|lead|principal|staff|architect)\b", t))
    junior = bool(re.search(r"\b(junior|jr\.?|intern|internship|new grad|"
                            r"yeni mezun|graduate|entry[ -]level)\b", t))
    # Measured years win over a stray title word: a past internship must not pin
    # a multi-year CV to "junior", and 2+ measured years outranks a junior tag.
    if senior or maxyr >= 5:
        return "senior", maxyr
    if maxyr >= 2:
        return "mid", maxyr
    if junior or maxyr < 2:
        return "junior", maxyr
    return "mid", maxyr


def build_derived(present: dict, skills: dict, level: str) -> dict:
    """The allowlisted override dict. skill_weights = every alias of every
    detected skill (so any alias in a job scores); topic_must_match = the
    dominant-domain aliases as boundary regexes (the gate); seniority_boost =
    the detected level's patterns (soft +score only)."""
    skill_weights = {}
    for canon in present:
        for alias in skills[canon]["aliases"]:
            skill_weights[alias.lower()] = skills[canon]["weight"]

    doms = dominant_domains(present, skills)
    gate = []
    for canon in present:
        if skills[canon]["domain"] in doms:
            for alias in skills[canon]["aliases"]:
                gate.append(rf"(?<!\w){re.escape(alias.lower())}(?!\w)")
    gate = sorted(set(gate))

    return {
        "skill_weights": skill_weights,
        "topic_must_match": gate,
        "topic_must_match_weak": gate,
        "seniority_boost": LEVEL_PATTERNS[level],
    }


def _validate_derived(d: dict) -> None:
    if set(d) - tracker.DERIVED_KEYS:
        raise ValueError(f"derived has non-allowlisted keys: {set(d) - tracker.DERIVED_KEYS}")
    sw = d.get("skill_weights")
    if not (isinstance(sw, dict) and sw
            and all(isinstance(k, str) and isinstance(v, int) for k, v in sw.items())):
        raise ValueError("skill_weights must be a non-empty {str: int}")
    for k in ("topic_must_match", "topic_must_match_weak", "seniority_boost"):
        v = d.get(k)
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise ValueError(f"{k} must be a list[str]")
        for p in v:
            re.compile(p)  # every emitted regex must compile


def _atomic_write_yaml(path, data: dict) -> None:
    d = Path(path).parent
    fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# Generated by `run.py cv parse`. Git-ignored. Overrides only\n"
                    "# the allowlisted keys in profile.yaml (tracker.DERIVED_KEYS).\n"
                    "# Re-run `fetch` after this changes - scores are set at fetch time.\n")
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def parse_cv_to_derived(text, skills_path=SKILLS_PATH,
                        min_chars=MIN_CHARS, min_skills=MIN_SKILLS) -> tuple[dict, dict]:
    """Pure core of `parse_cv`: extracted CV text -> (validated derived dict,
    JSON-friendly summary). Raises ValueError on too-short text, too few skills,
    or an invalid profile - never reads a file, writes, or prints. Both the CLI
    (`parse_cv`) and the panel upload endpoint call this so the parse rules and
    the transactional guarantee live in exactly one place."""
    if len(text.strip()) < min_chars:
        raise ValueError(f"extracted only {len(text.strip())} chars (< {min_chars}) "
                         f"- is the CV a scanned image?")
    skills = load_skills(skills_path)
    present = detect_skills(text, skills)
    if len(present) < min_skills:
        raise ValueError(f"only {len(present)} skills detected (< {min_skills})")
    level, maxyr = detect_seniority(text)
    derived = build_derived(present, skills, level)
    _validate_derived(derived)  # raises ValueError on a malformed dict
    summary = {
        "skills": sorted(present),
        "domains": dominant_domains(present, skills),
        "seniority": level,
        "yearsSeen": maxyr,
        "aliasCount": len(derived["skill_weights"]),
        "gateCount": len(derived["topic_must_match"]),
    }
    return derived, summary


def parse_cv(cv_path, skills_path=SKILLS_PATH, derived_path=DERIVED_PATH,
             min_chars=MIN_CHARS, min_skills=MIN_SKILLS) -> int:
    """Parse a CV to derived.yaml. Transactional: on any failure the existing
    derived.yaml is untouched and this returns nonzero - never writes a garbage
    or empty profile over a working one (codex-sol-4)."""
    try:
        text = read_cv_text(cv_path)
    except Exception as e:
        print(f"cv parse: {e}", file=sys.stderr)
        return 1
    try:
        derived, summary = parse_cv_to_derived(text, skills_path, min_chars, min_skills)
    except ValueError as e:
        print(f"cv parse: {e} - derived.yaml left as-is", file=sys.stderr)
        return 1
    _atomic_write_yaml(derived_path, derived)

    print(f"cv parse: wrote {derived_path}")
    print(f"  skills detected ({len(summary['skills'])}): "
          f"{', '.join(summary['skills'])}")
    print(f"  dominant domain(s): {', '.join(summary['domains'])}")
    print(f"  seniority: {summary['seniority']}"
          + (f" (~{summary['yearsSeen']}y seen)" if summary["yearsSeen"] else ""))
    print(f"  {summary['aliasCount']} aliases scored, "
          f"{summary['gateCount']} gate patterns")
    print("  -> run `uv run python run.py fetch` to re-score jobs with this CV")
    return 0


# ------------------------------------------------------------- CV builder
MASTER_PATH = Path(__file__).parent / "cv" / "master.yaml"
TEMPLATE_PATH = Path(__file__).parent / "cv" / "template.tex"
XELATEX = ("/Library/TeX/texbin/xelatex"
           if Path("/Library/TeX/texbin/xelatex").exists()
           else shutil.which("xelatex"))

# Single map, single pass. Sequential .replace() would re-escape the braces a
# backslash's own replacement introduces (codex-sol-8); one re.sub scans the
# string once and each match is substituted from the map without rescanning.
_LATEX_MAP = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}
_LATEX_RX = re.compile(r"[\\&%$#_{}~^]")


def latex_escape(s) -> str:
    return _LATEX_RX.sub(lambda m: _LATEX_MAP[m.group()], str(s))


def fill_template(template: str, values: dict) -> str:
    """One-pass placeholder fill. Values are already-escaped LaTeX fragments and
    are inserted verbatim, so user text containing <<X>> inside a value is never
    re-substituted. Fails on any duplicate placeholder in the template or any
    unresolved/unknown one (codex-sol-8)."""
    keys = re.findall(r"<<(\w+)>>", template)
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"template has duplicate placeholders: {dupes}")

    # re.sub replaces every <<\w+>> in the template in one pass, so a template
    # placeholder is always either substituted or raises here - there is no
    # "unresolved" case to scan for afterwards. Re-scanning the output would
    # instead false-flag <<...>> that legitimately appears inside a user value.
    def repl(m):
        k = m.group(1)
        if k not in values:
            raise KeyError(f"template placeholder <<{k}>> has no value")
        return values[k]

    return re.sub(r"<<(\w+)>>", repl, template)


def job_keywords(title: str, desc: str, skills: dict) -> set:
    """Lowercased aliases the job post mentions - the same skills.yaml match the
    parser uses, so bullet relevance and job scoring speak the same vocabulary."""
    present = detect_skills(f"{title} {desc}", skills)
    kw = set()
    for canon in present:
        kw.update(a.lower() for a in skills[canon]["aliases"])
    return kw


def ats_keywords(master: dict, title: str, desc: str, skills: dict) -> list[str]:
    """ATS keyword list: the job's own alias spellings for skills the candidate
    GENUINELY has. Intersection of {skills the job mentions} and {skills present
    in the CV} - never a keyword the candidate lacks (the no-invent contract). The
    job's spelling is emitted so a literal ATS keyword filter matches."""
    cand_text = " ".join([
        *(b.get("text", "") for e in (master.get("experiences") or [])
          for b in (e.get("bullets") or [])),
        *(str(v) for g in (master.get("skills") or {}).values() for v in g),
        master.get("summary", "") or "",
    ])
    cand = detect_skills(cand_text, skills)
    job = detect_skills(f"{title} {desc}", skills)
    seen, out = set(), []
    for canon in sorted(cand.keys() & job.keys(),
                        key=lambda c: (-skills[c].get("weight", 0), c)):
        for alias in job[canon]:            # aliases the JOB used = literal ATS terms
            if alias.lower() not in seen:
                seen.add(alias.lower())
                out.append(alias)
    return out


def select_bullets(bullets: list, job_kw: set, min_n=2, max_n=4):
    """Pick bullets for one experience. EVERY bullet whose skills overlap the job
    is kept - they carry the ATS keywords in context and must not be dropped. If
    fewer than min_n overlap, top up with original-order non-overlapping bullets
    so no experience is emptied. Returns (selected, omitted), both in ORIGINAL
    chronological order (codex-sol-10)."""
    overlap = [i for i, b in enumerate(bullets)
               if {t.lower() for t in b.get("skills", [])} & job_kw]
    over = set(overlap)
    filler = [i for i, b in enumerate(bullets) if i not in over]
    need = max(0, min(min_n, max_n) - len(overlap))  # floor to min_n; never drop overlaps
    keep = over | set(filler[:need])
    selected = [b for i, b in enumerate(bullets) if i in keep]
    omitted = [b for i, b in enumerate(bullets) if i not in keep]
    return selected, omitted


def _experience_tex(experiences: list, job_kw: set) -> str:
    """LaTeX for the experience section - structural commands wrap already-
    escaped leaves. Experience order (chronology) is preserved as written."""
    out = []
    for exp in experiences:
        role = latex_escape(exp.get("role", ""))
        company = latex_escape(exp.get("company", ""))
        dates = latex_escape(exp.get("dates", ""))
        selected, omitted = select_bullets(exp.get("bullets", []), job_kw)
        print(f"  {exp.get('company','?')}: kept {len(selected)}/"
              f"{len(exp.get('bullets', []))} bullets"
              + (f" (dropped {len(omitted)} less-relevant)" if omitted else ""))
        out.append(rf"\textbf{{{role}}}, {company}\hfill {dates}")
        if selected:
            items = "\n".join(rf"\item {latex_escape(b['text'])}" for b in selected)
            out.append(r"\begin{itemize}" + "\n" + items + "\n" + r"\end{itemize}")
        out.append("")
    return "\n".join(out).strip()


def _ats_tex(kws: list) -> str:
    """Folded heading: empty keyword list renders nothing (no orphan title)."""
    if not kws:
        return ""
    return r"\heading{Key Skills for this Role}" + "\n" + ", ".join(
        latex_escape(k) for k in kws)


def _education_tex(education: list) -> str:
    """Static section - same on every CV. Folded heading; empty -> nothing."""
    if not education:
        return ""
    lines = [r"\heading{Education}"]
    for e in education:
        head = ", ".join(x for x in (latex_escape(e.get("degree", "")),
                                     latex_escape(e.get("school", ""))) if x)
        lines.append(rf"\textbf{{{head}}}\hfill {latex_escape(e.get('dates', ''))}\\")
    return "\n".join(lines)


def _skills_tex(groups: dict) -> str:
    lines = []
    for name, items in (groups or {}).items():
        vals = ", ".join(latex_escape(x) for x in items)
        lines.append(rf"\textbf{{{latex_escape(name)}}}: {vals}\\")
    return "\n".join(lines)


def _contact_tex(contact: dict) -> str:
    parts = []
    for k in ("email", "phone", "location"):
        if contact.get(k):
            parts.append(latex_escape(contact[k]))
    for link in contact.get("links", []) or []:
        label, url = link.get("label", ""), link.get("url", "")
        if url:
            parts.append(rf"\href{{{latex_escape(url)}}}{{{latex_escape(label)}}}")
    return r" \textbar{} ".join(parts)


def compile_pdf(tex: str, out_path: Path) -> int:
    """Hardened xelatex compile (codex-sol-9): array args, no shell-escape,
    halt-on-error, temp dir, timeout, checked returncode, verified non-empty
    output. If xelatex is missing or fails, write the .tex beside the target and
    return nonzero - never report a PDF that was not built."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not XELATEX:
        tex_out = out_path.with_suffix(".tex")
        tex_out.write_text(tex, encoding="utf-8")
        print(f"cv build: xelatex not found - wrote {tex_out} (compile it elsewhere)",
              file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as td:
        texf = Path(td) / "cv.tex"
        texf.write_text(tex, encoding="utf-8")
        r = subprocess.run(
            [XELATEX, "-no-shell-escape", "-halt-on-error",
             "-interaction=nonstopmode", "-output-directory", td, str(texf)],
            capture_output=True, text=True, timeout=120)
        pdf = Path(td) / "cv.pdf"
        if r.returncode != 0 or not pdf.exists() or pdf.stat().st_size == 0:
            tex_out = out_path.with_suffix(".tex")
            tex_out.write_text(tex, encoding="utf-8")
            tail = (r.stdout or "")[-800:]
            print(f"cv build: xelatex failed (rc={r.returncode}) - wrote {tex_out}\n{tail}",
                  file=sys.stderr)
            return 1
        shutil.copyfile(pdf, out_path)
    return 0


def build_cv(uid: str, out=None, master_path=MASTER_PATH,
             template_path=TEMPLATE_PATH, skills_path=SKILLS_PATH,
             db_path=None) -> int:
    """Build a job-tailored CV PDF for one job uid. Bullet selection is
    mechanical (skill overlap), never generative - the no-LLM contract."""
    if not re.fullmatch(r"[0-9a-fA-F]{16}", uid):
        print(f"cv build: {uid!r} is not a valid job uid", file=sys.stderr)
        return 1
    conn = tracker.connect(db_path) if db_path else tracker.connect()
    row = conn.execute(
        "SELECT title, company, description FROM jobs WHERE uid=?", (uid,)
    ).fetchone()
    conn.close()
    if row is None:
        print(f"cv build: no job with uid {uid} - run `report` to list uids",
              file=sys.stderr)
        return 1

    master = yaml.safe_load(Path(master_path).read_text(encoding="utf-8"))
    template = Path(template_path).read_text(encoding="utf-8")
    skills = load_skills(skills_path)
    job_kw = job_keywords(row["title"], row["description"] or "", skills)
    print(f"cv build: tailoring to {row['title']} @ {row['company']}")
    print(f"  job mentions: {', '.join(sorted(job_kw)) or '(no known skills)'}")
    ats = ats_keywords(master, row["title"], row["description"] or "", skills)
    print(f"  ATS keywords (only skills already in your CV): "
          f"{', '.join(ats) or '(none)'}")

    values = {
        "NAME": latex_escape(master.get("name", "")),
        "CONTACT": _contact_tex(master.get("contact", {}) or {}),
        "SUMMARY": latex_escape((master.get("summary") or "").strip()),
        "KEYSKILLS": _ats_tex(ats),
        "EXPERIENCE": _experience_tex(master.get("experiences", []) or [], job_kw),
        "EDUCATION": _education_tex(master.get("education", []) or []),
        "SKILLS": _skills_tex(master.get("skills", {}) or {}),
    }
    tex = fill_template(template, values)

    out_path = Path(out) if out else (Path(__file__).parent / "cv" / f"{uid}.pdf")
    rc = compile_pdf(tex, out_path)
    if rc == 0:
        print(f"cv build: wrote {out_path}")
    return rc
