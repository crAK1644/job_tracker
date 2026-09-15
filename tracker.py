"""SQLite store, CV-based filtering/scoring, and HTML report rendering."""

from __future__ import annotations

import html
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

DB_PATH = Path(__file__).parent / "jobs.db"
PROFILE_PATH = Path(__file__).parent / "profile.yaml"
DERIVED_PATH = Path(__file__).parent / "derived.yaml"
# Only these keys may come from a CV-generated derived.yaml. Everything
# else - exclusions (topic_never_match), geography, remote policy, hard
# seniority rejection - stays hand-set in profile.yaml and a CV can never
# override it. This allowlist is the "no AI content creator can be parsed
# away" guarantee, not the parser politely emitting a subset.
DERIVED_KEYS = {"skill_weights", "topic_must_match",
                "topic_must_match_weak", "seniority_boost"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid          TEXT PRIMARY KEY,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    location     TEXT,
    workplace    TEXT,
    posted_at    TEXT,
    source       TEXT,
    description  TEXT,
    raw_seniority TEXT,
    score        REAL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'new',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    run_id       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
-- Was created ad hoc by cmd_fetch just before its INSERT. It belongs here now
-- that degraded_sources READS the previous run's summary: a reader must not
-- have to depend on a writer further down the same function having run first.
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, summary TEXT);
"""


def connect(path=None) -> sqlite3.Connection:
    # path is for the selftest's ":memory:" database - the store and report
    # checks need a real schema they can write to without touching jobs.db.
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load_profile(profile_path=None, derived_path=None) -> dict:
    """profile.yaml is the complete, hand-set config. A CV-generated
    derived.yaml, if present, overrides ONLY the DERIVED_KEYS allowlist -
    unknown keys in it are ignored, so no CV can touch the exclusions or
    the geo/remote/hard-seniority gates. No derived.yaml -> behaves exactly
    as profile.yaml alone. Paths are injectable for the selftest."""
    profile_path = profile_path or PROFILE_PATH
    derived_path = derived_path or DERIVED_PATH
    p = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if Path(derived_path).exists():
        d = yaml.safe_load(Path(derived_path).read_text(encoding="utf-8")) or {}
        for k in DERIVED_KEYS:
            if k in d:
                p[k] = d[k]
    return p


# --------------------------------------------------------------------- filter

class Filter:
    """Compiles profile.yaml once; call .evaluate(job) per posting."""

    def __init__(self, profile: dict):
        self.p = profile
        c = lambda pats: [re.compile(p, re.I) for p in pats]  # noqa: E731
        self.topic_must = c(profile.get("topic_must_match", []))
        self.topic_must_weak = c(profile.get("topic_must_match_weak", []))
        self.topic_never = c(profile.get("topic_never_match", []))
        self.topic_never_body = c(profile.get("topic_never_match_body", []))
        self.sen_reject_title = c(profile.get("seniority_reject_title", []))
        self.sen_reject_body = c(profile.get("seniority_reject_body", []))
        self.sen_boost = c(profile.get("seniority_boost", []))
        self.loc_city = c(profile.get("location_accept_city", []))
        self.loc_country = c(profile.get("location_accept_country", []))
        self.loc_reject_city = c(profile.get("location_reject_city", []))
        self.loc_remote_hint = c(profile.get("location_remote_hint", []))
        self.loc_no_geo = c(profile.get("location_no_geo", []))
        self.tr_sources = set(profile.get("turkey_only_sources") or [])
        self.remote_ok = c(profile.get("remote_ok", []))
        self.remote_reject = c(profile.get("remote_reject", []))
        self.remote_elig_reject = c(profile.get("remote_eligibility_reject", []))
        self.skill_weights = {k.lower(): v for k, v in
                               (profile.get("skill_weights") or {}).items()}
        # Bare substrings mis-fire ("rag" hits storage/average/fragment, "git"
        # hits digital/legitimate). Lookarounds not \b - keywords like "c#" and
        # "scikit-learn" end in non-word characters \b wouldn't match on.
        self.skill_rx = [(re.compile(rf"(?<!\w){re.escape(k)}(?!\w)", re.I), k, v)
                          for k, v in self.skill_weights.items()]
        self.bonus = profile.get("score_bonus", {})

    def evaluate(self, j: dict) -> tuple[bool, float, str]:
        """Returns (keep, score, reject_reason)."""
        # NFC first, or a decomposed Turkish character defeats every pattern
        # below: "Yapay Zeka \u0130\u00e7erik \u00dcreticisi" was rejected in its composed
        # form and KEPT when the same string arrived with combining accents.
        # Boards do emit both. Normalize once here rather than per pattern.
        n = lambda s: unicodedata.normalize("NFC", s or "")  # noqa: E731
        title = n(j.get("title"))
        desc = n(j.get("description"))
        loc = n(j.get("location"))
        workplace = (j.get("workplace") or "").lower()
        blob = f"{title} {desc}"

        if any(rx.search(title) for rx in self.topic_never):
            return False, 0, "topic_never_match"
        if any(rx.search(blob) for rx in self.topic_never_body):
            return False, 0, "topic_never_match_body"
        topic_hit = any(rx.search(blob) for rx in self.topic_must) or \
            any(rx.search(title) for rx in self.topic_must_weak)
        if (self.topic_must or self.topic_must_weak) and not topic_hit:
            return False, 0, "no_topic_match"

        if any(rx.search(title) for rx in self.sen_reject_title):
            return False, 0, "seniority_title"
        if any(rx.search(desc) for rx in self.sen_reject_body):
            return False, 0, "seniority_body"

        # Geography is read off the location field ONLY. Concatenating the title
        # let a job NAME decide a job's geography in both directions: "Data
        # Scientist, Istanbul Office Support" in Ankara passed the city gate
        # (and so skipped the city reject entirely), while "Ankara Data Analyst
        # | Remote" was rejected on its title. The title was still used as a
        # fallback for an empty location until 2026-09-15, which had the same
        # defect in miniature: "Istanbul Data Scientist" with no location at all
        # cleared the city gate on its title. An absent location is unknown, and
        # unknown is what the gates below are already built to handle - the
        # 6 postings with an empty location today are all youthall, which
        # establishes its country through turkey_only_sources instead.
        geo = loc
        city_ok = any(rx.search(geo) for rx in self.loc_city)
        if not city_ok and any(rx.search(geo) for rx in self.loc_reject_city):
            return False, 0, "city_reject"
        country_ok = any(rx.search(geo) for rx in self.loc_country)
        # A posting off a curated companies.yaml board ("{ats}:{token}" source
        # ids, vs. a bare board name) comes from a company we already know is
        # Istanbul-based - but only its VAGUE locations may be trusted to mean
        # Istanbul. Trendyol's Lever board genuinely posts Amsterdam and Berlin
        # roles, so a location naming a real foreign place is taken at its word.
        src = j.get("source") or ""
        curated_ok = ":" in src and (
            country_ok or any(rx.search(geo) for rx in self.loc_no_geo)
        )
        # A Turkish-only board establishes the country by itself, so it relaxes
        # the remote gate too. A curated companies.yaml entry does NOT: that
        # list holds remote-first foreign employers whose "Remote" postings are
        # exactly the ones that still need to name a real geography.
        tr_source = src in self.tr_sources
        # Trust the location string over the workplace field: boards routinely
        # ship "Anywhere"/"Uzaktan" with workplace unset, and treating those as
        # onsite let them bypass the remote gates entirely.
        is_remote = workplace == "remote" or any(rx.search(loc) for rx in self.loc_remote_hint)
        if is_remote:
            # city_ok wins over the reject list: a multi-region posting that
            # literally names Istanbul ("Remote - Istanbul, Dubai, London") is
            # reachable, and rejecting it on one of its other cities dropped the
            # highest-value remote hits we get.
            if not city_ok and any(rx.search(geo) for rx in self.remote_reject):
                return False, 0, "remote_location_reject"
            # remote_reject reads the location string and loses to city_ok on
            # purpose. Neither is enough on its own: "Remote - EMEA" whose body
            # demands US work authorization was kept, and so was a location
            # reading "Remote - Istanbul; US-only". These patterns read the
            # BODY too and beat city_ok, because they state a hard legal
            # requirement rather than list one more place.
            if any(rx.search(blob) or rx.search(geo) for rx in self.remote_elig_reject):
                return False, 0, "remote_eligibility_reject"
            # A remote posting needs an explicit EMEA/Turkey signal to count -
            # bare "Remote"/"Worldwide"/"Anywhere" with no geography is too
            # ambiguous on aggregator boards (remoteok/weworkremotely/himalayas)
            # to assume it's reachable from Istanbul. Not relaxed for curated
            # companies either: the curated list holds remote-first foreign
            # employers (GitLab/Canonical/Toggl) whose remote roles are exactly
            # the ones that need a real geography before being trusted.
            location_ok = city_ok or tr_source or any(rx.search(geo) for rx in self.remote_ok)
        else:
            # Onsite must be Istanbul itself; hybrid/unknown also accept a
            # country-level match ("Turkey" with no city named). A curated
            # known-Istanbul employer and a Turkey-only board both pass either
            # way when the location field says nothing useful - tr_source has to
            # be consulted on this branch too, or a techcareer posting whose
            # workplace happens to read "onsite" is dropped while the identical
            # "hybrid" one is kept.
            location_ok = (city_ok or curated_ok or tr_source) if workplace == "onsite" else (
                city_ok or curated_ok or tr_source or country_ok
            )
        if not location_ok:
            return False, 0, "location_reject"

        score = 0.0
        for rx, _key, weight in self.skill_rx:
            if rx.search(blob):
                score += weight

        # Awarded once, not once per matching pattern. The patterns are near-
        # synonyms ("junior", "new grad", "entry-level", "associate") and they
        # match the body too, so stacking them paid +88 to a title that stuffed
        # five of them and +15 to any posting whose boilerplate merely mentioned
        # running an internship program.
        if any(rx.search(blob) for rx in self.sen_boost):
            score += self.bonus.get("seniority_boost", 0)

        # Same regex as the gate, which also accepts the dotless "ıstanbul" a
        # plain .lower() comparison misses.
        if any(rx.search(loc) for rx in self.loc_city):
            score += self.bonus.get("istanbul", 0)
        if workplace == "remote":
            score += self.bonus.get("remote", 0)
        elif workplace == "hybrid":
            score += self.bonus.get("hybrid", 0)

        posted = j.get("posted_at") or ""
        if posted:
            try:
                # Sources disagree on date format: Lever/boards give
                # "2026-09-01", hiringcafe gives "2026-09-08T08:58:09.491Z".
                # fromisoformat handles both once "Z" is swapped for an offset.
                posted_date = datetime.fromisoformat(posted.replace("Z", "+00:00")).date()
                days = (datetime.now(timezone.utc).date() - posted_date).days
                # 0 <= : a posting dated in the future gives a NEGATIVE day
                # count, which satisfied "days <= 7" and took the top bonus.
                if 0 <= days <= 7:
                    score += self.bonus.get("posted_last_7_days", 0)
                elif 0 <= days <= 30:
                    score += self.bonus.get("posted_last_30_days", 0)
            except ValueError:
                pass

        return True, round(score, 1), ""

    def matched_skills(self, j: dict) -> list[str]:
        """Skill keys whose pattern hits this job - the same skill_rx the
        score uses, so the report chips explain the number. Recomputed at
        report time from the stored title+description; no schema change."""
        n = lambda s: unicodedata.normalize("NFC", s or "")  # noqa: E731
        get = lambda k: (j[k] if k in j.keys() else None) if hasattr(j, "keys") else j.get(k)
        blob = f"{n(get('title'))} {n(get('description'))}"
        return [k for rx, k, _ in self.skill_rx if rx.search(blob)]


# ---------------------------------------------------------------------- store

def upsert_jobs(conn: sqlite3.Connection, jobs: list[dict], run_id: str) -> dict:
    """Insert new jobs as 'new', refresh last_seen on known ones.
    Returns counts: {inserted, updated}."""
    now = run_id
    inserted = updated = 0
    for j in jobs:
        cur = conn.execute("SELECT uid, status FROM jobs WHERE uid = ?", (j["uid"],))
        row = cur.fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO jobs
                   (uid, company, title, url, location, workplace, posted_at, source,
                    description, raw_seniority, score, status, first_seen, last_seen, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (j["uid"], j["company"], j["title"], j["url"], j["location"],
                 j["workplace"], j["posted_at"], j["source"], j["description"],
                 j["raw_seniority"], j["score"], "new", now, now, run_id),
            )
            inserted += 1
        else:
            # A closed row that shows up again is back on the board, so it must
            # come back with it - last_seen/run_id refresh either way, so a row
            # left closed here is never re-closed and never re-opened, just
            # permanently invisible with no uid in the report to `mark` it with.
            # Every other status is the user's own bookkeeping and is preserved.
            conn.execute(
                """UPDATE jobs SET company=?, title=?, url=?, location=?, workplace=?,
                   posted_at=?, source=?, description=?, raw_seniority=?, score=?,
                   last_seen=?, run_id=?,
                   status=CASE WHEN status='closed' THEN 'new' ELSE status END
                   WHERE uid=?""",
                (j["company"], j["title"], j["url"], j["location"], j["workplace"],
                 j["posted_at"], j["source"], j["description"], j["raw_seniority"],
                 j["score"], now, run_id, j["uid"]),
            )
            updated += 1
    conn.commit()
    return {"inserted": inserted, "updated": updated}


def mark_closed(conn: sqlite3.Connection, run_id: str,
                 sources_seen: list[str] | None = None) -> int:
    """Anything not seen in this run is closed (dropped from the report).

    sources_seen limits the sweep to the sources that actually reported. A
    source that raised contributes no postings, which this function otherwise
    cannot tell apart from "every job on that board is gone" - a run where all
    sources failed closed all 10 live listings in testing. Pass None only when
    every source is known to have succeeded.

    'ignored' is excluded alongside 'applied'/'rejected'/'interested': all four
    are the user's own bookkeeping and overwriting them loses a manual decision.
    """
    sql = ("UPDATE jobs SET status='closed' WHERE run_id != ? "
           "AND status NOT IN ('closed','applied','rejected','interested','ignored')")
    params: list = [run_id]
    if sources_seen is not None:
        if not sources_seen:
            return 0
        sql += f" AND source IN ({','.join('?' * len(sources_seen))})"
        params += list(sources_seen)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def active_count(conn: sqlite3.Connection, source: str) -> int:
    """Live (sweepable) rows a source currently owns.

    Used to tell "this board really is empty today" from "this board answered
    200 and we parsed nothing out of it": the second one must not be allowed to
    close a history it only failed to re-read. Counts exactly the statuses
    mark_closed would touch, so the two stay in step.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE source=? AND status NOT IN "
        "('closed','applied','rejected','interested','ignored')", (source,),
    ).fetchone()[0]


def set_status(conn: sqlite3.Connection, uid: str, status: str) -> bool:
    valid = {"new", "interested", "applied", "rejected", "ignored", "closed"}
    if status not in valid:
        raise ValueError(f"status must be one of {valid}, got {status!r}")
    cur = conn.execute("UPDATE jobs SET status=? WHERE uid=?", (status, uid))
    conn.commit()
    return cur.rowcount > 0


def active_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE status NOT IN ('closed','rejected','ignored') "
        "ORDER BY score DESC, posted_at DESC"
    ).fetchall()
