"""Submission HTTP behavior with synthetic files and a deterministic processor.

PDF bytes below test transport only; they do not stand in for export/render QA.
"""

import asyncio
import base64
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from uuid import uuid4

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from english_coach import submissions
from english_coach.web_app import create_app


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+aXioAAAAASUVORK5CYII="
)
PDF = b"%PDF-1.4\ntransport-test-only\n"
AUTH = ("teacher", "test-password")
HEADERS = {"X-LearnMate-Request": "1"}


class SyntheticProcessor:
    def __init__(self, *, status="succeeded", pdf_status="ready", blocked=False):
        self.status = status
        self.pdf_status = pdf_status
        self.blocked = blocked
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, job_dir, inputs, on_update):
        self.calls.append((job_dir, inputs))
        self.started.set()
        if self.blocked:
            await self.release.wait()
        on_update({"stage": "reporting", "completed": len(inputs)})
        successful = len(inputs) if self.status == "succeeded" else max(0, len(inputs) - 1)
        if self.status == "failed":
            successful = 0
        reports = []
        if self.pdf_status in {"ready", "partial"}:
            pdf_dir = job_dir / "reports" / "pdf_exports"
            pdf_dir.mkdir(parents=True)
            (pdf_dir / "synthetic-report.pdf").write_bytes(PDF)
            reports.append({"filename": "synthetic-report.pdf", "title": "合成样本报告"})
        return {
            "status": self.status,
            "completed": len(inputs),
            "profiles": [{"student_name": "Synthetic learner", "feedback_items": []}]
            if successful else [],
            "files": [
                {"filename": item["filename"],
                 "status": "succeeded" if index < successful else "failed",
                 "message": "合成测试结果"}
                for index, item in enumerate(inputs)
            ],
            "reports": reports,
            "pdf_status": self.pdf_status,
            "message": "合成测试已完成。",
        }


class SubmissionApiTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnmate-submission-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "web"

    @contextmanager
    def client(self, processor, *, password="test-password", include_adk=False):
        environment = {"REPORTS_USERNAME": AUTH[0]}
        if password is not None:
            environment["REPORTS_PASSWORD"] = password
        with mock.patch.dict("os.environ", environment, clear=True):
            app = create_app(data_dir=self.root, processor=processor, include_adk=include_adk)
            with TestClient(app) as client:
                yield client

    def submit(self, client, *, identifier=None, images=None, language="zh-Hans", headers=None):
        return client.post(
            "/api/submissions",
            auth=AUTH,
            headers=HEADERS if headers is None else headers,
            data={"request_id": identifier or str(uuid4()), "feedback_language": language},
            files=images if images is not None else [("files", ("worksheet.png", PNG, "image/png"))],
        )

    def finished(self, client, identifier):
        async def wait_for_task():
            task = client.app.state.submissions.tasks.get(identifier)
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)

        client.portal.call(wait_for_task)
        response = client.get(f"/api/submissions/{identifier}", auth=AUTH)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_success_is_queryable_and_report_can_be_viewed_or_downloaded(self):
        processor = SyntheticProcessor()
        with self.client(processor) as client:
            response = self.submit(client)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.headers["cache-control"], "no-store")
            record = self.finished(client, response.json()["id"])
            self.assertEqual(record["status"], "succeeded")
            self.assertEqual(record["completed"], record["total"])
            self.assertEqual(record["pdf_status"], "ready")
            self.assertNotIn("fingerprint", record)
            self.assertNotIn(str(self.root), json.dumps(record))
            report = record["reports"][0]
            inline = client.get(report["url"], auth=AUTH)
            download = client.get(report["download_url"], auth=AUTH)
            self.assertEqual(inline.content, PDF)
            self.assertEqual(inline.headers["content-type"], "application/pdf")
            self.assertIn("inline", inline.headers["content-disposition"])
            self.assertIn("attachment", download.headers["content-disposition"])
            self.assertEqual(inline.headers["cache-control"], "no-store")
            recent = client.get("/api/submissions", auth=AUTH).json()["submissions"]
            self.assertEqual([item["id"] for item in recent], [record["id"]])
            self.assertNotIn("profiles", recent[0])
            self.assertNotIn("fingerprint", recent[0])
        self.assertEqual(len(processor.calls), 1)
        job_dir, inputs = processor.calls[0]
        self.assertEqual(job_dir, self.root / record["id"])
        self.assertEqual(Path(inputs[0]["path"]).read_bytes(), PNG)

    def test_partial_and_failed_results_preserve_their_statuses(self):
        for status, pdf_status, expected_files in [
            ("partial", "partial", ["succeeded", "failed"]),
            ("failed", "not_available", ["failed", "failed"]),
        ]:
            with self.subTest(status=status):
                processor = SyntheticProcessor(status=status, pdf_status=pdf_status)
                with self.client(processor) as client:
                    response = self.submit(client, images=[
                        ("files", ("first.png", PNG, "image/png")),
                        ("files", ("second.png", PNG, "image/png")),
                    ])
                    record = self.finished(client, response.json()["id"])
                self.assertEqual(record["status"], status)
                self.assertEqual(record["pdf_status"], pdf_status)
                self.assertEqual([item["status"] for item in record["files"]], expected_files)

    def test_pdf_failure_does_not_discard_successful_feedback(self):
        with self.client(SyntheticProcessor(pdf_status="failed")) as client:
            response = self.submit(client)
            record = self.finished(client, response.json()["id"])
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["pdf_status"], "failed")
        self.assertTrue(record["profiles"])
        self.assertEqual(record["reports"], [])

    def test_processor_exception_returns_safe_failure(self):
        async def failed_processor(*args):
            raise RuntimeError("synthetic-private-error-detail")

        with self.client(failed_processor) as client:
            response = self.submit(client)
            record = self.finished(client, response.json()["id"])
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("synthetic-private-error-detail", json.dumps(record))
        self.assertTrue(record["message"])

    def test_all_pages_assets_and_apis_require_authentication(self):
        processor = SyntheticProcessor()
        identifier = str(uuid4())
        with self.client(processor) as client:
            for path in ["/", "/teacher-assets/teacher.js", "/reports", "/dev-ui/", "/docs",
                         "/openapi.json", "/api/submissions", f"/api/submissions/{identifier}",
                         f"/api/submissions/{identifier}/reports/example.pdf"]:
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["www-authenticate"], "Basic")
                    self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.post("/api/submissions").status_code, 401)
            self.assertEqual(client.get("/", auth=("teacher", "wrong")).status_code, 401)
        self.assertEqual(processor.calls, [])

    def test_missing_password_fails_closed(self):
        with self.client(SyntheticProcessor(), password=None) as client:
            self.assertEqual(client.get("/", auth=AUTH).status_code, 503)
            self.assertEqual(client.get("/api/submissions", auth=AUTH).status_code, 503)

    def test_adk_websocket_rejects_missing_wrong_and_malformed_credentials(self):
        processor = SyntheticProcessor()
        socket_url = "/run_live?app_name=english_coach&user_id=synthetic&session_id=missing"
        wrong = base64.b64encode(b"teacher:wrong").decode("ascii")
        non_ascii = base64.b64encode(b"teacher:\xff").decode("ascii")
        with self.client(processor, include_adk=True) as client:
            for authorization in [None, f"Basic {wrong}", "Basic invalid-base64", f"Basic {non_ascii}"]:
                with self.subTest(authorization=authorization):
                    headers = {"Origin": "http://testserver"}
                    if authorization:
                        headers["Authorization"] = authorization
                    with self.assertRaises(WebSocketDisconnect) as caught:
                        with client.websocket_connect(socket_url, headers=headers):
                            self.fail("Unauthenticated WebSocket handshake was accepted")
                    self.assertEqual(caught.exception.code, 1008)
        self.assertEqual(processor.calls, [])

    def test_adk_websocket_rejects_cross_origin_even_with_correct_credentials(self):
        socket_url = "/run_live?app_name=english_coach&user_id=synthetic&session_id=missing"
        authorization = "Basic " + base64.b64encode(":".join(AUTH).encode()).decode("ascii")
        with self.client(SyntheticProcessor(), include_adk=True) as client:
            with self.assertRaises(WebSocketDisconnect) as caught:
                with client.websocket_connect(socket_url, headers={
                    "Authorization": authorization, "Origin": "https://unrelated.example",
                }):
                    self.fail("Cross-origin WebSocket handshake was accepted")
            self.assertEqual(caught.exception.code, 1008)

    def test_adk_websocket_authenticated_same_origin_reaches_session_check(self):
        socket_url = "/run_live?app_name=english_coach&user_id=synthetic&session_id=missing"
        authorization = "Basic " + base64.b64encode(":".join(AUTH).encode()).decode("ascii")
        accepted = False
        with mock.patch("google.adk.runners.Runner.run_live") as run_live:
            with self.client(SyntheticProcessor(), include_adk=True) as client:
                with self.assertRaises(WebSocketDisconnect) as caught:
                    with client.websocket_connect(socket_url, headers={
                        "Authorization": authorization, "Origin": "http://testserver",
                    }) as socket:
                        accepted = True
                        socket.receive_json()
                self.assertTrue(accepted)
                self.assertEqual(caught.exception.code, 1002)
                self.assertEqual(caught.exception.reason, "Session not found")
            run_live.assert_not_called()

    def test_invalid_uploads_never_run_processor_or_scan_old_input(self):
        processor = SyntheticProcessor()
        old_input = self.root.parent / "old-input"
        old_input.mkdir()
        original = old_input / "old.png"
        original.write_bytes(PNG)
        with mock.patch("english_coach.workflow.WRITING_INPUTS_DIR", old_input):
            with self.client(processor) as client:
                empty = client.post("/api/submissions", auth=AUTH, headers=HEADERS, files=[
                    ("request_id", (None, str(uuid4()))),
                    ("feedback_language", (None, "zh-Hans")),
                ])
                self.assertEqual(empty.status_code, 422)
                for filename, payload in [("notes.pdf", b"%PDF"), ("fake.png", b"not an image"),
                                          ("empty.png", b""), ("../escape.png", PNG)]:
                    with self.subTest(filename=filename):
                        response = self.submit(client, images=[("files", (filename, payload, "image/png"))])
                        self.assertEqual(response.status_code, 422)
                invalid_id = self.submit(client, identifier="invalid")
                self.assertEqual(invalid_id.status_code, 422)
                invalid_language = self.submit(client, language="invalid")
                self.assertEqual(invalid_language.status_code, 422)
                self.assertEqual(client.get("/api/submissions", auth=AUTH).json(), {"submissions": []})
        self.assertEqual(processor.calls, [])
        self.assertEqual(original.read_bytes(), PNG)
        self.assertEqual(list(old_input.iterdir()), [original])

    def test_file_total_and_count_limits_reject_before_processing(self):
        processor = SyntheticProcessor()
        with self.client(processor) as client:
            with mock.patch.object(submissions, "MAX_FILE_BYTES", len(PNG) - 1):
                self.assertEqual(self.submit(client).status_code, 413)
            with mock.patch.object(submissions, "MAX_TOTAL_BYTES", len(PNG)):
                response = self.submit(client, images=[
                    ("files", ("one.png", PNG, "image/png")),
                    ("files", ("two.png", PNG, "image/png")),
                ])
                self.assertEqual(response.status_code, 413)
            too_many = self.submit(client, images=[
                ("files", (f"sample-{index}.png", PNG, "image/png")) for index in range(11)
            ])
            self.assertEqual(too_many.status_code, 400)
        self.assertEqual(processor.calls, [])

    def test_same_request_id_is_idempotent_and_changed_payload_conflicts(self):
        processor = SyntheticProcessor(blocked=True)
        identifier = str(uuid4())
        with self.client(processor) as client:
            first = self.submit(client, identifier=identifier)
            client.portal.call(asyncio.wait_for, processor.started.wait(), 3)
            repeated = self.submit(client, identifier=identifier)
            changed = self.submit(client, identifier=identifier, language="en")
            self.assertEqual(first.status_code, 202)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.json()["id"], identifier)
            self.assertEqual(changed.status_code, 409)
            client.portal.call(processor.release.set)
            self.finished(client, identifier)
        self.assertEqual(len(processor.calls), 1)

    def test_cross_site_and_missing_custom_header_are_rejected(self):
        processor = SyntheticProcessor()
        with self.client(processor) as client:
            for headers in [{}, {**HEADERS, "Origin": "https://unrelated.example"},
                            {**HEADERS, "Sec-Fetch-Site": "cross-site"}]:
                with self.subTest(headers=headers):
                    self.assertEqual(self.submit(client, headers=headers).status_code, 403)
            accepted = self.submit(client, headers={**HEADERS, "Origin": "http://testserver"})
            self.assertEqual(accepted.status_code, 202)
            self.finished(client, accepted.json()["id"])
        self.assertEqual(len(processor.calls), 1)

    def test_reports_require_manifest_membership_and_reject_path_escape(self):
        with self.client(SyntheticProcessor()) as client:
            response = self.submit(client)
            record = self.finished(client, response.json()["id"])
            identifier = record["id"]
            pdf_dir = self.root / identifier / "reports" / "pdf_exports"
            (pdf_dir / "unlisted.pdf").write_bytes(PDF)
            prefix = f"/api/submissions/{identifier}/reports/"
            self.assertEqual(client.get(prefix + "unlisted.pdf", auth=AUTH).status_code, 404)
            self.assertEqual(client.get(prefix + "%2e%2e%2Fstatus.json", auth=AUTH).status_code, 404)
            external = self.root.parent / "external.pdf"
            external.write_bytes(PDF)
            listed = pdf_dir / "synthetic-report.pdf"
            listed.unlink()
            listed.symlink_to(external)
            self.assertEqual(client.get(record["reports"][0]["url"], auth=AUTH).status_code, 404)

    def test_symlinked_report_directory_cannot_expose_external_files(self):
        with self.client(SyntheticProcessor()) as client:
            response = self.submit(client)
            record = self.finished(client, response.json()["id"])
            pdf_dir = self.root / record["id"] / "reports" / "pdf_exports"
            (pdf_dir / "synthetic-report.pdf").unlink()
            pdf_dir.rmdir()
            external = self.root.parent / "external-reports"
            external.mkdir()
            (external / "synthetic-report.pdf").write_bytes(PDF)
            pdf_dir.symlink_to(external, target_is_directory=True)
            self.assertEqual(client.get(record["reports"][0]["url"], auth=AUTH).status_code, 404)

    def test_completed_results_survive_restart_without_reprocessing(self):
        processor = SyntheticProcessor()
        with self.client(processor) as client:
            response = self.submit(client)
            identifier = response.json()["id"]
            before = self.finished(client, identifier)
        restarted_processor = SyntheticProcessor()
        with self.client(restarted_processor) as client:
            after = client.get(f"/api/submissions/{identifier}", auth=AUTH).json()
            self.assertEqual(after, before)
            repeated = self.submit(client, identifier=identifier)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(client.get(after["reports"][0]["url"], auth=AUTH).content, PDF)
        self.assertEqual(restarted_processor.calls, [])

    def test_report_history_keeps_oldest_submission_beyond_recent_api_limit(self):
        processor = SyntheticProcessor()
        with self.client(processor) as client:
            records = []
            for _ in range(21):
                response = self.submit(client)
                self.assertEqual(response.status_code, 202)
                records.append(self.finished(client, response.json()["id"]))
            oldest = records[0]
            recent = client.get("/api/submissions", auth=AUTH).json()["submissions"]
            self.assertEqual(len(recent), 20)
            self.assertNotIn(oldest["id"], [record["id"] for record in recent])

            # Isolate legacy reports; this scenario exercises the Web submission history.
            with mock.patch("english_coach.web_app._pdf_reports", return_value=[]):
                history = client.get("/reports", auth=AUTH)
            self.assertEqual(history.status_code, 200)
            old_report = oldest["reports"][0]
            self.assertIn(f'href="{old_report["url"]}"', history.text)
            self.assertIn(f'href="{old_report["download_url"]}"', history.text)
            self.assertEqual(client.get(old_report["url"]).status_code, 401)
            self.assertEqual(client.get(old_report["url"], auth=AUTH).content, PDF)
        self.assertEqual(len(processor.calls), 21)

    def test_shutdown_interrupts_running_work_and_restart_does_not_retry(self):
        processor = SyntheticProcessor(blocked=True)
        identifier = str(uuid4())
        with self.client(processor) as client:
            self.assertEqual(self.submit(client, identifier=identifier).status_code, 202)
            client.portal.call(asyncio.wait_for, processor.started.wait(), 3)
            running = client.get(f"/api/submissions/{identifier}", auth=AUTH).json()
            self.assertEqual(running["status"], "running")
        restarted_processor = SyntheticProcessor()
        with self.client(restarted_processor) as client:
            record = client.get(f"/api/submissions/{identifier}", auth=AUTH).json()
            self.assertEqual(record["status"], "interrupted")
            self.assertEqual(self.submit(client, identifier=identifier).status_code, 200)
        self.assertEqual(len(processor.calls), 1)
        self.assertEqual(restarted_processor.calls, [])

    def test_crash_left_running_record_is_marked_interrupted_on_start(self):
        processor = SyntheticProcessor()
        with self.client(processor) as client:
            response = self.submit(client)
            identifier = response.json()["id"]
            self.finished(client, identifier)
        path = self.root / identifier / "status.json"
        record = json.loads(path.read_text())
        record.update(status="running", stage="processing")
        path.write_text(json.dumps(record))
        restarted_processor = SyntheticProcessor()
        with self.client(restarted_processor) as client:
            record = client.get(f"/api/submissions/{identifier}", auth=AUTH).json()
            self.assertEqual(record["status"], "interrupted")
            self.assertTrue(record["reports"])
        self.assertEqual(restarted_processor.calls, [])

    def test_chunked_body_is_limited_without_content_length(self):
        processor = SyntheticProcessor()
        with self.client(processor) as client:
            def chunks():
                yield b"--sample\r\nContent-Disposition: form-data; name=\"request_id\"\r\n\r\n"
                yield str(uuid4()).encode() + b"\r\n"
                yield b"x" * 128

            with mock.patch.object(submissions, "MAX_REQUEST_BYTES", 128):
                response = client.post(
                    "/api/submissions", auth=AUTH,
                    headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=sample"},
                    content=chunks(),
                )
            self.assertNotIn("content-length", response.request.headers)
            self.assertEqual(response.status_code, 413)
        self.assertEqual(processor.calls, [])


if __name__ == "__main__":
    unittest.main()
