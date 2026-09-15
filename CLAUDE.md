# job_tracker — project scope

A **panel-centric, single-user-per-clone** job radar for İstanbul / Turkey-compatible remote
Computer Engineering roles: software, data/AI, QA, cloud, security, embedded and research.
The React panel (`panel_server.py` + `web/`) is the primary UI;
everything is built around it. It is meant to be cloned and run by anyone — not only the owner.

## The flow

1. `run.py fetch` populates a **globally-curated** pool into `jobs.db`; confirmed and review-needed
   opportunities are retained with their location evidence while excluded roles never enter.
2. A person opens the panel and **uploads their CV** (header → *CV yükle*, `.pdf/.txt/.md`).
3. The CV is parsed (heuristic, no LLM) into a git-ignored `derived.yaml`; the panel then
   **re-ranks the whole pool by that person's own skills** on the next dashboard load. Nothing is
   dropped — match mode is *re-rank all* (`Filter.score_only`, gateless). Matched-skill chips
   become *their* skills.

Same parse is available on the CLI: `run.py cv parse <cv>`. `run.py cv build <uid>` tailors
`cv/master.yaml` to one job post (per-clone: reads that clone's own master).

## Hard invariants — do not break

- **No LLM anywhere.** Heuristic alias detection + template fill only. No invented CV content.
- **`DERIVED_KEYS` allowlist** (`tracker.py`): a CV / `derived.yaml` may override **only**
  `{skill_weights, topic_must_match, topic_must_match_weak, seniority_boost}`. Exclusions
  (`topic_never_match` — the founding "no AI content creator / marketing / creative" rule),
  geography, remote policy and hard-seniority rejection live in `profile.yaml` **outside** the
  allowlist. **No uploaded CV can ever surface an excluded role.** Double-guaranteed: excluded
  roles never enter `jobs.db` at fetch time either.
- **Raw CV is never written to disk.** Parse in memory; persist only the derived dict.
- **Localhost only** (`127.0.0.1`). No accounts, no passwords, no server-side user list, no
  multi-tenant server. "Multi-user" = the flow is generic to whoever runs *their* clone,
  personalized through *their* `derived.yaml`. Don't add auth/HTTPS/tenancy.

## Commands

```bash
uv sync && cd web && npm install && npm run build && cd ..   # web/dist is git-ignored: build after clone
uv run python run.py fetch          # curated pool into jobs.db
uv run python run.py panel          # http://127.0.0.1:8000 — upload CV here
uv run python run.py selftest       # assert-based suite (no network)
python -m unittest tests.test_panel_server
```

## Out of scope

Accounts / hosting / multi-tenancy; per-friend server-side profiles; a UI CV *builder* (the
tailored-PDF builder stays a CLI `cv build`).
