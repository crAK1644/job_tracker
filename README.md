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
uv run python run.py selftest     # run the assert-based test suite

uv run python run.py cv parse <cv.pdf|.txt|.md>   # derive matching from your CV, then re-fetch
uv run python run.py cv build <uid> [--out f.pdf] # tailor cv/master.yaml to a job post (uid from the report)
```

`cv parse` writes a git-ignored `derived.yaml` that overrides only an allowlisted subset of the
profile (skills/topics/seniority boost). Exclusions, geography, remote and hard-seniority rules stay
in `profile.yaml` and are never touched by a CV.
