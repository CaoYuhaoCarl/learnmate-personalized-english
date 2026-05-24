import json
import tempfile
import unittest
from pathlib import Path

from google.genai import types

from composition_correction_agent.agent import (
    CompositionCorrectionInput,
    CompositionCorrectionReport,
    CorrectionRecordStore,
    LocalWorkflowConfig,
    _input_from_node_input,
    _report_from_output,
    _review_response_to_text,
    ocr_extraction_agent,
    run_sample_workflow,
)


class CompositionCorrectionWorkflowTest(unittest.TestCase):
    def test_ocr_extraction_is_agent_owned_without_tool_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = LocalWorkflowConfig(storage_dir=Path(tmp), log_steps=False)

            message = "OCR extraction should be handled by the agent path, not a tool/provider."
            self.assertEqual(list(ocr_extraction_agent.tools), [], message)
            self.assertTrue(hasattr(config, "ocr_agent"), message)
            self.assertFalse(hasattr(config, "ocr_provider"), message)

    def test_content_text_input_is_converted_to_composition_input(self):
        content = types.Content(
            role="user",
            parts=[types.Part(text="samples/handwritten_english_composition.svg")],
        )

        request = _input_from_node_input(content)

        self.assertEqual(
            request.image_path,
            "samples/handwritten_english_composition.svg",
        )

    def test_content_file_upload_input_is_converted_to_composition_input(self):
        content = types.Content(
            role="user",
            parts=[
                types.Part(
                    file_data=types.FileData(
                        file_uri="samples/handwritten_english_composition.svg",
                        mime_type="image/svg+xml",
                    )
                )
            ],
        )

        request = _input_from_node_input(content)

        self.assertEqual(
            request.image_path,
            "samples/handwritten_english_composition.svg",
        )

    def test_content_inline_image_input_is_converted_to_saved_image_path(self):
        content = types.Content(
            role="user",
            parts=[
                types.Part(
                    inline_data=types.Blob(
                        data=b"fake-image-bytes",
                        mime_type="image/png",
                    )
                )
            ],
        )

        request = _input_from_node_input(content)

        self.assertTrue(request.image_path.endswith(".png"))
        self.assertEqual(
            Path(request.image_path).read_bytes(),
            b"fake-image-bytes",
        )

    def test_review_response_text_allows_approval_or_manual_edit(self):
        self.assertEqual(_review_response_to_text(" APPROVE "), "APPROVE")
        self.assertEqual(
            _review_response_to_text({"result": "Edited OCR text"}),
            "Edited OCR text",
        )

    def test_report_content_output_is_restorable_after_resume(self):
        content = types.Content(
            role="model",
            parts=[
                types.Part(
                    text=json.dumps(
                        {
                            "original_image_path": "samples/handwriting.svg",
                            "raw_ocr_text": "I go to park.",
                            "human_reviewed_text": "I go to park.",
                            "corrected_composition": "I went to the park.",
                            "grammar_mistakes": [],
                            "spelling_punctuation_issues": [],
                            "vocabulary_improvements": [],
                            "sentence_level_suggestions": [],
                            "structure_style_feedback": "Add more details.",
                            "overall_score": 82,
                            "teacher_style_final_comment": "Good effort.",
                        }
                    )
                )
            ],
        )

        report = _report_from_output(content)

        self.assertIsInstance(report, CompositionCorrectionReport)
        self.assertEqual(report.corrected_composition, "I went to the park.")
        self.assertEqual(report.overall_score, 82)

    def test_record_store_persists_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CorrectionRecordStore(Path(tmp))
            report = CompositionCorrectionReport(
                original_image_path="samples/handwriting.svg",
                raw_ocr_text="I go to park.",
                human_reviewed_text="I go to park.",
                corrected_composition="I went to the park.",
                grammar_mistakes=[],
                spelling_punctuation_issues=[],
                vocabulary_improvements=[],
                sentence_level_suggestions=[],
                structure_style_feedback="Add more details.",
                overall_score=82,
                teacher_style_final_comment="Good effort.",
            )

            record = store.save_completed_record(
                image_path="samples/handwriting.svg",
                raw_ocr_text="I go to park.",
                reviewed_text="I go to park.",
                final_report=report,
            )

            loaded = store.load(record.record_id)
            saved_json = json.loads(Path(record.path).read_text(encoding="utf-8"))

            self.assertEqual(loaded.status, "completed")
            self.assertEqual(loaded.image_path, "samples/handwriting.svg")
            self.assertEqual(loaded.ocr_result.raw_text, "I go to park.")
            self.assertEqual(loaded.reviewed_text, "I go to park.")
            self.assertEqual(
                loaded.final_correction_report.corrected_composition,
                "I went to the park.",
            )
            self.assertIn("timestamp", saved_json)
            self.assertEqual(saved_json["status"], "completed")

    def test_sample_workflow_runs_end_to_end_with_stub_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            image_path = tmp_path / "sample_handwriting.svg"
            image_path.write_text(
                "<svg xmlns='http://www.w3.org/2000/svg'><text>I go to park.</text></svg>",
                encoding="utf-8",
            )
            image_path.with_suffix(".txt").write_text(
                "Last weekend I go to park. I see many peoples. It was very fun.",
                encoding="utf-8",
            )

            result = run_sample_workflow(
                CompositionCorrectionInput(image_path=str(image_path)),
                storage_dir=tmp_path / "records",
                human_review_response="APPROVE",
                log_steps=False,
            )

            self.assertEqual(result.report.original_image_path, str(image_path))
            self.assertEqual(
                result.report.raw_ocr_text,
                "Last weekend I go to park. I see many peoples. It was very fun.",
            )
            self.assertEqual(result.report.human_reviewed_text, result.report.raw_ocr_text)
            self.assertIn("went to the park", result.report.corrected_composition)
            self.assertTrue(result.report.grammar_mistakes)
            self.assertTrue(result.report.vocabulary_improvements)
            self.assertTrue(result.report.sentence_level_suggestions)
            self.assertGreaterEqual(result.report.overall_score, 0)
            self.assertEqual(result.record.status, "completed")
            self.assertTrue(Path(result.record.path).exists())


if __name__ == "__main__":
    unittest.main()
