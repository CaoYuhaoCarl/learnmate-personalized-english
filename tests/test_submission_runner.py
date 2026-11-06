"""Integration tests use real ADK execution with only model/PDF boundaries faked."""

import asyncio
import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from english_coach import workflow
from english_coach.pdf_export import PdfExportError
from english_coach.schemas import GrammarTrainingEvidence, ImageCategory, StudentLearningProfile, WritingEvidence
from english_coach.submission_runner import process_submission


class SubmissionRunnerTest(unittest.IsolatedAsyncioTestCase):
    def _inputs(self, job: Path, *names: str) -> list[dict[str, str]]:
        directory = job / "uploads"
        directory.mkdir(parents=True, exist_ok=True)
        inputs = []
        for name in names:
            path = directory / name
            path.write_bytes(b"synthetic-image-bytes")
            inputs.append({
                "path": str(path), "filename": name, "mime": "image/png",
                "feedback_language": "zh-Hans",
            })
        return inputs

    @staticmethod
    async def _extract(ctx, *, schema, filename, **kwargs):
        if schema is ImageCategory:
            return ImageCategory(
                category="unsupported" if filename == "unsupported.png" else "grammar_training",
                student_name="Synthetic Student",
                submission_date="2026-01-02",
                confidence=1,
                reason="Synthetic classification",
            )
        return GrammarTrainingEvidence(
            student_name="Synthetic Student", submission_date="2026-01-02",
            mistakes=[{
                "skill_tag": "past_tense", "original_answer": "I go yesterday.",
                "correct_answer": "I went yesterday.", "explanation": "Use the past tense.",
            }],
        )

    @staticmethod
    def _export(markdown_path, output_dir):
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        path = output / f"{Path(markdown_path).stem}.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic test output\n")
        return path

    async def _run(self, job, inputs, updates=None):
        if updates is None:
            updates = []
        with mock.patch.object(workflow, "_run_extractor", side_effect=self._extract), \
                mock.patch.object(workflow, "export_report_pdf", side_effect=self._export), \
                mock.patch.object(workflow, "open_pdf_in_wps") as open_pdf:
            result = await process_submission(job, inputs, updates.append)
        open_pdf.assert_not_called()
        return result

    async def test_explicit_files_use_real_runner_and_preserve_isolated_draft_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job"
            unrelated = Path(tmp) / "old.png"
            unrelated.write_bytes(b"untouched")
            inputs = self._inputs(job, "first.png", "second.png")
            updates = []
            with mock.patch.object(workflow, "list_writing_inputs", side_effect=AssertionError("Must not scan")):
                result = await self._run(job, inputs, updates)

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["completed"], 2)
            self.assertEqual(result["pdf_status"], "not_available")
            self.assertEqual([item["filename"] for item in result["files"]], ["first.png", "second.png"])
            self.assertEqual(len(result["profiles"][0]["grammar_trainings"]), 2)
            self.assertEqual(result["reports"], [])
            self.assertEqual([item["input_id"] for item in result["review_items"]], ["input-1", "input-2"])
            self.assertEqual(len(result["original_results"]), 2)
            self.assertEqual(result["original_results"][0]["grammar_evidence"]["mistakes"][0]["correct_answer"], "I went yesterday.")
            self.assertFalse((job / "reports").exists())
            self.assertFalse((job / "training_inputs").exists())
            self.assertEqual(unrelated.read_bytes(), b"untouched")
            self.assertTrue((job / "uploads" / "first.png").is_file())
            self.assertTrue((job / "uploads" / "second.png").is_file())
            self.assertNotIn(str(job), json.dumps(result))
            self.assertEqual(updates[0], {"stage": "processing", "completed": 0, "total": 2})
            self.assertTrue(any(item["completed"] == 1 for item in updates))
            self.assertEqual(updates[-1], {"stage": "reviewing", "completed": 2, "total": 2})

    async def test_partial_failure_preserves_success_and_original_file_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            result = await self._run(job, self._inputs(job, "first.png", "unsupported.png"))
            self.assertEqual(result["status"], "partial")
            self.assertEqual([item["status"] for item in result["files"]], ["succeeded", "failed"])
            self.assertEqual(result["files"][1]["filename"], "unsupported.png")
            self.assertEqual(len(result["profiles"][0]["grammar_trainings"]), 1)
            self.assertEqual(result["reports"], [])
            self.assertEqual(result["review_items"][0]["input_id"], "input-1")
            self.assertNotIn("Synthetic classification", json.dumps(result["profiles"]))
            self.assertEqual(result["original_results"][0]["classification"]["reason"], "Synthetic classification")

    async def test_unknown_student_is_not_assigned_to_known_student(self):
        async def extract(ctx, *, filename, **kwargs):
            evidence = await self._extract(ctx, filename=filename, **kwargs)
            if filename == "image.png":
                evidence = evidence.model_copy(update={"student_name": "unknown"})
            return evidence

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            inputs = self._inputs(job, "known.png", "image.png")
            with mock.patch.object(workflow, "_run_extractor", side_effect=extract), \
                    mock.patch.object(workflow, "export_report_pdf", side_effect=self._export):
                result = await process_submission(job, inputs, lambda update: None)
            profiles = {profile["student_name"]: profile for profile in result["profiles"]}
            self.assertEqual(set(profiles), {"Synthetic Student", "unknown"})
            self.assertEqual(len(profiles["Synthetic Student"]["grammar_trainings"]), 1)
            self.assertEqual(len(profiles["unknown"]["grammar_trainings"]), 1)
            self.assertEqual(profiles["unknown"]["grammar_trainings"][0]["student_name"], "unknown")

    async def test_all_unsupported_creates_no_empty_success_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            result = await self._run(job, self._inputs(job, "unsupported.png"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["pdf_status"], "not_available")
            self.assertEqual(result["profiles"], [])
            self.assertEqual(result["reports"], [])
            self.assertFalse((job / "reports").exists())

    async def test_model_failure_does_not_expose_provider_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            inputs = self._inputs(job, "first.png")
            with mock.patch.object(workflow, "_run_extractor", side_effect=RuntimeError("private-provider-diagnostic")), \
                    mock.patch.object(workflow.config, "PROCESS_INPUT_MAX_ATTEMPTS", 1):
                result = await process_submission(job, inputs, lambda update: None)
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("private-provider-diagnostic", json.dumps(result))
            self.assertEqual(result["files"][0]["status"], "failed")

    async def test_pdf_failure_preserves_feedback_and_explicit_pdf_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            inputs = self._inputs(job, "first.png")
            with mock.patch.object(workflow, "_run_extractor", side_effect=self._extract), \
                    mock.patch.object(workflow, "export_report_pdf", side_effect=PdfExportError("private-path-diagnostic")):
                result = await process_submission(job, inputs, lambda update: None)
                manifest = list(workflow.write_report(result["profiles"], reports_dir=job / "reports",
                                                     training_inputs_dir=job / "training_inputs",
                                                     pdf_exports_dir=job / "reports" / "pdf_exports", open_in_wps=False))[0].output
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["pdf_status"], "not_available")
            self.assertEqual(manifest["pdf_failures"], 2)
            self.assertTrue(result["profiles"])
            self.assertEqual(result["reports"], [])
            self.assertNotIn("private-path-diagnostic", json.dumps(result))
            self.assertTrue(list((job / "reports").glob("*.md")))

    async def test_partial_pdf_failure_keeps_available_report(self):
        def export(markdown_path, output_dir):
            if Path(markdown_path).name.startswith("session_"):
                raise PdfExportError("Synthetic session PDF failure")
            return self._export(markdown_path, output_dir)

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            inputs = self._inputs(job, "first.png")
            with mock.patch.object(workflow, "_run_extractor", side_effect=self._extract), \
                    mock.patch.object(workflow, "export_report_pdf", side_effect=export):
                result = await process_submission(job, inputs, lambda update: None)
                manifest = list(workflow.write_report(result["profiles"], reports_dir=job / "reports",
                                                     training_inputs_dir=job / "training_inputs",
                                                     pdf_exports_dir=job / "reports" / "pdf_exports", open_in_wps=False))[0].output
            self.assertEqual(result["pdf_status"], "not_available")
            self.assertEqual(manifest["pdf_failures"], 1)
            self.assertEqual(len(manifest["reports"]), 1)
            self.assertEqual(result["status"], "succeeded")

    async def test_empty_or_outside_input_rejected_before_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job"
            inputs = self._inputs(Path(tmp) / "outside", "first.png")
            with mock.patch("english_coach.submission_runner.Runner") as runner:
                with self.assertRaises(ValueError):
                    await process_submission(job, [], lambda update: None)
                with self.assertRaises(ValueError):
                    await process_submission(job, inputs, lambda update: None)
            runner.assert_not_called()

    async def test_concurrent_submissions_keep_outputs_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            first_inputs = self._inputs(first, "first.png")
            second_inputs = self._inputs(second, "second.png")
            with mock.patch.object(workflow, "_run_extractor", side_effect=self._extract), \
                    mock.patch.object(workflow, "export_report_pdf", side_effect=self._export):
                results = await asyncio.gather(
                    process_submission(first, first_inputs, lambda update: None),
                    process_submission(second, second_inputs, lambda update: None),
                )
            self.assertEqual([result["files"][0]["filename"] for result in results], ["first.png", "second.png"])
            for directory, result in zip((first, second), results):
                self.assertEqual(result["status"], "succeeded")
                self.assertFalse((directory / "reports").exists())
                self.assertTrue((directory / result["original_results"][0]["source_path"]).is_file())

    async def test_writing_keeps_full_original_evidence_and_never_exports_before_confirmation(self):
        async def extract(ctx, *, schema, **kwargs):
            if schema is ImageCategory:
                return ImageCategory(category="writing", student_name="Synthetic Student", confidence=1, reason="Synthetic")
            return WritingEvidence(
                student_name="Synthetic Student", prompt_summary="Describe a day", transcription="He go.",
                required_points_covered=2, required_points_total=3, grammar_errors=["He go."], spelling_errors=[],
                has_clear_structure=True, has_conclusion=False, handwriting_legibility="clear",
                strengths=["Clear topic"], improvements=["Check agreement"],
            )
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            inputs = self._inputs(job, "unsupported.png", "writing.png")
            inputs[0]["input_id"] = "original-one"
            inputs[1]["input_id"] = "original-two"
            async def with_unsupported(ctx, *, filename, schema, **kwargs):
                if filename == "unsupported.png":
                    return ImageCategory(category="unsupported", confidence=1, reason="Synthetic")
                return await extract(ctx, schema=schema, **kwargs)
            with mock.patch.object(workflow, "_run_extractor", side_effect=with_unsupported), \
                    mock.patch.object(workflow, "write_report", side_effect=AssertionError("Must await teacher confirmation")):
                result = await process_submission(job, inputs, lambda update: None)
            item = result["review_items"][0]
            self.assertEqual(item["input_id"], "original-two")
            self.assertEqual(item["rubric"]["required_points_total"], 3)
            self.assertEqual(item["system_scores"]["language"], 4.5)
            self.assertEqual(result["original_results"][0]["writing_evidence"]["grammar_errors"], ["He go."])

    def test_colliding_report_names_and_same_second_runs_do_not_overwrite(self):
        profiles = [StudentLearningProfile(student_name=name) for name in (
            "Synthetic A/B", "Synthetic A?B", "session",
        )]
        frozen_time = datetime.datetime(2026, 1, 2, 3, 4, 5).astimezone()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            reports = directory / "reports"
            training = directory / "training_inputs"
            pdfs = reports / "pdf_exports"
            with mock.patch.object(workflow.datetime, "datetime") as clock, \
                    mock.patch.object(workflow, "export_report_pdf", side_effect=self._export):
                clock.now.return_value = frozen_time
                first = list(workflow.write_report(
                    profiles, reports_dir=reports, training_inputs_dir=training,
                    pdf_exports_dir=pdfs, open_in_wps=False,
                ))[0].output
                original_files = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
                second = list(workflow.write_report(
                    profiles, reports_dir=reports, training_inputs_dir=training,
                    pdf_exports_dir=pdfs, open_in_wps=False,
                ))[0].output

            self.assertEqual(len(list(reports.glob("*.md"))), 8)
            self.assertEqual(len(list(training.glob("*.json"))), 6)
            self.assertEqual(len(list(pdfs.glob("*.pdf"))), 8)
            names = [item["filename"] for result in (first, second) for item in result["reports"]]
            self.assertEqual(len(set(names)), 8)
            self.assertEqual(first["expected_report_count"], 4)
            self.assertEqual([item["report_key"] for item in first["reports"]],
                             [item["report_key"] for item in second["reports"]])
            self.assertEqual({item["report_key"] for item in first["reports"]},
                             {"student-1", "student-2", "student-3", "session"})
            for path, content in original_files.items():
                self.assertEqual(path.read_bytes(), content)
            first_student = json.loads((training / "Synthetic_AB_2026-01-02_03-04-05.json").read_text())
            second_student = json.loads((training / "Synthetic_AB_2026-01-02_03-04-05_2.json").read_text())
            self.assertEqual(first_student["student_name"], "Synthetic A/B")
            self.assertEqual(second_student["student_name"], "Synthetic A?B")
            self.assertIn('report_type: "student_learning_profile"', (reports / "session_2026-01-02_03-04-05.md").read_text())
            self.assertIn('report_type: "session_learning_profiles"', (reports / "session_2026-01-02_03-04-05_2.md").read_text())


if __name__ == "__main__":
    unittest.main()
