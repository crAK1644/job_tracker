#!/usr/bin/env python3
"""CLI: discover | fetch | report | panel | mark | selftest

    uv run python run.py fetch              # pull all sources, score, store
    uv run python run.py fetch --browser    # + hiring.cafe (Playwright, slower)
    uv run python run.py fetch --source lever:trendyol
    uv run python run.py report             # render report.html from the DB
    uv run python run.py panel              # serve the React dashboard locally
    uv run python run.py mark <uid> applied
    uv run python run.py discover           # verify companies.yaml tokens (no Playwright)
    uv run python run.py discover --auto    # + crawl ats:unknown companies with Playwright
    uv run python run.py selftest           # assert-based checks on the filter logic
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import unicodedata
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

import sources
import report
import tracker

COMPANIES_PATH = Path(__file__).parent / "companies.yaml"
REPORT_PATH = Path(__file__).parent / "report.html"


def load_companies() -> list[dict]:
    """Load both the original single-board entries and the expanded catalogue.

    Normalising here keeps the hand-edited YAML readable while giving every
    employer a stable key, visible careers link, collection method and status.
    """
    companies = yaml.safe_load(COMPANIES_PATH.read_text(encoding="utf-8"))["companies"]
    for entry in companies:
        entry.setdefault("id", re.sub(r"[^a-z0-9]+", "-", entry["name"].lower()).strip("-"))
        ats, token = entry.get("ats"), entry.get("token")
        if not entry.get("careers_url"):
            if ats == "lever" and token:
                entry["careers_url"] = f"https://jobs.lever.co/{token}"
            elif ats == "ashby" and token:
                entry["careers_url"] = f"https://jobs.ashbyhq.com/{token}"
            elif ats == "greenhouse" and token:
                entry["careers_url"] = f"https://boards.greenhouse.io/{token}"
            else:
                entry["careers_url"] = f"https://{entry['domain']}/careers"
        entry.setdefault("collection_method", "ats" if ats not in (None, "unknown") else "manual")
        entry.setdefault("collection_status", "verified" if entry.get("verified") else "unchecked")
        entry.setdefault("verification", "Verified public ATS board." if entry.get("verified")
                         else "Official careers destination needs a live check.")
        if "boards" not in entry:
            entry["boards"] = [{
                "ats": ats, "token": token, "careers_url": entry["careers_url"],
                "collection_method": entry["collection_method"],
                "collection_status": entry["collection_status"],
                "verification": entry["verification"],
            }]
    return companies


def company_boards(entry: dict) -> list[dict]:
    return [board for board in entry.get("boards") or [] if board.get("enabled") is not False]


def run_id_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_audit(args) -> int:
    """Sync catalogue coverage and reclassify stored history without fetching."""
    companies = load_companies()
    conn = tracker.connect()
    try:
        tracker.register_employer_coverage(conn, companies)
        rekeyed = tracker.rekey_legacy_jobs(conn)
        reclassified = tracker.reclassify_jobs(
            conn, tracker.Filter(tracker.load_profile(include_derived=False)))
        states = dict(conn.execute(
            "SELECT collection_status, COUNT(*) FROM employer_coverage GROUP BY collection_status"
        ).fetchall())
    finally:
        conn.close()
    print(f"catalogue : {len(companies)} employers")
    print(f"coverage  : {states}")
    print(f"jobs      : {reclassified} reclassified, {rekeyed} IDs reconciled")
    return 0


# --------------------------------------------------------------------- fetch

def degraded_sources(conn, per_source: dict[str, int]) -> dict[str, str]:
    """Sources that answered without raising but still under-reported the board.

    Both cases here are invisible to the error list and would otherwise be read
    as a healthy, authoritative fetch by the closing sweep.
    """
    # A board that has genuinely gone empty answers 0 every day, and exempting
    # it every day would mean its jobs never expire at all. One zero is
    # ambiguous; two in a row is a board with nothing on it, so the second run
    # sweeps. The previous run's own summary is the memory - this row exists
    # before the current run's is written.
    prev = conn.execute(
        "SELECT summary FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    import json as _json
    zero_before = {k for k, v in (_json.loads(prev[0]).get("per_source") or {}).items()
                   if v == 0} if prev else set()
    out = {}
    for tag, n in per_source.items():
        if tag in sources.TRUNCATED:
            out[tag] = sources.TRUNCATED[tag]
        elif n == 0 and tag not in zero_before and tracker.active_count(conn, tag) > 0:
            out[tag] = "HTTP 200 but parsed 0 jobs while it still has live rows"
    return out


def cmd_fetch(args) -> int:
    # A personal CV ranks results after collection. It must never narrow the
    # globally useful catalogue for a different graduate or master’s student.
    profile = tracker.load_profile(include_derived=False)
    filt = tracker.Filter(profile)
    conn = tracker.connect()
    run_id = run_id_now()
    companies = load_companies()
    tracker.register_employer_coverage(conn, companies)
    tracker.rekey_legacy_jobs(conn)
    tracker.reclassify_jobs(conn, filt)

    only = None
    if args.source is not None:
        # "lever:trendyol" -> filter to that one company/board fetcher.
        # `is not None`, not truthiness: a bare `--source` with the value
        # forgotten yields [], which as a falsy value turned a scoped run back
        # into a full one - including the job-closing sweep it means to skip.
        only = set(args.source)
        if not only:
            print("--source needs at least one value", file=sys.stderr)
            return 2

    raw_jobs: list[dict] = []
    sources.TRUNCATED.clear()  # module state; a second fetch in one process must not inherit it
    per_source: dict[str, int] = {}
    errors: list[str] = []

    c = sources.client()
    try:
        # Group A: per-company ATS boards
        for entry in companies:
            if entry.get("enabled") is False:
                continue
            for board in company_boards(entry):
                ats, token = board.get("ats"), board.get("token")
                if ats in (None, "unknown") or not token:
                    continue
                tag = f"{ats}:{token}"
                if only and tag not in only and entry["name"] not in only and entry["id"] not in only:
                    continue
                try:
                    jobs = sources.fetch_company({**entry, **board}, c)
                    per_source[tag] = len(jobs)
                    raw_jobs.extend(jobs)
                    status = "partial" if tag in sources.TRUNCATED else ("complete" if jobs else "empty")
                    tracker.update_employer_coverage(conn, entry, board, status=status,
                                                     checked_at=run_id, jobs_seen=len(jobs),
                                                     detail=sources.TRUNCATED.get(tag, ""))
                except httpx.HTTPStatusError as e:
                    status = "blocked" if e.response.status_code in {401, 403, 429} else "failed"
                    detail = f"HTTP {e.response.status_code}"
                    errors.append(f"{tag}: {detail}")
                    tracker.update_employer_coverage(conn, entry, board, status=status,
                                                     checked_at=run_id, detail=detail)
                except Exception as e:  # noqa: BLE001 - one bad board must not kill the run
                    detail = f"{type(e).__name__}: {e}"
                    errors.append(f"{tag}: {detail}")
                    tracker.update_employer_coverage(conn, entry, board, status="failed",
                                                     checked_at=run_id, detail=detail)

        # Group B: job boards
        for name, fn in sources.BOARD_FETCHERS.items():
            if only and name not in only:
                continue
            try:
                jobs = fn(c)
                per_source[name] = len(jobs)
                raw_jobs.extend(jobs)
            except httpx.HTTPError as e:
                errors.append(f"{name}: HTTP error {e}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {type(e).__name__}: {e}")
    finally:
        c.close()

    # Group C: browser-backed sources are part of a full scan. `--http-only`
    # makes a quick deterministic API/HTML scan and reports browser coverage as
    # unchecked instead of pretending it was inspected.
    if not args.http_only:
        try:
            import browser_sources  # lazy: a missing optional dependency is explicit below
        except ModuleNotFoundError as e:
            errors.append(f"browser sources unavailable: install browser extra ({e.name})")
            browser_sources = None
        for name, fn in (browser_sources.BROWSER_FETCHERS.items() if browser_sources else []):
            if only and name not in only:
                continue
            try:
                jobs = fn()
                per_source[name] = len(jobs)
                raw_jobs.extend(jobs)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {type(e).__name__}: {e}")

    kept = []
    reasons: dict[str, int] = {}
    for j in raw_jobs:
        classified = filt.classify(j)
        if classified["keep"]:
            # classify already decided to keep this row; score_only gives the
            # real ranking value without re-gating. evaluate() would return 0
            # for a `review` row it deems too vague, burying every review job
            # at the bottom of report.html (its accept path returns score_only
            # anyway, so confirmed rows are unchanged).
            j["score"] = filt.score_only(j)
            j.update({k: v for k, v in classified.items() if k != "keep"})
            kept.append(j)
        else:
            reason = classified["reason"]
            reasons[reason] = reasons.get(reason, 0) + 1

    counts = tracker.upsert_jobs(conn, kept, run_id)
    # Only sweep the sources that actually reported. per_source is written on
    # success only, so a source that raised is absent from it and its jobs are
    # left alone instead of being closed for a network failure.
    #
    # An exception is not the only way to come back with nothing, though, and
    # the two cases below both reported "success" while under-reporting the
    # board. Either one lets the sweep close live jobs that were never gone.
    degraded = degraded_sources(conn, per_source)
    sweep = [t for t in per_source if t not in degraded]
    closed = 0 if only else tracker.mark_closed(conn, run_id, sweep)

    print(f"fetched   : {len(raw_jobs)} raw postings across {len(per_source)} sources")
    print(f"kept      : {len(kept)} after filters (rejected: {reasons})")
    print(f"stored    : {counts['inserted']} new, {counts['updated']} refreshed, {closed} closed")
    if errors:
        print(f"\n{len(errors)} source(s) failed (others still ran fine):")
        for e in errors:
            print(f"  ! {e}")
    if degraded:
        print(f"\n{len(degraded)} source(s) degraded - not swept, jobs kept open:")
        for tag, why in degraded.items():
            print(f"  ~ {tag}: {why}")

    import json as _json
    # A scoped probe updates rows and employer coverage, but is not a complete
    # dashboard scan. Do not replace the latest full-run health summary with a
    # one-source result.
    if only is None:
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, summary) VALUES (?, ?)",
            (run_id, _json.dumps({"per_source": per_source, "errors": errors,
                                  "degraded": degraded})),
        )
        conn.commit()
    conn.close()
    return 0


# -------------------------------------------------------------------- report

def cmd_report(args) -> int:
    profile = tracker.load_profile()
    conn = tracker.connect()
    row = conn.execute(
        "SELECT run_id, summary FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone() if _table_exists(conn, "runs") else None
    if row is None:
        print("no runs recorded yet - run `fetch` first", file=sys.stderr)
        return 1
    import json as _json
    run_id, summary = row["run_id"], _json.loads(row["summary"])
    html_out = report.render_report(conn, profile, run_id, summary)
    REPORT_PATH.write_text(html_out, encoding="utf-8")
    print(f"wrote {REPORT_PATH} ({len(tracker.active_jobs(conn))} active listings)")
    conn.close()
    return 0


def cmd_panel(args) -> int:
    """Serve the local React dashboard and its SQLite-backed API."""
    import panel_server
    return panel_server.serve(host=args.host, port=args.port)


def _table_exists(conn, name) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ---------------------------------------------------------------------- mark

def cmd_mark(args) -> int:
    conn = tracker.connect()
    ok = tracker.set_status(conn, args.uid, args.status)
    conn.close()
    if not ok:
        print(f"no job with uid {args.uid!r}", file=sys.stderr)
        return 1
    print(f"{args.uid} -> {args.status}")
    return 0


# ----------------------------------------------------------------- discover

def cmd_discover(args) -> int:
    """Verify existing companies.yaml entries by fetching them, and (with --auto)
    crawl ats:unknown companies with Playwright to find and verify a new token."""
    companies = load_companies()
    only = set(args.only.split(",")) if args.only else None
    c = sources.client()
    ok_count = fail_count = skip_count = 0
    try:
        for entry in companies:
            if only and entry["name"] not in only and entry.get("token") not in only:
                continue
            ats, token = entry.get("ats"), entry.get("token")
            if ats in (None, "unknown") or not token:
                skip_count += 1
                continue
            try:
                jobs = sources.fetch_company(entry, c)
                sample = jobs[0]["company"] if jobs else entry["name"]
                print(f"  OK    {entry['name']:<20} {ats}:{token:<15} {len(jobs):3d} jobs "
                      f"(sample company field: {sample!r})")
                ok_count += 1
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL  {entry['name']:<20} {ats}:{token:<15} {type(e).__name__}: {e}")
                fail_count += 1

        if args.auto:
            import discover  # lazy: pulls in playwright, not needed for plain verify
            print(f"\nauto-discovering ats:unknown companies (Playwright)...")
            results = discover.discover_unknown(companies, c, only=only)
            written = write_discovery_results(results)
            verified_ct = sum(1 for r in results if r["verified"])
            print(f"\n{verified_ct}/{len(results)} unknown companies resolved and verified "
                  f"({written} written to companies.yaml)")
    finally:
        c.close()
    print(f"\n{ok_count} ok, {fail_count} failed, {skip_count} skipped (no token)")
    return 0 if fail_count == 0 else 1


def write_discovery_results(results: list[dict]) -> int:
    """Patch companies.yaml in place for verified discoveries only, editing each
    matching line's `ats:`/`token:` fields directly so the file's grouping
    comments and hand-tuned formatting survive (a full yaml.safe_dump would
    flatten both)."""
    import re

    text = COMPANIES_PATH.read_text()
    written = 0
    for r in results:
        if not r["verified"]:
            continue
        name_rx = re.escape(r["name"])
        line_rx = re.compile(
            r'(-\s*\{name:\s*' + name_rx + r',.*?)ats:\s*unknown,(\s*)token:\s*null,(\s*)'
        )
        replacement = f'\\1ats: {r["ats"]},\\2token: {r["token"]},\\3verified: true, '
        new_text, n = line_rx.subn(replacement, text, count=1)
        if n:
            text = new_text
            written += 1
    if written:
        COMPANIES_PATH.write_text(text)
    return written


# ----------------------------------------------------------------- selftest

def cmd_selftest(args) -> int:
    failures = []

    def check(name, cond):
        (print(f"  ok   {name}") if cond else failures.append(name))
        if not cond:
            print(f"  FAIL {name}")

    # Pin to profile.yaml only: never read the user's real derived.yaml, so
    # these gate checks are deterministic whether or not a CV was uploaded
    # (codex-sol-12).
    profile = tracker.load_profile(derived_path=Path("/nonexistent/derived-selftest.yaml"))
    filt = tracker.Filter(profile)

    senior = sources.job(
        "Acme", "Senior Machine Learning Engineer", "https://example.com/j/1",
        location="Istanbul, Turkey", description="5+ years experience required. PyTorch.",
    )
    ok, score, reason = filt.evaluate(senior)
    check("senior role is rejected", ok is False and reason in
          ("seniority_title", "seniority_body"))

    junior = sources.job(
        "Acme", "Junior Data Scientist", "https://example.com/j/2",
        location="Istanbul, Turkey",
        description="New grad welcome. Python, pandas, scikit-learn, SQL.",
    )
    ok, score, reason = filt.evaluate(junior)
    check("Istanbul junior DS role is accepted", ok is True and score > 0)

    us_remote = sources.job(
        "Acme", "Machine Learning Engineer", "https://example.com/j/3",
        location="Remote (US only)", workplace="remote",
        description="Must be authorized to work in the United States. Python, ML.",
    )
    ok, score, reason = filt.evaluate(us_remote)
    check("US-only remote role is rejected", ok is False and reason == "remote_location_reject")

    emea_remote = sources.job(
        "Acme", "Machine Learning Engineer", "https://example.com/j/4",
        location="Remote - EMEA", workplace="remote",
        description="Work from anywhere in EMEA. Python, PyTorch, NLP.",
    )
    ok, score, reason = filt.evaluate(emea_remote)
    check("EMEA remote ML role is accepted", ok is True)

    bare_worldwide_remote = sources.job(
        "Acme", "Machine Learning Engineer", "https://example.com/j/4b",
        location="Remote - Worldwide", workplace="remote",
        description="Work from anywhere. Python, PyTorch, NLP.",
    )
    ok, score, reason = filt.evaluate(bare_worldwide_remote)
    check("bare-worldwide remote (no EMEA/TR signal) is rejected", ok is False)

    off_topic = sources.job(
        "Acme", "Account Executive", "https://example.com/j/5",
        location="Istanbul, Turkey", description="Sales quota, CRM, outbound.",
    )
    ok, score, reason = filt.evaluate(off_topic)
    check("sales role is rejected on topic", ok is False)

    ai_content = sources.job(
        "Dream Games", "AI Content Producer", "https://example.com/j/6",
        location="Istanbul", workplace="onsite",
        description="We are looking for an AI Content Producer to produce "
                     "AI videos internally for marketing purposes.",
    )
    ok, score, reason = filt.evaluate(ai_content)
    check("AI content-producer role is rejected", ok is False and reason == "topic_never_match")

    # The check above passes on the TITLE alone ("content producer"), so it says
    # nothing about topic_never_match_body. This one has a clean title and hides
    # its disqualifier in the body - revert the body gate and only this fails.
    body_only_marketing = sources.job(
        "Acme", "AI Engineer", "https://example.com/j/6b",
        location="Istanbul", workplace="onsite",
        description="Build AI tooling. You will produce videos for marketing "
                     "purposes and run our social media campaigns.",
    )
    ok, score, reason = filt.evaluate(body_only_marketing)
    check("clean title + marketing-only body is rejected on the body gate",
          ok is False and reason == "topic_never_match_body")

    # workplace unset + a location that only *implies* remote used to skip both
    # remote gates and pass on the country list's old 'anywhere' pattern.
    implied_remote_us = sources.job(
        "Acme", "Machine Learning Engineer", "https://example.com/j/4c",
        location="Anywhere (US only)", workplace="unknown", source="remoteok",
        description="Python, PyTorch, NLP.",
    )
    ok, score, reason = filt.evaluate(implied_remote_us)
    check("US-only 'Anywhere' with workplace unset hits the remote reject gate",
          ok is False and reason == "remote_location_reject")

    implied_remote_bare = sources.job(
        "Acme", "Machine Learning Engineer", "https://example.com/j/4d",
        location="Uzaktan", workplace="unknown", source="remoteok",
        description="Python, PyTorch, NLP.",
    )
    ok, score, reason = filt.evaluate(implied_remote_bare)
    check("bare 'Uzaktan' with workplace unset still needs a TR/EMEA signal",
          ok is False and reason == "location_reject")

    curated_hybrid_no_city = sources.job(
        "Insider", "Data Scientist", "https://example.com/j/11c",
        location="Hibrit", workplace="hybrid", source="lever:insiderone",
        description="Python, pandas, SQL.",
    )
    ok, score, reason = filt.evaluate(curated_hybrid_no_city)
    check("curated hybrid role with no city named is still accepted", ok is True)

    # A Turkish-only board carries no foreign postings, so "Hibrit" with no city
    # is still a Turkey job (this is Code2Work's "Veri Bilimci" program).
    tr_board_no_city = sources.job(
        "Code2Work", "Veri Bilimci Yetiştirme Programı", "https://example.com/j/15",
        location="Hibrit", workplace="hybrid", source="youthall",
        description="Python, veri bilimi, SQL.",
    )
    ok, score, reason = filt.evaluate(tr_board_no_city)
    check("Turkish-board role with no city named is accepted", ok is True)

    tr_board_ankara = sources.job(
        "Some Co", "Veri Bilimci", "https://example.com/j/16",
        location="Ankara / Türkiye", workplace="hybrid", source="techcareer",
        description="Python, veri bilimi.",
    )
    ok, score, reason = filt.evaluate(tr_board_ankara)
    check("Turkish-board pass does not reopen the non-Istanbul city reject",
          ok is False and reason == "city_reject")

    open_pool = sources.job(
        "Insider", "Genel Başvuru | Engelli Çalışan (Development Roles)",
        "https://example.com/j/7", location="Istanbul, Turkiye", workplace="onsite",
        description="Insider bünyesinde engelli çalışan olarak genel başvuru havuzu. "
                     "Python, machine learning, data.",
    )
    ok, score, reason = filt.evaluate(open_pool)
    check("generic open-application pool is rejected", ok is False)

    annotation_gig = sources.job(
        "iMerit", "AI Response Analyst", "https://example.com/j/8",
        location="Remote", workplace="remote",
        description="Rate and annotate AI model responses. Turkey ok.",
    )
    ok, score, reason = filt.evaluate(annotation_gig)
    check("annotation/rating gig is rejected", ok is False)

    boilerplate_leak = sources.job(
        "Mirantis", "Software Engineer", "https://example.com/j/9",
        location="Remote - EMEA", workplace="remote",
        description="We are the Kubernetes-native AI infrastructure company, "
                     "enabling modern AI, machine learning, and data-intensive "
                     "applications. Go, Python.",
    )
    ok, score, reason = filt.evaluate(boilerplate_leak)
    check("a software-engineer title is accepted without AI-keyword boilerplate",
          ok is True)

    ankara_unknown_wp = sources.job(
        "Some Co", "Yapay Zeka Mühendisi", "https://example.com/j/10",
        location="Ankara / Türkiye", workplace="unknown",
        description="Python, machine learning, yapay zeka.",
    )
    ok, score, reason = filt.evaluate(ankara_unknown_wp)
    check("Ankara onsite-ish role is rejected even with workplace=unknown",
          ok is False and reason == "city_reject")

    curated_country_only = sources.job(
        "Insider", "Software Engineer - (AI Native)", "https://example.com/j/11",
        location="Turkey", workplace="onsite", source="lever:insiderone",
        description="AI-native software engineer. Python, LLM, RAG.",
    )
    ok, score, reason = filt.evaluate(curated_country_only)
    check("curated-source onsite role with country-only location is accepted", ok is True)

    uncurated_country_only = sources.job(
        "Some Co", "Software Engineer - (AI Native)", "https://example.com/j/11b",
        location="Turkey", workplace="onsite", source="remoteok",
        description="AI-native software engineer. Python, LLM, RAG.",
    )
    ok, score, reason = filt.evaluate(uncurated_country_only)
    check("non-curated onsite role with country-only location is still rejected",
          ok is False and reason == "location_reject")

    iso_recent = sources.job(
        "Acme", "Data Scientist", "https://example.com/j/12",
        location="Istanbul", posted_at="2026-09-13T06:55:26.000Z",
        description="Python, pandas.",
    )
    iso_old = sources.job(
        "Acme", "Data Scientist", "https://example.com/j/13",
        location="Istanbul", posted_at="2024-01-01",
        description="Python, pandas.",
    )
    _, score_recent, _ = filt.evaluate(iso_recent)
    _, score_old, _ = filt.evaluate(iso_old)
    check("ISO-8601 posted_at earns the recency bonus", score_recent > score_old)

    # fetch_himalayas deliberately emits no date (its feed timestamp is not the
    # posting date). That only stays neutral while "" earns no recency bonus.
    no_date = sources.job(
        "Acme", "Data Scientist", "https://example.com/j/12b",
        location="Istanbul", posted_at="", description="Python, pandas.",
    )
    _, score_no_date, _ = filt.evaluate(no_date)
    check("missing posted_at is neutral, not treated as fresh",
          score_no_date == score_old and score_no_date < score_recent)

    substring_leak = sources.job(
        "Acme", "Data Scientist", "https://example.com/j/14", location="Istanbul",
        description="Object storage, average latency, digital transformation, legitimate access only.",
    )
    substring_baseline = sources.job(
        "Acme", "Data Scientist", "https://example.com/j/14b", location="Istanbul",
        description="",
    )
    _, score_leak, _ = filt.evaluate(substring_leak)
    _, score_base, _ = filt.evaluate(substring_baseline)
    check("'rag'/'git' skill weights don't match inside storage/average/digital/legitimate",
          score_leak == score_base)

    u1 = sources.make_uid("Acme", "Data Scientist", "https://x.com/job/1?utm_source=li")
    u2 = sources.make_uid("Acme", "Data Scientist", "https://x.com/job/1?utm_source=tw")
    u3 = sources.make_uid("Acme", "Data Scientist", "https://x.com/job/1/")
    check("uid stable across query-string / trailing-slash variants", u1 == u2 == u3)

    u4 = sources.make_uid("Acme", "Senior Data Scientist", "https://x.com/job/1")
    check("uid differs for a different title", u1 != u4)

    # ---------------------------------------------------------------- round 2
    # Every check below pins a hole that was live on 2026-09-14 and is verified
    # to flip if its fix is reverted. The first two groups are the stated hard
    # requirement (no content/marketing/creative/annotation roles); they are
    # paired deliberately, because the obvious way to satisfy them is a broad
    # pattern that also deletes the real ML roles named in the "kept" group.
    def verdict(title, **kw):
        kw.setdefault("description", "Python, ML.")
        kw.setdefault("location", "Istanbul, Türkiye")
        kw.setdefault("source", "lever:trendyol")
        return filt.evaluate(sources.job("Acme", title, "https://example.com/j/r2", **kw))

    for title in ["AI Marketer", "İçerik Üreticisi (AI)", "Yapay Zeka İçerik Editörü",
                  "Metin Yazarı (Yapay Zeka)", "AI Tasarımcı", "AI Seslendirme Uzmanı",
                  "AI Data Labeling Specialist", "AI Data Labeler", "AI Model Evaluator",
                  "LLM Output Reviewer", "AI Prompt Writer",
                  "Yapay Zeka Veri Etiketleme Uzmanı", "Yapay Zeka Veri Giriş Elemanı"]:
        ok, _, reason = verdict(title)
        check(f"off-target role rejected: {title}", ok is False and reason == "topic_never_match")

    # The mirror image: these carry the same tokens as a real ML problem domain,
    # and a bare 'brand'/'creative'/'annotat'/'satis' pattern kills all of them.
    for title in ["Data Scientist, Guest Satisfaction", "Machine Learning Engineer, Brand Safety",
                  "Generative AI Engineer, Creative Tools",
                  "Machine Learning Engineer, Annotation Infrastructure",
                  "Müşteri Analitiği Veri Bilimcisi"]:
        ok, _, _ = verdict(title)
        check(f"real ML role survives the never-list: {title}", ok is True)

    # Round 3 (2026-09-15, cross-vendor review). Each of these was reproduced as
    # a live defect before it was fixed, and each reject is paired with the keep
    # that the obvious over-broad fix would have destroyed.
    for title in ["AI Content Editor", "Yapay Zeka Video Editörü",
                  "Yapay Zeka Veri Anotasyon Uzmanı", "AI Evaluator",
                  "LLM Rater", "Video Montaj Uzmanı"]:
        ok, _, reason = verdict(title)
        check(f"off-target role rejected: {title}",
              ok is False and reason == "topic_never_match")
    # These pin the never-list only. "Model Evaluation Engineer" and "Prompt
    # Engineer" carry no ML topic word in the TITLE and the topic gate is
    # title-only for \bllm\b (see "an LLM mention in a body" below), so they are
    # rejected as no_topic_match either way - a separate rule, and not what the
    # widened editor/evaluator/anotasyon patterns are allowed to break.
    for title in ["AI Evaluation Platform Engineer", "Model Evaluation Engineer",
                  "Video Understanding Research Engineer", "Prompt Engineer",
                  "Data Scientist, Editorial Analytics"]:
        _, _, reason = verdict(title, description="LLM evaluation, RAG, Python.")
        check(f"never-list does not fire on real ML role: {title}",
              reason not in ("topic_never_match", "topic_never_match_body"))
    for title in ["AI Evaluation Platform Engineer",
                  "Video Understanding Research Engineer",
                  "Data Scientist, Editorial Analytics"]:
        ok, _, _ = verdict(title, description="LLM evaluation, RAG, Python.")
        check(f"real ML role survives the widened never-list: {title}", ok is True)

    # The body never-pattern used to fire on "create content" as a bare verb
    # phrase, which is ordinary generative-ML vocabulary.
    ok, _, _ = verdict("Generative AI Engineer", description=(
        "You will create content generation systems using diffusion models."))
    check("'create content generation systems' is not read as a content job", ok is True)
    ok, _, reason = verdict("AI Specialist", description=(
        "You will produce marketing videos for the brand team."))
    check("'produce marketing videos' is still rejected on the body",
          ok is False and reason == "topic_never_match_body")

    # NFC: the same Turkish title in composed and decomposed form must agree.
    nfd = unicodedata.normalize("NFD", "Yapay Zeka İçerik Üreticisi")
    check("a decomposed Turkish title cannot bypass the never-list",
          verdict(nfd)[0] is False and verdict("Yapay Zeka İçerik Üreticisi")[0] is False)

    # remote_reject reads the location and loses to city_ok; neither covers a
    # hard legal requirement stated in the body.
    ok, _, reason = verdict("ML Engineer", location="Remote - EMEA", workplace="remote",
                            source="remoteok",
                            description="Must have US work authorization. Python.")
    check("a US work-authorization demand beats an EMEA location",
          ok is False and reason == "remote_eligibility_reject")
    ok, _, reason = verdict("ML Engineer", location="Remote - Istanbul; US-only",
                            workplace="remote", source="remoteok")
    check("an exclusivity token beats a named Istanbul",
          ok is False and reason == "remote_eligibility_reject")
    ok, _, _ = verdict("ML Engineer", location="Remote - Istanbul, Dubai, London",
                       workplace="remote", source="remoteok")
    check("an ordinary multi-city list still lets Istanbul win", ok is True)

    # An absent location is unknown, not whatever the title happens to say.
    ok, _, reason = verdict("Istanbul Data Scientist", location="", workplace="unknown",
                            source="remoteok")
    check("an empty location is not inferred from the title",
          ok is False and reason == "location_reject")

    # A future date gave a negative day count, which satisfied "days <= 7".
    from datetime import timedelta as _td
    future = (datetime.now(timezone.utc).date() + _td(days=30)).isoformat()
    recent = (datetime.now(timezone.utc).date() - _td(days=1)).isoformat()
    check("a future posted_at does not earn the freshest bonus",
          verdict("Data Scientist", posted_at=future)[1]
          < verdict("Data Scientist", posted_at=recent)[1])

    ok, _, reason = verdict("Junior Data Scientist", workplace="onsite",
                             description="0-5 years of experience welcome. Python.")
    check("junior range '0-5 years' is not read as a 5-year demand", ok is True)
    ok, _, reason = verdict("Junior Data Scientist", workplace="onsite",
                             description="Our company was founded 6 years ago. Python, ML.")
    check("'founded 6 years ago' is not an experience demand", ok is True)
    ok, _, reason = verdict("Data Scientist", workplace="onsite",
                             description="We require 7+ years of experience. Python.")
    check("a genuine 7-year demand is still rejected",
          ok is False and reason == "seniority_body")

    # Geography reads the location field; the title is only a fallback.
    ok, _, reason = verdict("Data Scientist, Istanbul Office Support",
                             location="Ankara / Türkiye", workplace="onsite", source="techcareer")
    check("Istanbul in the TITLE cannot rescue an Ankara posting",
          ok is False and reason == "city_reject")
    ok, _, _ = verdict("Ankara Data Analyst", location="Remote - Europe", workplace="remote",
                        source="remoteok")
    check("a city in the title does not reject a Europe-remote role", ok is True)
    ok, _, _ = verdict("Remote Sensing Data Scientist", source="greenhouse:aselsan")
    check("'Remote Sensing' in a title is not a remote workplace", ok is True)

    # from_curated is a relaxation for VAGUE locations, not a blanket pass:
    # Trendyol's Lever board really does post Amsterdam and Berlin roles.
    for loc in ["Amsterdam, Netherlands", "Berlin, Germany", "Bengaluru, India"]:
        ok, _, reason = verdict("Data Scientist", location=loc, workplace="onsite")
        check(f"curated board does not whitelist {loc}",
              ok is False and reason == "location_reject")

    ok, _, _ = verdict("Machine Learning Engineer, Fleet Intelligence",
                        location="Anywhere in the World", workplace="remote", source="remoteok")
    check("'Fleet' does not satisfy the EET timezone signal", ok is False)
    ok, _, _ = verdict("Machine Learning Engineer",
                        location="Remote - Europe, Middle East and Africa",
                        workplace="remote", source="remoteok")
    check("'Middle East' inside the EMEA expansion is not a reject", ok is True)

    ok, _, reason = verdict("Yazılım Geliştirme Uzmanı(.Net)", source="techcareer",
                             description="C#, .NET, LLM servisleriyle entegrasyon.")
    check("an LLM mention in a body does not pass the topic gate",
          ok is False and reason == "no_topic_match")

    stacked = verdict("Junior Data Scientist Intern (New Grad, Entry-Level, Associate)",
                       workplace="onsite")[1]
    single = verdict("Junior Data Scientist", workplace="onsite")[1]
    check("seniority boost is awarded once, not once per matching pattern",
          stacked - single < profile["score_bonus"]["seniority_boost"])

    # ------------------------------------------------------- store + report
    conn = tracker.connect(":memory:")
    a = sources.job("A Co", "Data Scientist", "https://example.com/j/a",
                    location="Istanbul", source="lever:a", description="Python.")
    b = sources.job("B Co", "Data Scientist", "https://example.com/j/b",
                    location="Istanbul", source="lever:b", description="Python.")
    for x in (a, b):
        x["score"] = 1.0
    tracker.upsert_jobs(conn, [a, b], "r1")
    # Source b raised this run, so it reported nothing. Its jobs must survive.
    tracker.upsert_jobs(conn, [a], "r2")
    closed = tracker.mark_closed(conn, "r2", ["lever:a"])
    still = {r["uid"] for r in tracker.active_jobs(conn)}
    check("a source that errored does not get its jobs closed",
          closed == 0 and b["uid"] in still)
    # Now b reports and genuinely no longer lists the job.
    tracker.upsert_jobs(conn, [a], "r3")
    tracker.mark_closed(conn, "r3", ["lever:a", "lever:b"])
    check("a job a healthy source stopped listing is closed",
          b["uid"] not in {r["uid"] for r in tracker.active_jobs(conn)})
    # ...and comes back when the board lists it again.
    tracker.upsert_jobs(conn, [a, b], "r4")
    check("a closed job reopens when it reappears on the board",
          b["uid"] in {r["uid"] for r in tracker.active_jobs(conn)})

    tracker.set_status(conn, a["uid"], "ignored")
    tracker.upsert_jobs(conn, [b], "r5")
    tracker.mark_closed(conn, "r5", ["lever:a", "lever:b"])
    status = conn.execute("SELECT status FROM jobs WHERE uid=?", (a["uid"],)).fetchone()[0]
    check("a manually ignored job is not overwritten with 'closed'", status == "ignored")

    # -------------------------------------------- filter round 4 (re-review)
    # "AI Content Moderator" was KEPT with score 13. The pattern matches the
    # role noun only, so the engineer who builds moderation models survives.
    for title in ["AI Content Moderator", "Community Moderator",
                  "Yapay Zeka İçerik Moderatörü"]:
        ok, _, reason = verdict(title)
        check(f"moderation role rejected: {title}",
              ok is False and reason == "topic_never_match")
    for title in ["Content Moderation ML Engineer", "Trust and Safety ML Engineer",
                  "Machine Learning Engineer, Anotasyon Altyapısı"]:
        ok, _, _ = verdict(title)
        check(f"the moderation/annotation DOMAIN is still hirable: {title}", ok is True)
    # A bare 'anotasyon' rejected the infrastructure role above; the qualified
    # form must still reject the microwork one.
    ok, _, reason = verdict("Yapay Zeka Veri Anotasyon Uzmanı")
    check("a qualified anotasyon title is still rejected",
          ok is False and reason == "topic_never_match")

    # Negation: "you do NOT need to be authorized to work in the US" is an
    # explicitly OPEN posting and was being rejected as US-only.
    ok, _, _ = verdict("ML Engineer", location="Remote - EMEA", workplace="remote",
                       source="remoteok", description=(
                           "You do not need to be authorized to work in the US. Python."))
    check("a negated authorization sentence does not reject the posting", ok is True)
    for body in ["Must have US work authorization. Python.",
                 "You must be authorized to work in the United States. Python.",
                 "US citizenship required. Python."]:
        ok, _, reason = verdict("ML Engineer", location="Remote - EMEA",
                                workplace="remote", source="remoteok", description=body)
        check(f"a positive US-only requirement still rejects: {body[:28]}",
              ok is False and reason == "remote_eligibility_reject")

    # Every profile pattern must compile to what it reads as: YAML single
    # quotes keep a doubled backslash literally, and 'edit[öo]r[üu]?\\b' could
    # therefore never match anything.
    check("no profile pattern contains a literal double backslash",
          "\\\\" not in tracker.PROFILE_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------- degraded sources (round 4)
    # A paginator that runs out of pages while the payload still advertises
    # more must say so; one that ends naturally must stay silent, or every
    # healthy source ends up permanently exempt from the sweep.
    class _Resp:
        def __init__(self, p, status_code=200): self._p, self.status_code = p, status_code
        def raise_for_status(self): return self
        def json(self): return self._p

    class _Pages:
        def __init__(self, pages): self.pages, self.n = pages, 0
        def get(self, url, *a, **kw):
            p = self.pages[min(self.n, len(self.pages) - 1)]
            self.n += 1
            return _Resp(p)

    page = lambda cursor, pub=0: {  # noqa: E731
        "jobs": [{"companyName": "C", "title": "Data Scientist", "pubDate": pub,
                  "applicationLink": "https://example.com/j/h"}],
        "nextCursor": cursor}
    real_max, sources.MAX_PAGES = sources.MAX_PAGES, 2
    real_hmax, sources.HIMALAYAS_MAX_PAGES = sources.HIMALAYAS_MAX_PAGES, 2
    real_delay, sources.HIMALAYAS_PAGE_DELAY = sources.HIMALAYAS_PAGE_DELAY, 0
    now = time.time()
    try:
        sources.TRUNCATED.clear()
        got = sources.fetch_himalayas(_Pages([page("more")]))
        check("a paginator that hits the page cap reports itself truncated",
              "himalayas" in sources.TRUNCATED and len(got) == 2)
        sources.TRUNCATED.clear()
        sources.fetch_himalayas(_Pages([page("more"), page(None)]))
        check("a paginator that runs out of pages naturally is not truncated",
              sources.TRUNCATED == {})
        # The window, not the page cap, is what normally ends a himalayas walk:
        # one page inside HIMALAYAS_DAYS and the next one past it must stop the
        # loop WITHOUT marking the source truncated - the feed was read to the
        # depth asked for, so it stays sweepable.
        sources.TRUNCATED.clear()
        old_page = page("more", now - (sources.HIMALAYAS_DAYS + 1) * 86400)
        got = sources.fetch_himalayas(_Pages([page("more", now), old_page]))
        check("paging stops at the day window and is not called truncated",
              len(got) == 2 and sources.TRUNCATED == {})
        sources.TRUNCATED.clear()
        sources.fetch_himalayas(_Pages([page("more", now)]))
        check("a page still inside the window keeps paging until the cap",
              "himalayas" in sources.TRUNCATED)
        # A 429 partway through must keep what it has, not raise: raising loses
        # every job already collected AND files the source under errors, where
        # the sweep would read its absence as failure rather than a short read.
        sources.TRUNCATED.clear()
        limited = _Pages([page("more", now)])
        limited.pages.append({})
        real_get = limited.get
        limited.get = lambda *a, **kw: (  # noqa: E731
            _Resp({}, 429) if limited.n else real_get(*a, **kw))
        got = sources.fetch_himalayas(limited)
        check("a 429 mid-walk keeps the jobs already fetched and marks truncated",
              len(got) == 1 and "429" in sources.TRUNCATED.get("himalayas", ""))
    finally:
        sources.MAX_PAGES = real_max
        sources.HIMALAYAS_MAX_PAGES = real_hmax
        sources.HIMALAYAS_PAGE_DELAY = real_delay
        sources.TRUNCATED.clear()

    class _Post:
        def __init__(self, payload): self.payload, self.calls = payload, 0
        def post(self, url, **kw):
            self.calls += 1
            return _Resp(self.payload)

    sources.TRUNCATED.clear()
    p = _Post({"results": [{"title": "Data Scientist", "shortcode": "x"}], "total": 100})
    sources.fetch_workable("acme", "Acme", p)
    check("an unpaginated workable board is fetched once and reported truncated",
          p.calls == 1 and "workable:acme" in sources.TRUNCATED)
    sources.TRUNCATED.clear()
    p = _Post({"results": [{"title": "Data Scientist", "shortcode": "x"}], "total": 1})
    sources.fetch_workable("acme", "Acme", p)
    check("a workable board that fits in one response is not truncated",
          p.calls == 1 and sources.TRUNCATED == {})

    z = sources.job("Z Co", "Data Scientist", "https://example.com/j/z",
                    location="Istanbul", source="himalayas", description="Python.")
    z["score"] = 1.0
    tracker.upsert_jobs(conn, [z], "r7")
    check("active_count sees a live row and ignores a source with none",
          tracker.active_count(conn, "himalayas") == 1
          and tracker.active_count(conn, "nosuch") == 0)

    # Zero jobs off an HTTP 200 is not authorization to close the history.
    check("a source that parsed 0 jobs while holding live rows is degraded",
          degraded_sources(conn, {"himalayas": 0}) != {})
    check("a source that parsed 0 jobs and holds none is not degraded",
          degraded_sources(conn, {"nosuch": 0}) == {})
    # ...and the exemption lasts one run, or a board that has genuinely gone
    # empty keeps its dead jobs alive forever.
    conn.execute("INSERT OR REPLACE INTO runs (run_id, summary) VALUES (?, ?)",
                 ("r6.5", json.dumps({"per_source": {"himalayas": 0}})))
    check("a second consecutive zero is read as a genuinely empty board",
          degraded_sources(conn, {"himalayas": 0}) == {})
    conn.execute("DELETE FROM runs")
    sources.TRUNCATED["himalayas"] = "fetched 200 of more"
    try:
        check("a truncated source is degraded even with jobs in hand",
              "himalayas" in degraded_sources(conn, {"himalayas": 200}))
        # Prefix matching used to be deliberately loose here, which let one
        # truncated board exempt an unrelated one whose tag started the same.
        check("a truncated tag does not degrade a source that merely starts alike",
              degraded_sources(conn, {"himalayasXL": 1}) == {})
    finally:
        sources.TRUNCATED.clear()
    check("a healthy source with jobs is left sweepable",
          degraded_sources(conn, {"himalayas": 200}) == {})

    # The sweep must still close a healthy source, or nothing ever expires.
    tracker.upsert_jobs(conn, [z], "r8")
    tracker.mark_closed(conn, "r8", [t for t in {"himalayas": 1}
                                     if t not in degraded_sources(conn, {"himalayas": 1})])
    check("a degraded source's jobs survive the sweep it is excluded from",
          z["uid"] in {x["uid"] for x in tracker.active_jobs(conn)})
    sources.TRUNCATED["himalayas"] = "fetched 1 of more"
    try:
        per = {"himalayas": 1}
        tracker.upsert_jobs(conn, [], "r9")
        tracker.mark_closed(conn, "r9", [t for t in per
                                         if t not in degraded_sources(conn, per)])
        check("a truncated source does not close the jobs it failed to re-read",
              z["uid"] in {x["uid"] for x in tracker.active_jobs(conn)})
    finally:
        sources.TRUNCATED.clear()
    tracker.upsert_jobs(conn, [], "r10")
    tracker.mark_closed(conn, "r10", [t for t in {"himalayas": 1}
                                      if t not in degraded_sources(conn, {"himalayas": 1})])
    check("a healthy source still closes what it stopped listing",
          z["uid"] not in {x["uid"] for x in tracker.active_jobs(conn)})

    hostile = sources.job(
        "Evil", "Data Scientist", 'javascript:alert(document.cookie)',
        location="Istanbul", source="lever:x", description="Python.",
        workplace='x"><script>alert(1)</script><span class="',
    )
    hostile["score"] = 1.0
    tracker.upsert_jobs(conn, [hostile], "r6")
    page = report.render_report(conn, profile, "r6", {"per_source": {"lever:x": 1}})
    check("a board cannot inject script via the workplace badge class",
          "<script>alert(1)</script>" not in page)
    check("a javascript: url is not rendered as a link", "javascript:alert" not in page)

    degraded = report.render_report(conn, profile, "r6",
                                      {"per_source": {}, "errors": ["techcareer: ReadTimeout"]})
    check("a run that lost a source says so in the report",
          "techcareer: ReadTimeout" in degraded)
    page2 = report.render_report(conn, profile, "r6",
                                   {"per_source": {}, "degraded": {"himalayas": "capped"}})
    check("a run that under-reported a source says so in the report",
          "himalayas: capped" in page2)
    conn.close()

    # Lever ships the skills bullets outside descriptionPlain, and its
    # workplaceType is top-level - both were being dropped.
    payload = [{
        "text": "Data Scientist", "hostedUrl": "https://jobs.lever.co/x/1",
        "categories": {"location": "Istanbul / Maslak", "commitment": "Full-time"},
        "workplaceType": "hybrid", "createdAt": 1757000000000,
        "descriptionPlain": "About the team.",
        "descriptionBodyPlain": "You will build models.",
        "lists": [{"text": "Qualities", "content": "<li>PyTorch and scikit-learn</li>"}],
    }]

    class _FakeResp:
        def raise_for_status(self): return self
        def json(self): return payload

    class _FakeClient:
        def get(self, url, **kw): return _FakeResp()

    lev = sources.fetch_lever("x", "X Co", _FakeClient())[0]
    check("lever description includes the body and the bullet lists",
          "PyTorch" in lev["description"] and "You will build models" in lev["description"])
    check("lever reads workplaceType from the top level", lev["workplace"] == "hybrid")

    check("mojibake location is repaired",
          sources.unmojibake("Ø¯Ø¨Ù\x8a") == "دبي")
    check("already-correct text survives the repair untouched",
          sources.unmojibake("İstanbul / Türkiye") == "İstanbul / Türkiye"
          and sources.unmojibake("Data Scientist") == "Data Scientist")

    # A read timeout on one page used to kill the whole source: youthall lost
    # its 36-job board that way on 2026-09-14. Server stalls the first request
    # past the timeout and answers the second one at once.
    import http.server
    import threading

    hits, lock = [], threading.Lock()

    class _Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                first = not hits
                hits.append(1)
            if first:
                time.sleep(2)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            try:
                self.wfile.write(b"ok")
            except BrokenPipeError:
                # The intentional timeout closes the first test client before
                # this slow handler wakes up; that is expected, not test noise.
                pass

        def log_message(self, *a): pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real_timeout, sources.TIMEOUT = sources.TIMEOUT, httpx.Timeout(0.7, connect=5.0)
    try:
        with sources.client() as slow_c:
            body = slow_c.get(f"http://127.0.0.1:{srv.server_address[1]}/x").text
        check("a read timeout is retried instead of killing the source",
              body == "ok" and len(hits) == 2)
    except httpx.TimeoutException:
        check("a read timeout is retried instead of killing the source", False)
    finally:
        sources.TIMEOUT = real_timeout
        srv.shutdown()

    # ---------------------------------------------------- CV + config seam
    # All CV tests inject temp profile/derived/skills/db/out paths so they never
    # read the user's real files, and existing tests stay deterministic even if
    # the user has a derived.yaml sitting in the project (codex-sol-12).
    import cv as _cv
    import shlex as _shlex
    tmp = Path(tempfile.mkdtemp())

    # config boundary: a hostile derived cannot blank the exclusions or gates -
    # only the allowlisted keys merge (codex-sol-1).
    (tmp / "derived.yaml").write_text(yaml.safe_dump({
        "topic_never_match": [], "location_accept_country": ["anywhere"],
        "seniority_reject_title": [], "skill_weights": {"python": 5},
        "topic_must_match": ["(?<!\\w)python(?!\\w)"],
        "topic_must_match_weak": ["(?<!\\w)python(?!\\w)"],
        "seniority_boost": ["\\bjunior\\b"],
    }), encoding="utf-8")
    merged = tracker.load_profile(derived_path=tmp / "derived.yaml")
    check("a hostile derived cannot blank topic_never_match", bool(merged["topic_never_match"]))
    check("a hostile derived cannot inject geography",
          "anywhere" not in merged.get("location_accept_country", []))
    check("a derived overrides only the allowlisted skill_weights",
          merged["skill_weights"] == {"python": 5})
    hostile_filt = tracker.Filter(merged)
    creator = {"title": "AI Content Creator", "description": "Create content with AI. Python.",
               "company": "X", "source": "lever:x", "workplace": "remote",
               "location": "Istanbul", "url": "https://x/1"}
    check("no content-creator role is accepted under a hostile derived",
          not hostile_filt.evaluate(creator)[0])

    # no-derived parity: load_profile with no derived == profile.yaml verbatim
    # for the allowlisted keys (codex-sol-2).
    base = tracker.load_profile(derived_path=tmp / "does-not-exist.yaml")
    raw = yaml.safe_load(tracker.PROFILE_PATH.read_text(encoding="utf-8"))
    check("no derived.yaml -> profile.yaml is used unchanged",
          all(base.get(k) == raw.get(k) for k in tracker.DERIVED_KEYS))

    # malformed derived YAML is a load error the caller sees, not a silent wipe.
    (tmp / "bad.yaml").write_text("skill_weights: [this is: not, valid", encoding="utf-8")
    try:
        tracker.load_profile(derived_path=tmp / "bad.yaml"); malformed_ok = False
    except Exception:
        malformed_ok = True
    check("a malformed derived.yaml raises instead of writing garbage", malformed_ok)

    # parser: a java-backend CV emits ALIASES, and a Spring/AWS job that never
    # says "backend" is accepted by the merged filter (codex-sol-3).
    (tmp / "cv.txt").write_text(
        "Ayberk Karataban - Backend Engineer\n"
        "Summary: Senior backend engineer with 6 years of experience designing "
        "and operating production services. Strong in Java and Spring Boot, "
        "deploying to AWS (EC2, S3, Lambda). Comfortable with PostgreSQL, Docker, "
        "Kubernetes, Kafka and REST APIs. Uses Python for tooling and automation, "
        "with CI/CD via Jenkins and GitHub Actions. Led a small team and mentored "
        "junior developers on microservice architecture and observability.\n",
        encoding="utf-8")
    rc = _cv.parse_cv(tmp / "cv.txt", skills_path=_cv.SKILLS_PATH,
                      derived_path=tmp / "d1.yaml")
    d1 = yaml.safe_load((tmp / "d1.yaml").read_text())
    check("cv parse succeeds on a normal CV", rc == 0)
    check("cv parse emits aliases, not the label 'aws'",
          "amazon web services" in d1["skill_weights"] and "ec2" in d1["skill_weights"])
    jf = tracker.Filter(tracker.load_profile(derived_path=tmp / "d1.yaml"))
    spring_job = {"title": "Software Engineer", "company": "X", "source": "lever:x",
                  "workplace": "remote", "location": "Istanbul", "url": "https://x/1",
                  "description": "Build with Spring Boot on Amazon Web Services. PostgreSQL."}
    check("a Spring/AWS job that never says 'backend' is accepted after cv parse",
          jf.evaluate(spring_job)[0])
    check("matched_skills names the skills that scored a job",
          "spring boot" in jf.matched_skills(spring_job))

    # parse_cv_to_derived is the pure core the panel upload reuses: same rules,
    # but returns (derived, summary) and raises instead of writing/printing.
    cv_text = (tmp / "cv.txt").read_text()
    derived_mem, summary_mem = _cv.parse_cv_to_derived(cv_text, skills_path=_cv.SKILLS_PATH)
    check("parse_cv_to_derived returns only allowlisted keys",
          set(derived_mem) <= tracker.DERIVED_KEYS)
    check("parse_cv_to_derived summary lists detected skills", "aws" in summary_mem["skills"])
    try:
        _cv.parse_cv_to_derived("java", skills_path=_cv.SKILLS_PATH)
        _raised = False
    except ValueError:
        _raised = True
    check("parse_cv_to_derived raises on a too-short CV", _raised)

    # seniority reads date ranges (not just "N years"), scoped to the Experience
    # section so education/project spans don't inflate it, and merges overlaps.
    check("seniority derives years from Experience-section date ranges",
          _cv.detect_seniority("Experience\nEngineer Jan 2020 - Dec 2023\n"
                               "Education\nBSc 2012 - 2016") == ("mid", 4))
    check("a past internship does not pin a multi-year CV to junior",
          _cv.detect_seniority("Experience\nSoftware Intern 2018 - 2024")[0] == "senior")

    # Filter.score_only re-ranks without gating (the panel re-scores the pool
    # against a per-user CV): a skill-matching job outscores a skill-less one,
    # and a job evaluate() would reject still gets a number - never dropped.
    _hi = {"title": "Spring Boot Engineer", "description": "Amazon Web Services, PostgreSQL",
           "location": "Istanbul", "workplace": "remote", "posted_at": ""}
    _lo = {"title": "Office Manager", "description": "scheduling and filing",
           "location": "Istanbul", "workplace": "remote", "posted_at": ""}
    check("score_only ranks a skill-matching job above a skill-less one",
          jf.score_only(_hi) > jf.score_only(_lo))
    check("score_only never gates (returns a float for a would-be-rejected job)",
          isinstance(jf.score_only(_lo), float))

    # parser is transactional: too little text preserves the prior derived and
    # returns nonzero (codex-sol-4).
    (tmp / "d2.yaml").write_text("skill_weights: {python: 5}\n", encoding="utf-8")
    before = (tmp / "d2.yaml").read_text()
    (tmp / "tiny.txt").write_text("java", encoding="utf-8")
    rc_fail = _cv.parse_cv(tmp / "tiny.txt", skills_path=_cv.SKILLS_PATH,
                           derived_path=tmp / "d2.yaml")
    check("a too-short CV leaves the prior derived.yaml untouched and fails",
          rc_fail != 0 and (tmp / "d2.yaml").read_text() == before)

    # punctuation aliases need per-alias boundaries, not the uniform one
    # (codex-sol-6).
    check(".NET is detected by boundary_for", bool(_cv.boundary_for(".net").search("Built .NET services")))
    check("C++ is detected by boundary_for", bool(_cv.boundary_for("c++").search("Strong C++ skills")))
    check("C# is detected by boundary_for", bool(_cv.boundary_for("c#").search("C# and Java")))
    check(".net does not fire inside asp.net", not _cv.boundary_for(".net").search("uses asp.net here"))
    check("java is not matched inside javascript",
          not _cv.boundary_for("java").search("strong javascript skills"))

    # builder: latex_escape is single-pass (a backslash's own braces are not
    # re-escaped) (codex-sol-8).
    esc = _cv.latex_escape(r"R&D 100% _x_ #1 \ {y} ~ ^ $z")
    check("latex_escape covers all specials",
          all(t in esc for t in (r"\&", r"\%", r"\_", r"\#", r"\{", r"\}", r"\$",
                                 r"\textbackslash{}", r"\textasciitilde{}", r"\textasciicircum{}")))
    check("latex_escape is single-pass (no re-escaped braces)",
          r"\textbackslash\{\}" not in esc)

    # placeholder fill: one pass, user <<X>> in a value survives, missing/dup fail.
    check("fill_template does not re-substitute <<X>> inside a value",
          _cv.fill_template("<<A>> <<B>>", {"A": "x", "B": "y<<A>>z"}) == "x y<<A>>z")
    try:
        _cv.fill_template("<<A>> <<MISSING>>", {"A": "x"}); miss_ok = False
    except KeyError:
        miss_ok = True
    check("fill_template fails on a missing placeholder value", miss_ok)
    try:
        _cv.fill_template("<<A>> <<A>>", {"A": "x"}); dup_ok = False
    except ValueError:
        dup_ok = True
    check("fill_template fails on a duplicate placeholder", dup_ok)

    # bullet selection: relevant bullet ranks in, a no-overlap experience keeps
    # its min-N, chronology preserved, stable ties (codex-sol-10).
    bullets = [{"text": "b0", "skills": []}, {"text": "b1", "skills": ["kubernetes"]},
               {"text": "b2", "skills": []}, {"text": "b3", "skills": ["aws"]}]
    sel, om = _cv.select_bullets(bullets, {"kubernetes"}, min_n=2, max_n=4)
    check("select_bullets keeps the overlapping bullet", any(b["text"] == "b1" for b in sel))
    check("select_bullets preserves chronological order",
          [b["text"] for b in sel] == sorted(b["text"] for b in sel))
    sel2, _ = _cv.select_bullets([{"text": "x", "skills": []}, {"text": "y", "skills": []},
                                  {"text": "z", "skills": []}], set(), min_n=2, max_n=4)
    check("a no-overlap experience still keeps its min-N bullets", len(sel2) == 2)
    sel3, om3 = _cv.select_bullets(
        [{"text": f"o{i}", "skills": ["kubernetes"]} for i in range(5)],
        {"kubernetes"}, min_n=2, max_n=4)
    check("every overlapping bullet survives for ATS (no cap drop)",
          len(sel3) == 5 and om3 == [])

    # ATS keyword surfacing: intersection of {job mentions} and {CV has}, in the
    # job's spelling; never a skill the candidate lacks (no-invent contract).
    sk_ats = _cv.load_skills(_cv.SKILLS_PATH)
    mast_ats = {"summary": "Backend engineer.",
                "experiences": [{"company": "C", "role": "R", "dates": "2023-",
                                 "bullets": [{"text": "Ran kubernetes clusters.",
                                              "skills": ["kubernetes"]}]}],
                "education": [{"school": "Bogazici", "degree": "BSc CmpE",
                               "dates": "2017-2021"}],
                "skills": {"Cloud": ["Kubernetes", "Python"]}}
    ats_kw = _cv.ats_keywords(mast_ats, "Platform Engineer",
                              "We need k8s and golang.", sk_ats)
    check("ATS surfaces a job keyword the candidate has", "k8s" in ats_kw)
    check("ATS never emits a skill the candidate lacks (golang)",
          "golang" not in ats_kw)
    check("Key Skills section renders in the job's spelling",
          "k8s" in _cv._ats_tex(ats_kw) and "Key Skills" in _cv._ats_tex(ats_kw))
    edu_tex = _cv._education_tex(mast_ats["education"])
    check("education always renders (static section)",
          "Education" in edu_tex and "BSc CmpE" in edu_tex)
    check("education folds its heading when empty", _cv._education_tex([]) == "")

    # copyable command is shlex-quoted and the uid alphabet is hex (codex-sol-11).
    u = sources.make_uid("Acme", "Data Scientist", "https://x/1")
    check("make_uid is [0-9a-f]{16}", bool(re.fullmatch(r"[0-9a-f]{16}", u)))
    check("the copyable cv build command is shlex-safe",
          _shlex.quote(u) == u)  # hex needs no quoting, and quoting is a no-op

    # one real xelatex compile of a fixture with every special char + Turkish,
    # skipped with a note if xelatex is absent (codex-sol-8, codex-sol-9).
    if _cv.XELATEX:
        (tmp / "master.yaml").write_text(yaml.safe_dump({
            "name": "Ayberk çğşıöü İ", "contact": {"email": "a_b&c@x.com", "location": "İstanbul"},
            "summary": "100% Python, C++ & C#. {x} ~ ^ _", "experiences": [
                {"company": "Acme & Co", "role": "Sr. Engineer", "dates": "2023-",
                 "bullets": [{"text": "Kubernetes on AWS.", "skills": ["kubernetes", "aws"]},
                             {"text": "Docs.", "skills": []}]}],
            "skills": {"Diller": ["Python", "C++", "C#"]},
        }, allow_unicode=True), encoding="utf-8")
        dbp = tmp / "jobs.db"
        c2 = tracker.connect(str(dbp))
        j2 = sources.job("X", "Kubernetes Engineer", "https://x/2", location="Istanbul",
                         source="lever:x", workplace="remote", posted_at="2026-09-10",
                         description="Kubernetes and AWS.")
        j2["score"] = 1.0
        tracker.upsert_jobs(c2, [j2], "r1"); c2.close()
        outp = tmp / "out.pdf"
        rc_b = _cv.build_cv(j2["uid"], out=str(outp), master_path=tmp / "master.yaml",
                            template_path=_cv.TEMPLATE_PATH, skills_path=_cv.SKILLS_PATH,
                            db_path=str(dbp))
        check("a real xelatex compile produces a non-empty PDF (Turkish + escaped)",
              rc_b == 0 and outp.exists() and outp.stat().st_size > 1000)
    else:
        print("  skip xelatex compile check (xelatex not installed)")


    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


def cmd_cv(args) -> int:
    import cv
    if args.cv_cmd == "parse":
        return cv.parse_cv(args.path)
    if args.cv_cmd == "build":
        return cv.build_cv(args.uid, out=args.out)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="pull all sources, filter, score, store")
    p.add_argument("--source", nargs="*", help="restrict to these source tags, e.g. lever:trendyol")
    p.add_argument("--browser", action="store_true",
                    help="compatibility flag; browser sources are included unless --http-only is set")
    p.add_argument("--http-only", action="store_true",
                    help="skip Playwright-backed sources for a fast API/HTML-only scan")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("audit", help="sync catalogue coverage and reclassify stored jobs without fetching")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("report", help="render report.html from the DB")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("panel", help="serve the local React dashboard")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to bind (default: localhost only)")
    p.add_argument("--port", type=int, default=8000,
                   help="port to serve on (default: 8000)")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("mark", help="set a job's status")
    p.add_argument("uid")
    p.add_argument("status", choices=["new", "interested", "applied", "rejected", "ignored"])
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("discover", help="verify companies.yaml ATS tokens")
    p.add_argument("--only", help="comma-separated company names/tokens to check")
    p.add_argument("--auto", action="store_true",
                    help="also crawl ats:unknown companies with Playwright and try to "
                         "find + verify their ATS token (writes verified hits back to companies.yaml)")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("selftest", help="assert-based checks on the filter logic")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("cv", help="parse a CV to derived.yaml, or build a job-tailored CV")
    cvsub = p.add_subparsers(dest="cv_cmd", required=True)
    q = cvsub.add_parser("parse", help="parse CV (pdf/txt/md) -> derived.yaml (then run fetch)")
    q.add_argument("path", help="path to your CV (.pdf/.txt/.md)")
    q = cvsub.add_parser("build", help="build a job-tailored LaTeX CV for a job uid")
    q.add_argument("uid", help="job uid from the report")
    q.add_argument("--out", help="output PDF path (default cv/<uid>.pdf)")
    p.set_defaults(func=cmd_cv)

    args = ap.parse_args()
    try:
        return args.func(args)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
