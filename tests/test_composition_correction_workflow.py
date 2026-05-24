import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from google.genai import types

from composition_correction_agent.agent import (
    CorrectionIssue,
    CorrectionReport,
    HumanOcrReview,
    ImageCorrectionInput,
    LocalWorkflowConfig,
    _content_to_json_model,
    _review_response_to_result,
    run_local_correction_workflow,
)


class CompositionCorrectionWorkflowTest(unittest.TestCase):
    def test_content_output_is_restorable_after_resume(self):
        content = types.Content(
            role="model",
            parts=[
                types.Part(
                    text=json.dumps(
                        {
                            "image_path": "samples/handwriting.svg",
                            "raw_ocr_text": "I has a apple.",
                            "human_reviewed_text": "I have an apple.",
                            "corrected_composition": "I have an apple.",
                            "grammar_mistakes": [
                                {
                                    "original": "I has",
                                    "suggestion": "I have",
                                    "explanation": "Use have with I.",
                                }
                            ],
                            "spelling_punctuation_issues": [],
                            "vocabulary_improvements": [],
                            "sentence_level_suggestions": [],
                            "structure_style_feedback": "Clear and brief.",
                            "overall_score": 86,
                            "teacher_style_final_comment": "Good correction work.",
                        }
                    )
                )
            ],
        )

        report = _content_to_json_model(content, CorrectionReport)

        self.assertEqual(report.overall_score, 86)
        self.assertEqual(report.grammar_mistakes[0].suggestion, "I have")

    def test_review_response_can_approve_or_edit_ocr_text(self):
        approved = _review_response_to_result("approve", raw_text="I has a apple.")
        edited = _review_response_to_result(
            {"reviewed_text": "I have an apple.", "reviewer_notes": "Fixed OCR."},
            raw_text="I has a apple.",
        )

        self.assertEqual(
            approved,
            HumanOcrReview(
                approved=True,
                reviewed_text="I has a apple.",
                reviewer_notes="Approved without edits.",
            ),
        )
        self.assertEqual(edited.reviewed_text, "I have an apple.")
        self.assertTrue(edited.approved)

    def test_local_workflow_runs_end_to_end_and_persists_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            sample_image = workspace / "sample_handwriting.svg"
            sample_sidecar = workspace / "sample_handwriting.txt"
            sample_image.write_text("<svg><text>I has a apple. She go school.</text></svg>")
            sample_sidecar.write_text("I has a apple. She go school.")

            config = LocalWorkflowConfig(
                storage_dir=workspace / "records",
                human_review_response={
                    "reviewed_text": "I has a apple. She go to school.",
                    "reviewer_notes": "Restored the missing word before agent review.",
                },
                log_steps=False,
            )

            report = asyncio.run(
                run_local_correction_workflow(
                    ImageCorrectionInput(image_path=str(sample_image)),
                    config=config,
                )
            )

            self.assertEqual(report.image_path, str(sample_image))
            self.assertEqual(report.raw_ocr_text, "I has a apple. She go school.")
            self.assertEqual(
                report.human_reviewed_text,
                "I has a apple. She go to school.",
            )
            self.assertIn("She goes to school", report.corrected_composition)
            self.assertGreaterEqual(report.overall_score, 1)
            self.assertLessEqual(report.overall_score, 100)
            self.assertTrue(report.teacher_style_final_comment)
            self.assertTrue(
                any(
                    isinstance(issue, CorrectionIssue)
                    and issue.suggestion == "She goes"
                    for issue in report.grammar_mistakes
                )
            )

            persisted_files = list((workspace / "records").glob("*.json"))
            self.assertEqual(len(persisted_files), 1)
            payload = json.loads(persisted_files[0].read_text())
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["image_path"], str(sample_image))
            self.assertEqual(payload["ocr_result"]["raw_text"], "I has a apple. She go school.")
            self.assertEqual(
                payload["reviewed_text"],
                "I has a apple. She go to school.",
            )
            self.assertEqual(
                payload["final_report"]["teacher_style_final_comment"],
                report.teacher_style_final_comment,
            )


if __name__ == "__main__":
    unittest.main()
