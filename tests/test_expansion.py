"""Regression tests for the engineering-catalogue expansion.

These tests use responses kept in memory. They exercise portal parsing and
eligibility rules without reaching live careers sites.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import sources
import tracker


ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return self


class HtmlClient:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        return Response(self.pages[url])


class ExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = tracker.load_profile(derived_path=Path("/not-a-real-derived.yaml"))
        self.filt = tracker.Filter(self.profile)

    def test_json_ld_portal_keeps_full_vacancy_details(self) -> None:
        listing = """<script type="application/ld+json">{
          "@context":"https://schema.org", "@type":"JobPosting",
          "title":"Backend Engineer", "url":"https://careers.example/jobs/42",
          "identifier":{"value":"42"}, "datePosted":"2026-09-16",
          "description":"Build production APIs with Python.",
          "jobLocation":{"address":{"addressLocality":"Istanbul", "addressCountry":"TR"}}
        }</script>"""
        jobs = sources.fetch_html_jobs("https://careers.example/jobs", "Example", "custom:example",
                                       HtmlClient({"https://careers.example/jobs": listing}))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["external_id"], "42")
        self.assertIn("production APIs", jobs[0]["description"])
        self.assertEqual(self.filt.classify(jobs[0])["eligibility"], "confirmed")

    def test_html_portal_follows_detail_pages_and_marks_a_capped_walk_partial(self) -> None:
        listing_url = "https://careers.example/open"
        first = "https://careers.example/jobs/one"
        second = "https://careers.example/jobs/two"
        listing = '<a href="/jobs/one">Software Engineer</a><a href="/jobs/two">QA Engineer</a>'
        pages = {
            listing_url: listing,
            first: "<body>Software Engineer. Istanbul, Turkey. Python.</body>",
            second: "<body>QA Engineer. Istanbul, Turkey. Testing.</body>",
        }
        sources.TRUNCATED.clear()
        jobs = sources.fetch_html_jobs(listing_url, "Example", "custom:example", HtmlClient(pages), detail_limit=1)
        self.assertEqual([job["title"] for job in jobs], ["Software Engineer"])
        self.assertIn("custom:example", sources.TRUNCATED)
        sources.TRUNCATED.clear()

    def test_eligibility_distinguishes_confirmed_review_and_ineligible(self) -> None:
        confirmed = sources.job("A", "Backend Engineer", "https://x/1", location="Istanbul, Turkey")
        review = sources.job("A", "Backend Engineer", "https://x/2", location="Remote - EMEA", workplace="remote")
        blocked = sources.job("A", "Backend Engineer", "https://x/3", location="Remote (US only)", workplace="remote")
        self.assertEqual(self.filt.classify(confirmed)["eligibility"], "confirmed")
        self.assertEqual(self.filt.classify(review)["eligibility"], "review")
        self.assertFalse(self.filt.classify(blocked)["keep"])

    def test_early_career_rules_keep_unspecified_and_reject_hard_seniority(self) -> None:
        unspecified = sources.job("A", "Cloud Engineer", "https://x/1", location="Istanbul")
        range_ok = sources.job("A", "Cloud Engineer", "https://x/2", location="Istanbul",
                               description="0-5 years of experience welcome")
        required = sources.job("A", "Cloud Engineer", "https://x/3", location="Istanbul",
                               description="We require 4-6 years of experience")
        intern = sources.job("A", "Embedded Software Engineer Intern", "https://x/4", location="Istanbul")
        self.assertTrue(self.filt.classify(unspecified)["keep"])
        self.assertTrue(self.filt.classify(range_ok)["keep"])
        self.assertFalse(self.filt.classify(required)["keep"])
        self.assertTrue(self.filt.classify(intern)["student_compatible"])

    def test_migration_preserves_status_and_coverage_is_visible(self) -> None:
        conn = tracker.connect(":memory:")
        job = sources.job("Example", "Backend Engineer", "https://example/jobs/1", location="Istanbul")
        classification = self.filt.classify(job)
        job.update(classification, score=1)
        tracker.upsert_jobs(conn, [job], "r1")
        tracker.set_status(conn, job["uid"], "applied")
        job["description"] = "Updated detail"
        tracker.upsert_jobs(conn, [job], "r2")
        self.assertEqual(conn.execute("SELECT status FROM jobs WHERE uid=?", (job["uid"],)).fetchone()[0], "applied")

        entry = {"id": "example", "name": "Example", "careers_url": "https://example/careers",
                 "collection_method": "html", "collection_status": "unchecked", "verification": "Fixture"}
        tracker.register_employer_coverage(conn, [entry])
        tracker.update_employer_coverage(conn, entry, entry, status="complete", checked_at="r2", jobs_seen=1)
        coverage = tracker.employer_coverage(conn)[0]
        self.assertEqual((coverage["collection_status"], coverage["jobs_seen"]), ("complete", 1))

    def test_collection_profile_ignores_personal_gate_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "derived.yaml"
            derived.write_text("topic_must_match: ['impossible']\ntopic_must_match_weak: ['impossible']\n", encoding="utf-8")
            base = tracker.load_profile(derived_path=derived, include_derived=False)
            self.assertNotEqual(base["topic_must_match"], ["impossible"])


if __name__ == "__main__":
    unittest.main()
