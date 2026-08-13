import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from english_coach import history_store
from english_coach.web_app import _sandbox_banner
from english_coach.web_app import attach_history_routes
from english_coach.web_app import attach_report_routes
from english_coach.web_app import create_app


class EnglishCoachWebAppTest(unittest.TestCase):
    def _client(
        self,
        reports_dir: Path,
        *,
        password: str | None = "secret",
        username: str = "teacher",
    ) -> TestClient:
        app = FastAPI()
        env = {"REPORTS_USERNAME": username}
        if password is not None:
            env["REPORTS_PASSWORD"] = password
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        attach_report_routes(app, reports_dir=reports_dir)
        return TestClient(app)

    def test_reports_requires_basic_auth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(Path(tmpdir))

            response = client.get("/reports")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Basic")

    def test_reports_list_shows_pdf_reports_newest_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir)
            pdf_dir = reports_dir / "pdf"
            pdf_dir.mkdir()
            old_pdf = pdf_dir / "Eve_2026-06-08_09-00-00.pdf"
            new_pdf = pdf_dir / "Suzy_2026-06-08_11-15-07.pdf"
            old_pdf.write_bytes(b"%PDF-1.4 old\n")
            new_pdf.write_bytes(b"%PDF-1.4 new\n")
            os.utime(old_pdf, (1_700_000_000, 1_700_000_000))
            os.utime(new_pdf, (1_800_000_000, 1_800_000_000))
            client = self._client(reports_dir)

            response = client.get("/reports", auth=("teacher", "secret"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("PDF Reports", response.text)
        self.assertLess(response.text.index(new_pdf.name), response.text.index(old_pdf.name))
        self.assertIn(f'href="/reports/{new_pdf.name}"', response.text)
        self.assertIn(f'href="/reports/{new_pdf.name}?download=1"', response.text)

    def test_reports_list_handles_empty_pdf_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(Path(tmpdir))

            response = client.get("/reports", auth=("teacher", "secret"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("No PDF reports yet.", response.text)

    def test_pdf_route_opens_inline_for_printing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir)
            pdf_dir = reports_dir / "pdf"
            pdf_dir.mkdir()
            pdf = pdf_dir / "Suzy_2026-06-08_11-15-07.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            client = self._client(reports_dir)

            response = client.get(f"/reports/{pdf.name}", auth=("teacher", "secret"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.4\n")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_pdf_route_downloads_when_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir)
            pdf_dir = reports_dir / "pdf"
            pdf_dir.mkdir()
            pdf = pdf_dir / "Suzy_2026-06-08_11-15-07.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            client = self._client(reports_dir)

            response = client.get(
                f"/reports/{pdf.name}?download=1",
                auth=("teacher", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_pdf_route_rejects_non_pdf_and_traversal_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir)
            pdf_dir = reports_dir / "pdf"
            pdf_dir.mkdir()
            (reports_dir / "secret.pdf").write_bytes(b"not listed")
            (pdf_dir / "notes.txt").write_text("nope", encoding="utf-8")
            client = self._client(reports_dir)

            non_pdf = client.get("/reports/notes.txt", auth=("teacher", "secret"))
            traversal = client.get(
                "/reports/%2e%2e%2Fsecret.pdf",
                auth=("teacher", "secret"),
            )

        self.assertEqual(non_pdf.status_code, 404)
        self.assertEqual(traversal.status_code, 404)

    def test_legacy_pdf_exports_remain_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir)
            legacy_dir = reports_dir / "pdf_exports"
            legacy_dir.mkdir()
            pdf = legacy_dir / "Suzy_2026-06-08_11-15-07.pdf"
            pdf.write_bytes(b"%PDF-1.4 legacy\n")
            client = self._client(reports_dir)

            listing = client.get("/reports", auth=("teacher", "secret"))
            response = client.get(f"/reports/{pdf.name}", auth=("teacher", "secret"))

        self.assertEqual(listing.status_code, 200)
        self.assertIn(pdf.name, listing.text)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.4 legacy\n")

    def test_reports_are_unavailable_until_password_is_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(Path(tmpdir), password=None)

            response = client.get("/reports", auth=("teacher", "secret"))

        self.assertEqual(response.status_code, 503)
        self.assertIn("REPORTS_PASSWORD", response.text)

    def test_students_page_shows_sandbox_banner_when_data_root_is_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "REPORTS_USERNAME": "teacher",
                "REPORTS_PASSWORD": "secret",
                history_store.DATA_ROOT_ENV: tmpdir,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                history_store.record_student_profile(
                    {
                        "student_name": "Suzy",
                        "feedback_items": [
                            {"filename": "Suzy_2026-06-08.png", "overall_score": 15.0}
                        ],
                        "learning_needs": [],
                    },
                    report_path=Path("r.md"),
                    training_path=Path("t.json"),
                    recorded_at=datetime.datetime(2026, 6, 1, 10, 0).astimezone(),
                )
                self.assertTrue(
                    (Path(tmpdir) / "data" / "learnmate.db").is_file()
                )
                app = FastAPI()
                attach_history_routes(app)
                client = TestClient(app)

                response = client.get("/students", auth=("teacher", "secret"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("Sandbox mode", response.text)
        self.assertIn(history_store.DATA_ROOT_ENV, response.text)
        self.assertIn("Suzy", response.text)

    def test_sandbox_banner_is_absent_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_sandbox_banner(), "")

    def test_create_app_exposes_only_english_coach_adk_app(self):
        with mock.patch.dict(os.environ, {"REPORTS_PASSWORD": "secret"}, clear=True):
            client = TestClient(create_app())

        response = client.get("/list-apps")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), ["english_coach"])

    def test_create_app_uses_learnmate_branding(self):
        with mock.patch.dict(os.environ, {"REPORTS_PASSWORD": "secret"}, clear=True):
            client = TestClient(create_app())

        logo = client.get("/learnmate-logo.svg")
        config = client.get("/dev-ui/assets/config/runtime-config.json")
        dark_theme = client.get("/dev-ui/prism-dark.css")
        light_theme = client.get("/dev-ui/prism-light.css")

        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.headers["content-type"], "image/svg+xml")
        self.assertEqual(dark_theme.status_code, 200)
        self.assertEqual(
            dark_theme.headers["content-type"], "text/css; charset=utf-8"
        )
        self.assertEqual(light_theme.status_code, 200)
        self.assertEqual(
            light_theme.headers["content-type"], "text/css; charset=utf-8"
        )
        self.assertEqual(
            config.json()["logo"],
            {"text": "LearnMate", "imageUrl": "/learnmate-logo.svg"},
        )


if __name__ == "__main__":
    unittest.main()
