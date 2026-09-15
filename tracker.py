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
CREATE TABLE IF NOT EXISTS employer_coverage (
    company_key TEXT NOT NULL,
    company TEXT NOT NULL,
    careers_url TEXT NOT NULL DEFAULT '',
    collection_method TEXT NOT NULL DEFAULT 'manual',
    collection_status TEXT NOT NULL DEFAULT 'unchecked',
    evidence TEXT NOT NULL DEFAULT '',
    last_checked TEXT,
    jobs_seen INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (company_key, careers_url)
);
CREATE TABLE IF NOT EXISTS job_provenance (
    uid TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (uid, source)
);
"""


_JOB_COLUMNS = {
    "role_family": "TEXT NOT NULL DEFAULT ''",
    "opportunity_type": "TEXT NOT NULL DEFAULT 'job'",
    "eligibility": "TEXT NOT NULL DEFAULT 'confirmed'",
    "eligibility_reason": "TEXT NOT NULL DEFAULT ''",
    "student_compatible": "INTEGER NOT NULL DEFAULT 0",
    "external_id": "TEXT NOT NULL DEFAULT ''",
}


def connect(path=None) -> sqlite3.Connection:
    # path is for the selftest's ":memory:" database - the store and report
    # checks need a real schema they can write to without touching jobs.db.
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # SQLite only supports additive migrations. Existing job history and the
    # user's application statuses therefore survive the catalogue expansion.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, definition in _JOB_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
    conn.commit()
    return conn


def load_profile(profile_path=None, derived_path=None, *, include_derived=True) -> dict:
    """profile.yaml is the complete, hand-set config. A CV-generated
    derived.yaml, if present, overrides ONLY the DERIVED_KEYS allowlist -
    unknown keys in it are ignored, so no CV can touch the exclusions or
    the geo/remote/hard-seniority gates. No derived.yaml -> behaves exactly
    as profile.yaml alone. Paths are injectable for the selftest."""
    profile_path = profile_path or PROFILE_PATH
    derived_path = derived_path or DERIVED_PATH
    p = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if include_derived and Path(derived_path).exists():
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
        self.role_families = {
            name: c(patterns) for name, patterns in (profile.get("role_families") or {}).items()
        }
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

    @staticmethod
    def _text(j: dict) -> tuple[str, str, str, str, str]:
        n = lambda s: unicodedata.normalize("NFC", s or "")  # noqa: E731
        title = n(j.get("title"))
        desc = n(j.get("description"))
        loc = n(j.get("location"))
        workplace = (j.get("workplace") or "").lower()
        return title, desc, loc, workplace, f"{title} {desc}"

    def _role_family(self, title: str, desc: str) -> str:
        """Use the role title as the primary evidence.

        A careers page's marketing copy often says AI, cloud, or data for every
        vacancy. Looking for technical words only in the title prevents an HR
        role on an AI company page becoming a software vacancy.
        """
        for family, patterns in self.role_families.items():
            if any(rx.search(title) for rx in patterns):
                return family
        # Graduate programmes can have generic titles, but must say that the
        # programme itself is technical in its description.
        graduate = re.search(r"graduate|new grad|yeni mezun|genc yetenek|genç yetenek", title, re.I)
        technical = re.search(r"software|yazılım|computer|bilgisayar|data|veri|cloud|bulut|cyber|güvenlik|security|engineering|mühendis", desc, re.I)
        return "graduate_programme" if graduate and technical else ""

    @staticmethod
    def _opportunity_type(title: str, desc: str) -> str:
        text = f"{title} {desc}"
        if re.search(r"\b(intern|internship|stajyer?|staj)\b", text, re.I):
            return "internship"
        if re.search(r"part[ -]?time|yar[ıi] zamanl[ıi]", text, re.I):
            return "part_time"
        if re.search(r"graduate program|graduate programme|new grad|yeni mezun|management trainee|young talent|genc yetenek|genç yetenek", text, re.I):
            return "graduate_programme"
        if re.search(r"research (engineer|assistant|scientist)|araştırma (mühendis|görevlisi)|arastirma (muhendis|gorevlisi)", text, re.I):
            return "research"
        return "job"

    @staticmethod
    def _requires_over_three_years(desc: str) -> bool:
        """Only hard requirements count; preferences and an upper range do not."""
        requirement = re.compile(
            r"(?:minimum(?: of)?|at least|en az|requires?|required|must have|zorunlu|tercihen).{0,45}?"
            r"\b(\d{1,2})\+?(?:\s*[-–]\s*\d{1,2})?\s*(?:years?|yrs?|y[ıi]l)\b", re.I)
        trailing_requirement = re.compile(
            r"\b(\d{1,2})\+?(?:\s*[-–]\s*\d{1,2})?\s*(?:years?|yrs?|y[ıi]l)"
            r".{0,50}?(?:experience|deneyim|tecr[uü]be).{0,35}?(?:required|zorunlu|must|requires?)", re.I)
        for match in requirement.finditer(desc):
            if match.group(0).lower().startswith("tercihen"):
                continue
            if int(match.group(1)) > 3:
                return True
        for match in trailing_requirement.finditer(desc):
            if int(match.group(1)) > 3:
                return True
        return False

    def _location(self, loc: str, workplace: str, blob: str) -> tuple[str, str]:
        city_ok = any(rx.search(loc) for rx in self.loc_city)
        non_istanbul = any(rx.search(loc) for rx in self.loc_reject_city)
        country_ok = any(rx.search(loc) for rx in self.loc_country)
        remote = workplace == "remote" or any(rx.search(loc) for rx in self.loc_remote_hint)
        hard_remote_reject = any(rx.search(blob) or rx.search(loc) for rx in self.remote_elig_reject)

        if remote:
            if hard_remote_reject or (not city_ok and any(rx.search(loc) for rx in self.remote_reject)):
                return "ineligible", "Remote role has an incompatible residency or work-authorisation requirement."
            if city_ok or country_ok:
                return "confirmed", "Remote role explicitly lists Istanbul or Turkey."
            # EMEA/worldwide is useful, but it does not prove Turkish payroll,
            # tax, or work-authorisation eligibility.
            return "review", "Remote eligibility for a resident of Turkey is not explicit."

        if city_ok:
            return "confirmed", "Location explicitly includes Istanbul."
        if non_istanbul:
            return "ineligible", "Onsite or hybrid location is outside Istanbul."
        if country_ok:
            return "review", "Turkey is listed but the onsite or hybrid city is not specified."
        return "review", "The job location is missing or too vague to confirm Istanbul eligibility."

    def classify(self, j: dict) -> dict:
        """Return global collection eligibility and labels for one posting.

        `confirmed` and `review` rows are stored. A CV never enters this method:
        CV data only changes the later ranking score shown in the dashboard.
        """
        title, desc, loc, workplace, blob = self._text(j)
        if any(rx.search(title) for rx in self.topic_never):
            return {"keep": False, "reason": "topic_never_match"}
        if any(rx.search(blob) for rx in self.topic_never_body):
            return {"keep": False, "reason": "topic_never_match_body"}
        family = self._role_family(title, desc)
        if not family:
            return {"keep": False, "reason": "no_topic_match"}
        if any(rx.search(title) for rx in self.sen_reject_title):
            return {"keep": False, "reason": "seniority_title"}
        if self._requires_over_three_years(desc):
            return {"keep": False, "reason": "seniority_body"}
        eligibility, eligibility_reason = self._location(loc, workplace, blob)
        if eligibility == "ineligible":
            return {"keep": False, "reason": "location_reject"}
        opportunity = self._opportunity_type(title, desc)
        student = opportunity in {"internship", "part_time", "graduate_programme", "research"}
        return {
            "keep": True,
            "role_family": family,
            "opportunity_type": opportunity,
            "eligibility": eligibility,
            "eligibility_reason": eligibility_reason,
            "student_compatible": student,
        }

    def evaluate(self, j: dict) -> tuple[bool, float, str]:
        """Compatibility wrapper for fetchers and legacy tests.

        Collection uses :meth:`classify`, which keeps ambiguous postings in the
        review queue. This historical predicate remains deliberately stricter
        for callers that need a binary answer.
        """
        classified = self.classify(j)
        if not classified["keep"]:
            title, desc, loc, workplace, blob = self._text(j)
            remote = workplace == "remote" or any(rx.search(loc) for rx in self.loc_remote_hint)
            city_ok = any(rx.search(loc) for rx in self.loc_city)
            if remote and city_ok and any(rx.search(blob) or rx.search(loc) for rx in self.remote_elig_reject):
                return False, 0, "remote_eligibility_reject"
            if remote and any(rx.search(loc) for rx in self.remote_reject):
                return False, 0, "remote_location_reject"
            if remote and any(rx.search(blob) or rx.search(loc) for rx in self.remote_elig_reject):
                return False, 0, "remote_eligibility_reject"
            if any(rx.search(loc) for rx in self.loc_reject_city):
                return False, 0, "city_reject"
            return False, 0, classified["reason"]
        title, desc, loc, workplace, _blob = self._text(j)
        city_ok = any(rx.search(loc) for rx in self.loc_city)
        country_ok = any(rx.search(loc) for rx in self.loc_country)
        tr_source = (j.get("source") or "") in self.tr_sources
        remote = workplace == "remote" or any(rx.search(loc) for rx in self.loc_remote_hint)
        if remote and not city_ok and not tr_source and not any(rx.search(loc) for rx in self.remote_ok):
            return False, 0, "location_reject"
        if not remote and not city_ok:
            # Legacy callers asked for a confirmed yes/no; vague rows are now
            # retained by classify() as `review`, but remain false here.
            curated = ":" in (j.get("source") or "")
            if not loc and not tr_source and not curated:
                return False, 0, "location_reject"
            if country_ok and not (tr_source or curated):
                return False, 0, "location_reject"
            if loc and not country_ok and not tr_source and not (curated and any(rx.search(loc) for rx in self.loc_no_geo)):
                return False, 0, "location_reject"
        return True, self.score_only(j), ""

    def score_only(self, j) -> float:
        """The score `evaluate` computes, but with NO accept/reject gate. Used to
        re-rank the already-curated stored pool against a per-user CV profile at
        request time (the panel): a job that wouldn't pass this profile's gates
        just scores low rather than vanishing (match mode = re-rank all). Same
        skill_rx + score_bonus math as evaluate, so an uploaded CV re-orders by
        its own skill_weights.
        ponytail: intentionally duplicates evaluate's scoring tail rather than
        refactoring that tested fetch-time hot path; if the two ever drift, pull
        the tail into one helper both call."""
        n = lambda s: unicodedata.normalize("NFC", s or "")  # noqa: E731
        get = lambda k: (j[k] if k in j.keys() else None) if hasattr(j, "keys") else j.get(k)
        blob = f"{n(get('title'))} {n(get('description'))}"
        loc = n(get("location"))
        workplace = (get("workplace") or "").lower()
        score = 0.0
        for rx, _key, weight in self.skill_rx:
            if rx.search(blob):
                score += weight
        if any(rx.search(blob) for rx in self.sen_boost):
            score += self.bonus.get("seniority_boost", 0)
        if any(rx.search(loc) for rx in self.loc_city):
            score += self.bonus.get("istanbul", 0)
        if workplace == "remote":
            score += self.bonus.get("remote", 0)
        elif workplace == "hybrid":
            score += self.bonus.get("hybrid", 0)
        posted = get("posted_at") or ""
        if posted:
            try:
                posted_date = datetime.fromisoformat(posted.replace("Z", "+00:00")).date()
                days = (datetime.now(timezone.utc).date() - posted_date).days
                if 0 <= days <= 7:
                    score += self.bonus.get("posted_last_7_days", 0)
                elif 0 <= days <= 30:
                    score += self.bonus.get("posted_last_30_days", 0)
            except ValueError:
                pass
        return round(score, 1)

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
                   description, raw_seniority, score, status, first_seen, last_seen, run_id,
                    role_family, opportunity_type, eligibility, eligibility_reason, student_compatible, external_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (j["uid"], j["company"], j["title"], j["url"], j["location"],
                 j["workplace"], j["posted_at"], j["source"], j["description"],
                 j["raw_seniority"], j["score"], "new", now, now, run_id,
                 j.get("role_family", ""), j.get("opportunity_type", "job"),
                 j.get("eligibility", "confirmed"), j.get("eligibility_reason", ""),
                 int(bool(j.get("student_compatible", False))), j.get("external_id", "")),
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
                   last_seen=?, run_id=?, role_family=?, opportunity_type=?, eligibility=?,
                   eligibility_reason=?, student_compatible=?, external_id=?,
                   status=CASE WHEN status='closed' THEN 'new' ELSE status END
                   WHERE uid=?""",
                (j["company"], j["title"], j["url"], j["location"], j["workplace"],
                 j["posted_at"], j["source"], j["description"], j["raw_seniority"],
                 j["score"], now, run_id, j.get("role_family", ""),
                 j.get("opportunity_type", "job"), j.get("eligibility", "confirmed"),
                 j.get("eligibility_reason", ""), int(bool(j.get("student_compatible", False))),
                 j.get("external_id", ""), j["uid"]),
            )
            updated += 1
        conn.execute(
            "INSERT OR REPLACE INTO job_provenance (uid, source, source_url) VALUES (?,?,?)",
            (j["uid"], j.get("source") or "", j.get("url") or ""),
        )
    conn.commit()
    return {"inserted": inserted, "updated": updated}


def rekey_legacy_jobs(conn: sqlite3.Connection) -> int:
    """Repair rows created by an early external-ID key experiment.

    Normal board jobs retain the original URL-and-title UID forever. If a
    duplicate exists, copy the freshly collected details onto the historical
    row while retaining an applied/interested/rejected/ignored status.
    """
    import sources  # local import avoids the tracker -> sources import cycle at module load

    changed = 0
    for row in conn.execute("SELECT * FROM jobs").fetchall():
        external_id = row["external_id"] if "external_id" in row.keys() else ""
        desired = sources.make_uid(row["company"], row["title"], row["url"], external_id)
        if row["uid"] == desired:
            continue
        current = conn.execute("SELECT * FROM jobs WHERE uid=?", (desired,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO job_provenance (uid, source, source_url) "
            "SELECT ?, source, source_url FROM job_provenance WHERE uid=?", (desired, row["uid"])
        )
        conn.execute("DELETE FROM job_provenance WHERE uid=?", (row["uid"],))
        if current is None:
            conn.execute("UPDATE jobs SET uid=? WHERE uid=?", (desired, row["uid"]))
        else:
            sticky = {"applied", "interested", "rejected", "ignored"}
            preserved_status = current["status"] if current["status"] in sticky else row["status"]
            conn.execute(
                """UPDATE jobs SET company=?, title=?, url=?, location=?, workplace=?, posted_at=?,
                   source=?, description=?, raw_seniority=?, score=?, last_seen=?, run_id=?,
                   role_family=?, opportunity_type=?, eligibility=?, eligibility_reason=?,
                   student_compatible=?, external_id=?, status=? WHERE uid=?""",
                (row["company"], row["title"], row["url"], row["location"], row["workplace"],
                 row["posted_at"], row["source"], row["description"], row["raw_seniority"],
                 row["score"], row["last_seen"], row["run_id"], row["role_family"],
                 row["opportunity_type"], row["eligibility"], row["eligibility_reason"],
                 row["student_compatible"], external_id, preserved_status, desired),
            )
            conn.execute("DELETE FROM jobs WHERE uid=?", (row["uid"],))
        changed += 1
    conn.commit()
    return changed


def register_employer_coverage(conn: sqlite3.Connection, entries: list[dict]) -> None:
    """Make every catalogue row visible even when it cannot be collected yet."""
    for entry in entries:
        boards = entry.get("boards") or [entry]
        urls = []
        for board in boards:
            url = board.get("careers_url") or entry.get("careers_url") or ""
            urls.append(url)
            method = board.get("collection_method") or entry.get("collection_method") or "manual"
            conn.execute(
                """INSERT INTO employer_coverage
                   (company_key, company, careers_url, collection_method, collection_status, evidence, detail)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(company_key, careers_url) DO UPDATE SET
                     company=excluded.company, collection_method=excluded.collection_method,
                     evidence=excluded.evidence""",
                (entry["id"], entry["name"], url, method,
                 board.get("collection_status") or entry.get("collection_status") or "unchecked",
                 board.get("verification") or entry.get("verification") or "Catalogue entry awaiting a live check.",
                 board.get("detail") or entry.get("detail") or ""),
            )
        # Normalisation may have once generated a fallback /careers URL before
        # a verified board URL was added. Remove only an *unchecked* stale
        # placeholder; completed/failed attempts remain audit history.
        if urls:
            conn.execute(
                f"DELETE FROM employer_coverage WHERE company_key=? AND collection_status='unchecked' "
                f"AND careers_url NOT IN ({','.join('?' * len(urls))})",
                [entry["id"], *urls],
            )
    conn.commit()


def update_employer_coverage(conn: sqlite3.Connection, entry: dict, board: dict, *,
                             status: str, checked_at: str, jobs_seen: int = 0, detail: str = "") -> None:
    url = board.get("careers_url") or entry.get("careers_url") or ""
    conn.execute(
        """UPDATE employer_coverage SET collection_status=?, last_checked=?, jobs_seen=?, detail=?
           WHERE company_key=? AND careers_url=?""",
        (status, checked_at, jobs_seen, detail, entry["id"], url),
    )
    conn.commit()


def employer_coverage(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM employer_coverage ORDER BY company COLLATE NOCASE, careers_url"
    ).fetchall()


def reclassify_jobs(conn: sqlite3.Connection, filt: Filter) -> int:
    """Apply new role/location labels without deleting a user's job history."""
    changed = 0
    for row in conn.execute("SELECT * FROM jobs").fetchall():
        result = filt.classify(dict(row))
        if result.get("keep"):
            values = (result["role_family"], result["opportunity_type"], result["eligibility"],
                      result["eligibility_reason"], int(result["student_compatible"]), row["uid"])
        else:
            values = (row["role_family"], row["opportunity_type"], "ineligible",
                      result["reason"], row["student_compatible"], row["uid"])
        conn.execute(
            """UPDATE jobs SET role_family=?, opportunity_type=?, eligibility=?,
               eligibility_reason=?, student_compatible=? WHERE uid=?""", values)
        changed += 1
    conn.commit()
    return changed


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
