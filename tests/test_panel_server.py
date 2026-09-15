from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

import panel_server
import sources
import tracker


ROOT = Path(__file__).resolve().parents[1]


class PanelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        conn = tracker.connect(str(self.db_path))
        jobs = [
            sources.job("Atlas", "ML Engineer", "https://example.com/atlas", location="Istanbul",
                        workplace="hybrid", source="lever:atlas", description="Python and PyTorch."),
            sources.job("Bora", "Data Scientist", "javascript:alert(1)", location="Remote",
                        workplace="remote", source="workable:bora", description="Python."),
            sources.job("Coda", "AI Researcher", "https://example.com/coda", location="Istanbul",
                        workplace="onsite", source="lever:coda", description="Research."),
        ]
        for score, job in enumerate(jobs, start=10):
            job["score"] = score
        tracker.upsert_jobs(conn, jobs, "2026-09-15T16:06:07Z")
        tracker.set_status(conn, jobs[1]["uid"], "rejected")
        tracker.set_status(conn, jobs[2]["uid"], "closed")
        conn.execute(
            "INSERT INTO runs (run_id, summary) VALUES (?, ?)",
            ("2026-09-15T16:06:07Z", json.dumps({
                "per_source": {"lever:atlas": 1},
                "errors": ["sample: ReadTimeout"],
                "degraded": {"workable:bora": "parsed 0 jobs"},
            })),
        )
        conn.commit()
        conn.close()
        app = panel_server.create_app(
            db_path=self.db_path,
            profile_path=ROOT / "profile.yaml",
            derived_path=Path(self.tmp.name) / "absent-derived.yaml",
            frontend_dist=Path(self.tmp.name) / "dist",
        )
        self.client = TestClient(app)
        self.jobs = jobs

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dashboard_returns_active_and_archive_data_safely(self) -> None:
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["runId"], "2026-09-15T16:06:07Z")
        self.assertEqual(data["counts"], {
            "new": 1, "interested": 0, "applied": 0,
            "rejected": 1, "ignored": 0, "closed": 1,
        })
        self.assertEqual(len(data["jobs"]), 3)
        self.assertIsNone(next(job for job in data["jobs"] if job["company"] == "Bora")["url"])
        self.assertIn("sample: ReadTimeout", data["errors"])
        self.assertIn("workable:bora: parsed 0 jobs", data["errors"])

    def test_status_changes_persist_and_reject_invalid_targets(self) -> None:
        uid = self.jobs[0]["uid"]
        response = self.client.patch(f"/api/jobs/{uid}/status", json={"status": "applied"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"uid": uid, "status": "applied"})
        conn = tracker.connect(str(self.db_path))
        self.assertEqual(conn.execute("SELECT status FROM jobs WHERE uid=?", (uid,)).fetchone()[0], "applied")
        conn.close()
        self.assertEqual(self.client.patch(f"/api/jobs/{uid}/status", json={"status": "closed"}).status_code, 422)
        self.assertEqual(self.client.patch("/api/jobs/missing/status", json={"status": "applied"}).status_code, 404)
        self.assertEqual(
            self.client.patch(f"/api/jobs/{self.jobs[2]['uid']}/status", json={"status": "new"}).status_code,
            409,
        )

    def test_empty_database_has_a_useful_dashboard_shape(self) -> None:
        empty = Path(self.tmp.name) / "empty.db"
        client = TestClient(panel_server.create_app(
            db_path=empty,
            profile_path=ROOT / "profile.yaml",
            derived_path=Path(self.tmp.name) / "missing.yaml",
            frontend_dist=Path(self.tmp.name) / "dist",
        ))
        response = client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["runId"])
        self.assertEqual(response.json()["jobs"], [])

    def test_built_react_panel_is_served_without_shadowing_the_api(self) -> None:
        client = TestClient(panel_server.create_app(
            db_path=self.db_path,
            profile_path=ROOT / "profile.yaml",
            derived_path=Path(self.tmp.name) / "missing.yaml",
            frontend_dist=ROOT / "web" / "dist",
        ))
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('<div id="root">', page.text)
        self.assertEqual(client.get("/api/dashboard").status_code, 200)

    # --- CV upload -> parse -> per-user re-rank -----------------------------

    BACKEND_CV = (
        "Ayberk Karataban - Senior Backend Engineer, 6 years of experience. "
        "Built REST APIs in Java and Spring Boot serving millions of requests a day. "
        "Deployed microservices to AWS (EC2, S3) using Docker and Kubernetes. "
        "Designed PostgreSQL schemas and tuned SQL queries. Backend stack: "
        "Java, Spring, AWS, Docker, PostgreSQL. Owned production payment services."
    )

    def _app(self, derived: Path) -> TestClient:
        return TestClient(panel_server.create_app(
            db_path=self.db_path,
            profile_path=ROOT / "profile.yaml",
            derived_path=derived,
            frontend_dist=Path(self.tmp.name) / "dist",
        ))

    @staticmethod
    def _upload(client: TestClient, text: str, filename: str = "cv.txt"):
        return client.post("/api/cv", files={"file": (filename, text.encode(), "text/plain")})

    def test_cv_upload_writes_derived_activates_and_reranks(self) -> None:
        derived = Path(self.tmp.name) / "derived.yaml"
        client = self._app(derived)
        before = client.get("/api/dashboard").json()
        self.assertFalse(before["cv"]["active"])
        atlas_before = next(j for j in before["jobs"] if j["company"] == "Atlas")["score"]

        response = self._upload(client, self.BACKEND_CV)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(derived.exists())
        self.assertTrue({"java", "spring", "aws"} <= set(response.json()["skills"]))

        # A CV upload may write ONLY the allowlisted keys - it can never smuggle
        # in an exclusion bypass (no topic_never_match / geo / remote / seniority).
        written = yaml.safe_load(derived.read_text())
        self.assertTrue(set(written) <= tracker.DERIVED_KEYS)

        after = client.get("/api/dashboard").json()
        self.assertTrue(after["cv"]["active"])
        self.assertGreater(after["cv"]["skillCount"], 0)
        atlas_after = next(j for j in after["jobs"] if j["company"] == "Atlas")["score"]
        # The backend CV has no python/pytorch, so the ML job loses those weights:
        # proof the pool is re-scored live against the uploaded CV, not the DB score.
        self.assertLess(atlas_after, atlas_before)

    def test_cv_upload_rejects_bad_extension_and_thin_input(self) -> None:
        derived = Path(self.tmp.name) / "absent.yaml"
        client = self._app(derived)
        self.assertEqual(self._upload(client, self.BACKEND_CV, "cv.docx").status_code, 415)
        # long enough but no recognisable skills -> 422, and nothing is written
        self.assertEqual(self._upload(client, "lorem ipsum dolor sit amet " * 20).status_code, 422)
        self.assertFalse(derived.exists())

    def test_cv_upload_is_transactional_on_failure(self) -> None:
        derived = Path(self.tmp.name) / "keep.yaml"
        good = {
            "skill_weights": {"java": 4},
            "topic_must_match": [r"(?<!\w)java(?!\w)"],
            "topic_must_match_weak": [r"(?<!\w)java(?!\w)"],
            "seniority_boost": [],
        }
        derived.write_text(yaml.safe_dump(good))
        original = derived.read_bytes()
        client = self._app(derived)
        self.assertEqual(self._upload(client, "nothing to detect here " * 25).status_code, 422)
        self.assertEqual(derived.read_bytes(), original)  # prior profile untouched


if __name__ == "__main__":
    unittest.main()
