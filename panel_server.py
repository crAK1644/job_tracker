"""Local API and static-file server for the React job-tracking panel."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cv
import report
import tracker


ROOT = Path(__file__).parent
FRONTEND_DIST = ROOT / "web" / "dist"
EDITABLE_STATUSES = {"new", "interested", "applied", "rejected", "ignored"}
ACTIVE_STATUSES = {"new", "interested", "applied"}
ARCHIVE_STATUSES = {"rejected", "ignored", "closed"}
MAX_CV_BYTES = 5 * 1024 * 1024          # localhost, single user; a CV is tiny
ALLOWED_CV_SUFFIXES = {".pdf", ".txt", ".md"}


class StatusUpdate(BaseModel):
    status: Literal["new", "interested", "applied", "rejected", "ignored"]


def safe_external_url(value: str | None) -> str | None:
    """Return only usable http(s) URLs from untrusted job-board data."""
    candidate = (value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return candidate
    return None


def _row_to_job(row, filt: tracker.Filter) -> dict:
    return {
        "uid": row["uid"],
        "company": row["company"],
        "title": row["title"],
        "url": safe_external_url(row["url"]),
        "location": row["location"] or "",
        "workplace": row["workplace"] or "unknown",
        "postedAt": row["posted_at"] or "",
        "source": row["source"] or "",
        "description": row["description"] or "",
        "rawSeniority": row["raw_seniority"] or "",
        # Re-scored live against the current profile (profile.yaml + the user's
        # derived.yaml if a CV was uploaded), NOT the value frozen at fetch time.
        # score_only never gates, so an uploaded CV re-ranks the pool without any
        # job disappearing (match mode = re-rank all).
        "score": filt.score_only(row),
        "status": row["status"],
        "firstSeen": row["first_seen"],
        "lastSeen": row["last_seen"],
        "runId": row["run_id"],
        "matchedSkills": filt.matched_skills(row),
    }


def _latest_run(conn) -> tuple[str | None, dict]:
    row = conn.execute(
        "SELECT run_id, summary FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None, {"per_source": {}, "errors": [], "degraded": {}}
    try:
        summary = json.loads(row["summary"])
    except (TypeError, json.JSONDecodeError):
        summary = {}
    return row["run_id"], summary


def create_app(
    *,
    db_path: str | Path | None = None,
    profile_path: str | Path | None = None,
    derived_path: str | Path | None = None,
    frontend_dist: str | Path | None = None,
) -> FastAPI:
    """Build an app with injectable paths for local use and API tests."""
    app = FastAPI(title="Job Tracker Panel", version="0.1.0")
    frontend = Path(frontend_dist or FRONTEND_DIST)
    derived_file = Path(derived_path) if derived_path is not None else tracker.DERIVED_PATH

    def connection():
        return tracker.connect(str(db_path) if db_path is not None else None)

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        profile = tracker.load_profile(profile_path, derived_file)
        filt = tracker.Filter(profile)
        conn = connection()
        try:
            run_id, summary = _latest_run(conn)
            rows = conn.execute("SELECT * FROM jobs").fetchall()
        finally:
            conn.close()

        # Re-rank in Python by the live per-user score (the SQL score column is
        # the fetch-time value; score_only re-computes against the current CV).
        jobs = [_row_to_job(row, filt) for row in rows]
        jobs.sort(key=lambda j: (j["score"], j["postedAt"]), reverse=True)
        counts = {status: sum(job["status"] == status for job in jobs)
                  for status in (*sorted(EDITABLE_STATUSES), "closed")}
        errors = list(summary.get("errors") or [])
        errors.extend(
            f"{source}: {reason}"
            for source, reason in (summary.get("degraded") or {}).items()
        )
        return {
            "runId": run_id,
            "jobs": jobs,
            "counts": counts,
            "activeStatuses": sorted(ACTIVE_STATUSES),
            "archiveStatuses": sorted(ARCHIVE_STATUSES),
            "errors": errors,
            "sources": summary.get("per_source") or {},
            "cv": {
                "active": derived_file.exists(),
                # When a CV is loaded, profile.skill_weights IS the CV's aliases
                # (derived overrides the allowlist); the UI only shows this when
                # active, so before an upload it is just the base profile count.
                "skillCount": len(profile.get("skill_weights") or {}),
            },
            "links": {
                "linkedin": [
                    {"label": label, "url": url}
                    for label, url in report.linkedin_links(profile)
                ],
                "kariyer": [
                    {"label": label, "url": url}
                    for label, url in report.kariyer_links(profile)
                ],
            },
        }

    @app.patch("/api/jobs/{uid}/status")
    def update_status(uid: str, update: StatusUpdate) -> dict:
        conn = connection()
        try:
            row = conn.execute("SELECT status FROM jobs WHERE uid=?", (uid,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="İlan bulunamadı.")
            if row["status"] == "closed":
                raise HTTPException(
                    status_code=409,
                    detail="Kapanan ilanlar panelden yeniden etkinleştirilemez.",
                )
            tracker.set_status(conn, uid, update.status)
        finally:
            conn.close()
        return {"uid": uid, "status": update.status}

    @app.post("/api/cv")
    async def upload_cv(file: UploadFile = File(...)) -> dict:
        """Parse an uploaded CV into this instance's derived.yaml, then the next
        dashboard load re-ranks the pool by it. The raw CV is never written to
        disk - only the derived skills. Transactional via parse_cv_to_derived:
        any failure leaves the existing derived.yaml untouched."""
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ALLOWED_CV_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail="Sadece .pdf, .txt veya .md dosyaları yüklenebilir.",
            )
        raw = await file.read()   # ponytail: whole file in memory; localhost, capped 5MB
        if len(raw) > MAX_CV_BYTES:
            raise HTTPException(status_code=413, detail="CV dosyası çok büyük (en fazla 5 MB).")
        if not raw:
            raise HTTPException(status_code=400, detail="Boş dosya.")

        fd, tmp = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            try:
                text = cv.read_cv_text(tmp)
            except Exception as e:   # pdftotext missing/failed, extraction error
                raise HTTPException(status_code=400, detail=f"CV okunamadı: {e}")
            try:
                derived, summary = cv.parse_cv_to_derived(text)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=f"CV işlenemedi: {e}")
            cv._atomic_write_yaml(derived_file, derived)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return summary

    if frontend.is_dir() and (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def panel(path: str):
        index = frontend / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=503,
                detail="Panel henüz derlenmedi. web/ içinde `npm install && npm run build` çalıştırın.",
            )
        candidate = (frontend / path).resolve()
        if path and candidate.is_relative_to(frontend.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    return app


def serve(*, host: str = "127.0.0.1", port: int = 8000) -> int:
    """Start the dashboard only after a production frontend build exists."""
    index = FRONTEND_DIST / "index.html"
    if not index.is_file():
        print("Panel build'i bulunamadı. Önce şunları çalıştırın:")
        print("  cd web && npm install && npm run build")
        return 1
    import uvicorn
    print(f"Panel hazır: http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port)
    return 0
