# job_tracker

Istanbul / remote DS-ML-AI job listings aggregator. Fetches from ~14 sources,
filters + scores against a profile, renders a static HTML report. Includes a
no-LLM CV parser (tailors matching to your skills) and a LaTeX CV builder that
tailors your CV to a specific job post.

## Setup

```bash
uv sync
cp cv/master.example.yaml cv/master.yaml   # then fill with your real CV content
```

CV parsing needs `pdftotext` (poppler); the CV builder needs `xelatex` (MacTeX / TeX Live).

## Commands

```bash
uv run python run.py fetch        # fetch + score jobs into jobs.db
uv run python run.py report       # write report.html (open it)
uv run python run.py panel        # serve the interactive local panel
uv run python run.py selftest     # run the assert-based test suite

uv run python run.py cv parse <cv.pdf|.txt|.md>   # derive matching from your CV, then re-fetch
uv run python run.py cv build <uid> [--out f.pdf] # tailor cv/master.yaml to a job post (uid from the report)
```

## Interactive panel

The dashboard is a local React + shadcn/ui application. Build it once after
cloning, then serve it with the Python CLI:

```bash
cd web && npm install && npm run build && cd ..
uv run python run.py panel
```

Open `http://127.0.0.1:8000`. The panel reads `jobs.db` directly and persists
status changes locally; it does not expose a remote service. During UI work,
run `npm run dev` in `web/` and keep `uv run python run.py panel` running for
the API proxy.

Upload a CV from the panel header (**CV yükle**, `.pdf/.txt/.md`) and the curated pool re-ranks by
your skills right away — the matched-skill chips on each card become *your* skills. This is the same
parse as `cv parse` on the CLI; the raw CV is never written to disk, only the derived skills. It's
per-clone: each person runs their own clone and uploads their own CV.

`cv parse` writes a git-ignored `derived.yaml` that overrides only an allowlisted subset of the
profile (skills/topics/seniority boost). Exclusions, geography, remote and hard-seniority rules stay
in `profile.yaml` and are never touched by a CV.

> **ATS:** `cv build` adds a "Key Skills for this Role" section listing the job's own
> keyword spellings for skills you already have (intersection only — it never invents a skill).
