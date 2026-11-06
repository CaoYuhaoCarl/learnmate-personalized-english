"""Teacher review acceptance with synthetic work and a fake PDF boundary.

The report renderer and durable store are real. The fake PDF bytes verify which
content reaches the exporter, not typography or PDF validity; browser/render QA
uses the real exporter separately.
"""

import asyncio
import base64
import copy
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from uuid import uuid4

from fastapi.testclient import TestClient

from english_coach import utils, workflow
from english_coach.pdf_export import PdfExportError
from english_coach.review_models import items_from_results, profiles_from_items
from english_coach.schemas import EnglishCoachFeedback, InputProcessingResult, WritingEvidence
from english_coach.web_app import create_app


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+aXioAAAAASUVORK5CYII="
)
AUTH = ("teacher", "test-review-password")
HEADERS = {"X-LearnMate-Request": "1"}


class SyntheticReviewProcessor:
    """A fixed extraction mistake, never an actual model request."""

    def __init__(self, *, partial=False):
        self.calls = 0
        self.partial = partial

    async def __call__(self, job_dir, inputs, on_update):
        self.calls += 1
        item = inputs[0]
        evidence = WritingEvidence(
            student_name="Synthetic learner", submission_date="2026-11-06",
            prompt_summary="Describe a daily routine.", transcription="He go to school.",
            required_points_covered=1, required_points_total=2,
            grammar_errors=["He go to school."],
            grammar_error_fixes=[{
                "error": "He go to school.", "suggested_fix": "He goes to school.",
                "explanation": "Use goes with he.",
            }],
            spelling_errors=[], has_clear_structure=True, has_conclusion=False,
            handwriting_legibility="clear", strengths=["A clear idea."],
            improvements=["Check the verb ending."],
        )
        dimensions = utils.score_from_evidence(evidence)
        result = InputProcessingResult(
            category="writing", filename=item["filename"], source_path=item["path"],
            student_name=evidence.student_name, submission_date=evidence.submission_date,
            writing_evidence=evidence,
            feedback=EnglishCoachFeedback(
                filename=item["filename"], student_name=evidence.student_name,
                prompt_summary=evidence.prompt_summary, transcription=evidence.transcription,
                dimensions=dimensions, overall_score=utils.calculate_overall_score(dimensions),
                strengths=evidence.strengths, improvements=evidence.improvements,
            ),
        )
        items = items_from_results([result], [item["input_id"]])
        return {
            "status": "partial" if self.partial else "succeeded", "completed": len(inputs),
            "profiles": [profile.model_dump(mode="json") for profile in profiles_from_items(items)],
            "review_items": items, "original_results": [result.model_dump(mode="json")],
            "files": [{"filename": entry["filename"], "input_id": entry["input_id"],
                       "status": "succeeded" if index == 0 else "failed",
                       "message": "Synthetic fixture."}
                      for index, entry in enumerate(inputs)],
            "reports": [], "pdf_status": "not_available", "message": "等待教师校对。",
        }


class TeacherReviewApiTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learnmate-review-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "web"
        self.exported_markdown = []

    def export_pdf(self, markdown_path, output_dir):
        markdown = Path(markdown_path).read_text(encoding="utf-8")
        self.exported_markdown.append(markdown)
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{Path(markdown_path).stem}.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic transport only\n" + markdown.encode())
        return path

    @contextmanager
    def client(self, processor=None, *, exporter=None):
        if processor is None:
            processor = SyntheticReviewProcessor()
        with mock.patch.dict("os.environ", {
            "REPORTS_USERNAME": AUTH[0], "REPORTS_PASSWORD": AUTH[1],
        }, clear=True), mock.patch("english_coach.web_app.envs.load_dotenv_for_agent"), \
                mock.patch.object(workflow, "export_report_pdf", side_effect=exporter or self.export_pdf), \
                mock.patch.object(workflow, "open_pdf_in_wps") as open_pdf:
            app = create_app(data_dir=self.root, processor=processor, include_adk=False)
            with TestClient(app) as client:
                yield client
            open_pdf.assert_not_called()

    def create_review(self, client, *, count=1):
        response = client.post(
            "/api/submissions", auth=AUTH, headers=HEADERS,
            data={"request_id": str(uuid4()), "feedback_language": "zh-Hans"},
            files=[("files", (f"worksheet-{index}.png", PNG, "image/png"))
                   for index in range(count)],
        )
        self.assertEqual(response.status_code, 202, response.text)
        identifier = response.json()["id"]

        async def wait_for_processor():
            task = client.app.state.submissions.tasks.get(identifier)
            if task:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)

        client.portal.call(wait_for_processor)
        return identifier, self.get_review(client, identifier)

    def get_review(self, client, identifier):
        response = client.get(f"/api/submissions/{identifier}/review", auth=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, client, identifier, review, items=None):
        return client.put(
            f"/api/submissions/{identifier}/review", auth=AUTH, headers=HEADERS,
            json={"base_revision": review["revision"], "items": items or review["items"]},
        )

    def confirm(self, client, identifier, revision):
        response = client.post(
            f"/api/submissions/{identifier}/review/confirm", auth=AUTH, headers=HEADERS,
            json={"base_revision": revision, "reviewed": True},
        )
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait_for_export(client, identifier)

    def wait_for_export(self, client, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            review = self.get_review(client, identifier)
            if review["pdf_status"] != "pending":
                return review
            time.sleep(0.01)
        self.fail("Synthetic export did not finish")

    def test_new_result_is_draft_with_original_evidence_and_no_formal_pdf(self):
        processor = SyntheticReviewProcessor()
        with self.client(processor) as client:
            identifier, review = self.create_review(client)
            self.assertEqual(review["state"], "draft")
            self.assertIsNone(review["confirmed_revision"])
            self.assertEqual(review["reports"], [])
            self.assertEqual(review["pdf_status"], "not_available")
            self.assertEqual(review["items"][0]["transcription"], "He go to school.")
            self.assertEqual(review["original_items"][0]["transcription"], "He go to school.")
            self.assertNotIn(str(self.root), json.dumps(review))
            record = client.get(f"/api/submissions/{identifier}", auth=AUTH).json()
            self.assertEqual(record["reports"], [])
            self.assertTrue(record["review_available"])
            self.assertEqual(client.get(
                f"/api/submissions/{identifier}/revisions/1", auth=AUTH,
            ).status_code, 404)
        self.assertEqual(self.exported_markdown, [])
        self.assertEqual(processor.calls, 1)

    def test_teacher_correction_updates_dependencies_and_survives_restart(self):
        processor = SyntheticReviewProcessor()
        with self.client(processor) as client:
            identifier, review = self.create_review(client)
            items = copy.deepcopy(review["items"])
            items[0]["transcription"] = "He goes to school."
            items[0]["errors"] = []
            items[0]["teacher_scores"] = {"content": 4, "structure": 4, "language": 5, "handwriting": 4}
            items[0]["improvements"] = ["Teacher checked the original image."]
            saved = self.save(client, identifier, review, items)
            self.assertEqual(saved.status_code, 200, saved.text)
            updated = saved.json()
            self.assertEqual(updated["revision"], review["revision"] + 1)
            self.assertTrue(updated["needs_review"])
            self.assertEqual(updated["original_items"][0]["transcription"], "He go to school.")
            self.assertEqual(updated["items"][0]["errors"], [])
            self.assertEqual(updated["items"][0]["teacher_scores"]["language"], 5)
            confirmed = self.confirm(client, identifier, updated["revision"])
            self.assertEqual(confirmed["pdf_status"], "ready")
            snapshot = client.get(
                f'/api/submissions/{identifier}/revisions/{updated["revision"]}', auth=AUTH,
            ).json()
            profile = snapshot["profiles"][0]
            self.assertEqual(profile["learning_needs"], [])
            self.assertEqual(profile["feedback_items"][0]["overall_score"], 17)
            self.assertEqual(profile["feedback_items"][0]["transcription"], "He goes to school.")
            self.assertEqual(profile["feedback_items"][0]["improvements"], items[0]["improvements"])
            pdf_url = confirmed["reports"][0]["url"]
            pdf_bytes = client.get(pdf_url, auth=AUTH).content
            self.assertIn(b"He goes to school.", pdf_bytes)
            self.assertIn(b"Teacher checked the original image", pdf_bytes)
            self.assertNotIn(b"Use goes with he.", pdf_bytes)
        with self.client(processor) as client:
            restored = self.get_review(client, identifier)
            self.assertEqual(restored["revision"], updated["revision"])
            self.assertEqual(restored["items"][0]["teacher_scores"], items[0]["teacher_scores"])
            self.assertEqual(client.get(pdf_url, auth=AUTH).content, pdf_bytes)
        self.assertEqual(processor.calls, 1)

    def test_stale_save_and_confirm_cannot_overwrite_newer_edit(self):
        with self.client() as client:
            identifier, original = self.create_review(client)
            items = copy.deepcopy(original["items"])
            items[0]["strengths"] = ["Current teacher comment."]
            saved = self.save(client, identifier, original, items)
            self.assertEqual(saved.status_code, 200)
            stale_items = copy.deepcopy(original["items"])
            stale_items[0]["strengths"] = ["Stale comment."]
            self.assertEqual(self.save(client, identifier, original, stale_items).status_code, 409)
            stale_confirm = client.post(
                f"/api/submissions/{identifier}/review/confirm", auth=AUTH, headers=HEADERS,
                json={"base_revision": original["revision"], "reviewed": True},
            )
            self.assertEqual(stale_confirm.status_code, 409)
            current = self.get_review(client, identifier)
            self.assertEqual(current["items"][0]["strengths"], ["Current teacher comment."])
            self.assertIsNone(current["confirmed_revision"])

    def test_unchanged_save_does_not_create_a_spurious_revision(self):
        with self.client() as client:
            identifier, review = self.create_review(client)
            saved = self.save(client, identifier, review)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["revision"], review["revision"])

    def test_confirmation_requires_explicit_review_acknowledgement(self):
        with self.client() as client:
            identifier, review = self.create_review(client)
            for acknowledgement in [None, False]:
                payload = {"base_revision": review["revision"]}
                if acknowledgement is not None:
                    payload["reviewed"] = acknowledgement
                response = client.post(
                    f"/api/submissions/{identifier}/review/confirm", auth=AUTH, headers=HEADERS,
                    json=payload,
                )
                self.assertEqual(response.status_code, 422, response.text)
            self.assertIsNone(self.get_review(client, identifier)["confirmed_revision"])
        self.assertEqual(self.exported_markdown, [])

    def test_confirmation_is_idempotent_and_only_exports_the_confirmed_version(self):
        processor = SyntheticReviewProcessor()
        with self.client(processor) as client:
            identifier, review = self.create_review(client)
            confirmed = self.confirm(client, identifier, review["revision"])
            count = len(self.exported_markdown)
            repeated = self.confirm(client, identifier, review["revision"])
            self.assertEqual(repeated["confirmed_revision"], confirmed["confirmed_revision"])
            self.assertEqual(len(self.exported_markdown), count)
            self.assertEqual(repeated["reports"], confirmed["reports"])
        self.assertEqual(processor.calls, 1)

    def test_export_failure_preserves_confirmation_and_retry_never_reprocesses(self):
        processor = SyntheticReviewProcessor()
        fail_export = True

        def exporter(markdown_path, output_dir):
            if fail_export:
                raise PdfExportError("synthetic-private-export-diagnostic")
            return self.export_pdf(markdown_path, output_dir)

        with self.client(processor, exporter=exporter) as client:
            identifier, review = self.create_review(client)
            failed = self.confirm(client, identifier, review["revision"])
            self.assertEqual(failed["pdf_status"], "failed")
            self.assertEqual(failed["state"], "confirmed")
            self.assertEqual(failed["confirmed_revision"], review["revision"])
            self.assertNotIn("synthetic-private-export-diagnostic", json.dumps(failed))
            fail_export = False
            retry = client.post(
                f"/api/submissions/{identifier}/review/export", auth=AUTH, headers=HEADERS,
                json={"base_revision": review["revision"]},
            )
            self.assertEqual(retry.status_code, 202, retry.text)
            recovered = self.wait_for_export(client, identifier)
            self.assertEqual(recovered["pdf_status"], "ready")
            self.assertTrue(recovered["reports"])
            self.assertEqual(recovered["revision"], review["revision"])
        self.assertEqual(processor.calls, 1)

    def test_partial_retry_keeps_previously_downloadable_reports(self):
        attempt = 1

        def exporter(markdown_path, output_dir):
            is_summary = Path(markdown_path).stem.startswith("session_")
            if (attempt == 1 and is_summary) or (attempt == 2 and not is_summary):
                raise PdfExportError("Synthetic alternating PDF failure")
            return self.export_pdf(markdown_path, output_dir)

        with self.client(exporter=exporter) as client:
            identifier, review = self.create_review(client)
            initial = self.confirm(client, identifier, review["revision"])
            self.assertEqual(initial["pdf_status"], "partial")
            self.assertEqual(len(initial["reports"]), 1)
            initial_url = initial["reports"][0]["url"]
            original_bytes = client.get(initial_url, auth=AUTH).content
            attempt = 2
            response = client.post(
                f"/api/submissions/{identifier}/review/export", auth=AUTH, headers=HEADERS,
                json={"base_revision": review["revision"]},
            )
            self.assertEqual(response.status_code, 202)
            retried = self.wait_for_export(client, identifier)
            self.assertEqual(retried["pdf_status"], "ready")
            self.assertEqual(len(retried["reports"]), 2)
            retained = client.get(initial_url, auth=AUTH)
            self.assertEqual(retained.status_code, 200)
            self.assertEqual(retained.content, original_bytes)
            self.assertTrue(any("session_" in report["filename"] for report in retried["reports"]))

    def test_failed_save_keeps_previous_draft_and_failed_confirmation_can_be_retried(self):
        with self.client() as client:
            identifier, review = self.create_review(client)
            items = copy.deepcopy(review["items"])
            items[0]["strengths"] = ["Teacher edit not yet saved."]
            store = client.app.state.submissions
            with mock.patch.object(store, "_write", side_effect=OSError("Synthetic storage unavailable")):
                with self.assertRaises(OSError):
                    self.save(client, identifier, review, items)
            unchanged = self.get_review(client, identifier)
            self.assertEqual(unchanged["revision"], review["revision"])
            self.assertEqual(unchanged["items"], review["items"])

            with mock.patch.object(store, "_write", side_effect=OSError("Synthetic storage unavailable")):
                with self.assertRaises(OSError):
                    client.post(
                        f"/api/submissions/{identifier}/review/confirm", auth=AUTH, headers=HEADERS,
                        json={"base_revision": review["revision"], "reviewed": True},
                    )
            orphan_path = self.root / identifier / "confirmed" / f'r{review["revision"]}' / "snapshot.json"
            self.assertTrue(orphan_path.is_file())
            orphan_bytes = orphan_path.read_bytes()
            unchanged = self.get_review(client, identifier)
            self.assertEqual(unchanged["state"], "draft")
            self.assertIsNone(unchanged["confirmed_revision"])
            self.assertEqual(self.exported_markdown, [])
            confirmed = self.confirm(client, identifier, review["revision"])
            self.assertEqual(confirmed["pdf_status"], "ready")
            self.assertEqual(orphan_path.read_bytes(), orphan_bytes)

    def test_editing_after_confirmation_retains_old_snapshot_without_stale_current_pdf(self):
        with self.client() as client:
            identifier, review = self.create_review(client)
            confirmed = self.confirm(client, identifier, review["revision"])
            old_pdf_url = confirmed["reports"][0]["url"]
            old_pdf = client.get(old_pdf_url, auth=AUTH).content
            snapshot_url = f'/api/submissions/{identifier}/revisions/{review["revision"]}'
            old_snapshot = client.get(snapshot_url, auth=AUTH).json()
            items = copy.deepcopy(confirmed["items"])
            items[0]["strengths"] = ["Revised after approval."]
            saved = self.save(client, identifier, confirmed, items)
            self.assertEqual(saved.status_code, 200, saved.text)
            draft = saved.json()
            self.assertEqual(draft["state"], "draft")
            self.assertEqual(draft["reports"], [])
            self.assertEqual(draft["confirmed_revision"], review["revision"])
            self.assertEqual(client.get(old_pdf_url, auth=AUTH).content, old_pdf)
            self.assertEqual(client.get(snapshot_url, auth=AUTH).json(), old_snapshot)
            self.assertEqual(client.get(f"/api/submissions/{identifier}", auth=AUTH).json()["reports"], [])
            new_confirmed = self.confirm(client, identifier, draft["revision"])
            self.assertEqual(new_confirmed["confirmed_revision"], draft["revision"])
            self.assertNotEqual(new_confirmed["reports"][0]["url"], old_pdf_url)
            self.assertEqual(client.get(snapshot_url, auth=AUTH).json(), old_snapshot)

    def test_scores_and_identifiers_are_validated_on_the_server(self):
        with self.client() as client:
            identifier, review = self.create_review(client)
            bad_score = copy.deepcopy(review["items"])
            bad_score[0]["teacher_scores"] = {"content": 6, "structure": 4, "language": 4, "handwriting": 4}
            self.assertEqual(self.save(client, identifier, review, bad_score).status_code, 422)
            other_identity = copy.deepcopy(review["items"])
            other_identity[0]["input_id"] = "foreign-input"
            self.assertEqual(self.save(client, identifier, review, other_identity).status_code, 422)
            other_identity = copy.deepcopy(review["items"])
            other_identity[0]["id"] = "foreign-item"
            self.assertEqual(self.save(client, identifier, review, other_identity).status_code, 422)
            oversized = copy.deepcopy(review["items"])
            oversized[0]["transcription"] = "x" * 100001
            self.assertEqual(self.save(client, identifier, review, oversized).status_code, 422)
            self.assertEqual(self.get_review(client, identifier)["revision"], review["revision"])

    def test_invalid_json_nonfinite_scores_and_boolean_versions_do_not_save(self):
        with self.client() as client:
            identifier, review = self.create_review(client)
            path = f"/api/submissions/{identifier}/review"
            headers = {**HEADERS, "Content-Type": "application/json"}
            for raw in [b"{", b"[]", b'null', b'{"base_revision":true,"items":[]}']:
                with self.subTest(body=raw):
                    response = client.put(path, auth=AUTH, headers=headers, content=raw)
                    self.assertEqual(response.status_code, 422, response.text)
            for bad_number in [float("nan"), float("inf"), -float("inf")]:
                items = copy.deepcopy(review["items"])
                items[0]["teacher_scores"] = {"content": 4, "structure": 4, "language": bad_number, "handwriting": 4}
                raw = json.dumps({"base_revision": review["revision"], "items": items})
                response = client.put(path, auth=AUTH, headers=headers, content=raw)
                self.assertEqual(response.status_code, 422, response.text)
            wrong_type = client.put(path, auth=AUTH, headers=HEADERS, content="plain text")
            self.assertEqual(wrong_type.status_code, 415)
            self.assertEqual(self.get_review(client, identifier)["revision"], review["revision"])

    def test_streamed_oversized_json_is_rejected_before_saving(self):
        with self.client() as client:
            identifier, review = self.create_review(client)

            def chunks():
                yield b'{"base_revision":1,"items":['
                for _ in range(33):
                    yield b" " * 65536
                yield b"]}"

            response = client.put(
                f"/api/submissions/{identifier}/review", auth=AUTH,
                headers={**HEADERS, "Content-Type": "application/json"}, content=chunks(),
            )
            self.assertEqual(response.status_code, 413, response.text)
            self.assertEqual(self.get_review(client, identifier)["revision"], review["revision"])

    def test_review_images_snapshots_and_mutations_share_authentication(self):
        with self.client() as client:
            identifier, review = self.create_review(client)
            input_id = review["items"][0]["input_id"]
            image_url = f"/api/submissions/{identifier}/images/{input_id}"
            self.assertEqual(client.get(image_url).status_code, 401)
            image = client.get(image_url, auth=AUTH)
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.content, PNG)
            self.assertEqual(image.headers["cache-control"], "no-store")
            for suffix in ["review", "revisions/1"]:
                self.assertEqual(client.get(f"/api/submissions/{identifier}/{suffix}").status_code, 401)
            for method, suffix, payload in [
                ("PUT", "review", {"base_revision": review["revision"], "items": review["items"]}),
                ("POST", "review/confirm", {"base_revision": review["revision"], "reviewed": True}),
                ("POST", "review/export", {"base_revision": review["revision"]}),
            ]:
                path = f"/api/submissions/{identifier}/{suffix}"
                self.assertEqual(client.request(method, path, json=payload, headers=HEADERS).status_code, 401)
                self.assertEqual(client.request(method, path, json=payload, auth=AUTH).status_code, 403)
                self.assertEqual(client.request(method, path, json=payload, auth=AUTH, headers={
                    **HEADERS, "Origin": "https://unrelated.example",
                }).status_code, 403)
            self.assertIsNone(self.get_review(client, identifier)["confirmed_revision"])

    def test_images_cannot_cross_submission_or_follow_external_symlinks(self):
        with self.client() as client:
            first_id, first = self.create_review(client)
            second_id, second = self.create_review(client)
            first_input = first["items"][0]["input_id"]
            self.assertEqual(client.get(
                f"/api/submissions/{second_id}/images/{first_input}", auth=AUTH,
            ).status_code, 404)
            external = self.root.parent / "outside.png"
            external.write_bytes(b"synthetic-outside-image")
            image_files = list((self.root / first_id / "inputs").glob("*.png"))
            self.assertEqual(len(image_files), 1)
            image_files[0].unlink()
            image_files[0].symlink_to(external)
            self.assertEqual(client.get(
                f"/api/submissions/{first_id}/images/{first_input}", auth=AUTH,
            ).status_code, 404)

    def test_partial_batch_exposes_failed_files_for_teacher_review(self):
        with self.client(SyntheticReviewProcessor(partial=True)) as client:
            identifier, review = self.create_review(client, count=2)
            self.assertEqual(len(review["items"]), 1)
            self.assertEqual(len(review["failed_files"]), 1)
            self.assertEqual(review["failed_files"][0]["filename"], "worksheet-1.png")
            self.assertEqual(client.get(f"/api/submissions/{identifier}", auth=AUTH).json()["status"], "partial")

    def test_legacy_result_is_editable_but_never_claims_missing_evidence_or_prior_approval(self):
        identifier = str(uuid4())
        directory = self.root / identifier
        pdf_directory = directory / "reports" / "pdf_exports"
        pdf_directory.mkdir(parents=True)
        old_pdf = b"%PDF-1.4 synthetic legacy transport\n"
        (pdf_directory / "legacy.pdf").write_bytes(old_pdf)
        record = {
            "id": identifier, "created_at": "2026-11-01T00:00:00+00:00",
            "status": "succeeded", "stage": "finished", "completed": 1, "total": 1,
            "feedback_language": "zh-Hans", "files": [], "pdf_status": "ready",
            "reports": [{"filename": "legacy.pdf", "title": "Synthetic legacy report"}],
            "profiles": [{"student_name": "Legacy synthetic", "feedback_items": [{
                "student_name": "Legacy synthetic", "filename": "legacy.png",
                "prompt_summary": "Synthetic legacy task.", "transcription": "A legacy answer.",
                "overall_score": 15, "dimensions": {"content": 4, "structure": 4, "language": 3, "handwriting": 4},
                "strengths": ["Original legacy comment."], "improvements": [],
            }]}],
        }
        (directory / "status.json").write_text(json.dumps(record), encoding="utf-8")
        processor = SyntheticReviewProcessor()
        with self.client(processor) as client:
            review = self.get_review(client, identifier)
            self.assertTrue(review["legacy"])
            self.assertEqual(review["state"], "draft")
            self.assertIsNone(review["confirmed_revision"])
            self.assertIsNone(review["items"][0]["rubric"])
            self.assertTrue(review["items"][0]["legacy"])
            items = copy.deepcopy(review["items"])
            items[0]["teacher_scores"] = {"content": 4, "structure": 4, "language": 4, "handwriting": 4}
            saved = self.save(client, identifier, review, items)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["items"][0]["overall_score"], 16)
            self.assertEqual((pdf_directory / "legacy.pdf").read_bytes(), old_pdf)
            confirmed = self.confirm(client, identifier, saved.json()["revision"])
            self.assertEqual(confirmed["pdf_status"], "ready")
        self.assertEqual(processor.calls, 0)

    def test_export_completion_preserves_newer_draft(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_export(markdown_path, output_dir):
            entered.set()
            if not release.wait(timeout=5):
                raise PdfExportError("Synthetic test export timed out")
            return self.export_pdf(markdown_path, output_dir)

        with self.client(exporter=slow_export) as client:
            identifier, review = self.create_review(client)
            response = client.post(
                f"/api/submissions/{identifier}/review/confirm", auth=AUTH, headers=HEADERS,
                json={"base_revision": review["revision"], "reviewed": True},
            )
            self.assertEqual(response.status_code, 202)
            self.assertTrue(entered.wait(timeout=3))
            try:
                while_exporting = self.get_review(client, identifier)
                items = copy.deepcopy(while_exporting["items"])
                items[0]["strengths"] = ["Newer teacher draft while old PDF renders."]
                saved = self.save(client, identifier, while_exporting, items)
                self.assertEqual(saved.status_code, 200, saved.text)
                new_revision = saved.json()["revision"]
            finally:
                release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = self.get_review(client, identifier)
                if any(entry["revision"] == review["revision"] and entry["pdf_status"] == "ready"
                       for entry in current["history"]):
                    break
                time.sleep(0.01)
            else:
                self.fail("Old snapshot export did not complete")
            self.assertEqual(current["revision"], new_revision)
            self.assertEqual(current["state"], "draft")
            self.assertEqual(current["items"][0]["strengths"], items[0]["strengths"])
            self.assertEqual(current["reports"], [])
            old = client.get(f'/api/submissions/{identifier}/revisions/{review["revision"]}', auth=AUTH).json()
            self.assertNotEqual(old["items"][0]["strengths"], items[0]["strengths"])

    def test_restart_marks_interrupted_export_retryable_without_reprocessing(self):
        processor = SyntheticReviewProcessor()
        with self.client(processor) as client:
            identifier, review = self.create_review(client)
            confirmed = self.confirm(client, identifier, review["revision"])
            self.assertEqual(confirmed["pdf_status"], "ready")
        # Model the on-disk state left by a process dying during an export.
        path = self.root / identifier / "status.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["review"]["history"][-1]["pdf_status"] = "pending"
        record["review"]["history"][-1]["reports"] = []
        path.write_text(json.dumps(record), encoding="utf-8")
        prior_exports = len(self.exported_markdown)
        with self.client(processor) as client:
            recovered = self.get_review(client, identifier)
            self.assertEqual(recovered["pdf_status"], "failed")
            self.assertEqual(recovered["state"], "confirmed")
            self.assertEqual(recovered["items"], confirmed["items"])
            self.assertEqual(len(self.exported_markdown), prior_exports)
            response = client.post(
                f"/api/submissions/{identifier}/review/export", auth=AUTH, headers=HEADERS,
                json={"base_revision": recovered["revision"]},
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(self.wait_for_export(client, identifier)["pdf_status"], "ready")
        self.assertEqual(processor.calls, 1)

    def test_export_status_write_failure_can_recover_without_server_restart(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def exporter(markdown_path, output_dir):
            entered.set()
            if not release.wait(timeout=5):
                raise PdfExportError("Synthetic test export timed out")
            return self.export_pdf(markdown_path, output_dir)

        processor = SyntheticReviewProcessor()
        with self.client(processor, exporter=exporter) as client:
            identifier, review = self.create_review(client)
            response = client.post(
                f"/api/submissions/{identifier}/review/confirm", auth=AUTH, headers=HEADERS,
                json={"base_revision": review["revision"], "reviewed": True},
            )
            self.assertEqual(response.status_code, 202)
            self.assertTrue(entered.wait(timeout=3))
            store = client.app.state.submissions
            # Return a container so BlockingPortal does not await the task yet.
            task = client.portal.call(lambda: [store.reviews.tasks[(identifier, review["revision"])]])[0]
            original_write = store._write
            failed_once = False

            def fail_final_status_once(record):
                nonlocal failed_once
                version = record.get("review", {}).get("history", [])[-1]
                if not failed_once and version["pdf_status"] == "ready":
                    failed_once = True
                    raise OSError("Synthetic final status write failure")
                original_write(record)

            async def await_export():
                return await asyncio.gather(task, return_exceptions=True)

            with mock.patch.object(store, "_write", side_effect=fail_final_status_once):
                release.set()
                client.portal.call(await_export)
            self.assertTrue(failed_once)
            recovered = self.get_review(client, identifier)
            self.assertNotEqual(recovered["pdf_status"], "pending")
            self.assertEqual(recovered["state"], "confirmed")
            self.assertEqual(recovered["confirmed_revision"], review["revision"])
            retry = client.post(
                f"/api/submissions/{identifier}/review/export", auth=AUTH, headers=HEADERS,
                json={"base_revision": review["revision"]},
            )
            self.assertEqual(retry.status_code, 202, retry.text)
            self.assertEqual(self.wait_for_export(client, identifier)["pdf_status"], "ready")
        self.assertEqual(processor.calls, 1)


if __name__ == "__main__":
    unittest.main()
