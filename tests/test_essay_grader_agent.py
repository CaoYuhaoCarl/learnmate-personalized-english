import asyncio
import tempfile
import unittest
from pathlib import Path

from essay_grader import agent as essay_agent


class EssayGraderAgentTest(unittest.TestCase):
    def test_extractor_output_schema_excludes_score_fields(self):
        fields = set(essay_agent.extractor.output_schema.model_fields)

        self.assertNotIn("filename", fields)
        self.assertNotIn("overall_score", fields)
        self.assertNotIn("dimensions", fields)

    def test_feedback_language_from_input_defaults_to_chinese(self):
        examples = {
            "": "zh-Hans",
            "请用中文反馈": "zh-Hans",
            "please use English": "en",
            "日文反馈": "ja",
            "한국어로 피드백": "ko",
        }

        for user_input, expected in examples.items():
            with self.subTest(user_input=user_input):
                self.assertEqual(
                    essay_agent._feedback_language_from_input(user_input),
                    expected,
                )

    def test_list_essays_attaches_feedback_language_from_user_input(self):
        old_essays_dir = essay_agent.ESSAYS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            essay_agent.ESSAYS_DIR = Path(tmpdir)
            try:
                (Path(tmpdir) / "essay.png").write_bytes(b"fake image bytes")

                items = essay_agent.list_essays("please use English feedback")
            finally:
                essay_agent.ESSAYS_DIR = old_essays_dir

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["feedback_language"], "en")

    def test_score_from_evidence_is_deterministic(self):
        evidence = essay_agent.EssayEvidence(
            student_name="Suzy",
            prompt_summary="Write about whether AI helps daily life.",
            transcription="AI helps me study. It help me plan. AI makes me happy.",
            required_points_covered=2,
            required_points_total=3,
            grammar_errors=["It help me"],
            spelling_errors=["witer"],
            has_clear_structure=False,
            has_conclusion=True,
            handwriting_legibility="clear",
            strengths=["Addresses the topic."],
            improvements=["Fix grammar."],
        )

        first = essay_agent._score_from_evidence(evidence)
        second = essay_agent._score_from_evidence(evidence)

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            essay_agent.DimensionScores(
                content=4,
                structure=4,
                language=4,
                handwriting=4,
            ),
        )

    def test_language_score_deducts_half_point_per_error(self):
        evidence = essay_agent.EssayEvidence(
            student_name="Eve",
            prompt_summary="Write about winter holiday plans.",
            transcription="I learn writting. It help me. I like writters.",
            required_points_covered=3,
            required_points_total=3,
            grammar_errors=["It help me"],
            spelling_errors=["writting", "writters"],
            has_clear_structure=True,
            has_conclusion=True,
            handwriting_legibility="clear",
            strengths=["Covers all points."],
            improvements=["Fix language errors."],
        )

        score = essay_agent._score_from_evidence(evidence)

        self.assertEqual(score.language, 3.5)

    def test_overall_score_sums_dimension_scores(self):
        examples = [
            essay_agent.DimensionScores(
                content=5,
                structure=5,
                language=2.5,
                handwriting=4,
            ),
            essay_agent.DimensionScores(
                content=4,
                structure=5,
                language=3.5,
                handwriting=4,
            ),
        ]

        for dimensions in examples:
            with self.subTest(dimensions=dimensions):
                self.assertEqual(
                    essay_agent._calculate_overall_score(dimensions),
                    16.5,
                )

    def test_grade_one_sets_filename_and_calculates_overall_score(self):
        async def run_grade_one():
            with tempfile.TemporaryDirectory() as tmpdir:
                image_path = Path(tmpdir) / "essay.png"
                image_path.write_bytes(b"fake image bytes")

                class FakeContext:
                    attempt_count = 1
                    last_text = ""

                    async def run_node(self, *args, **kwargs):
                        content = kwargs["node_input"]
                        self.last_text = content.parts[0].text
                        return {
                            "filename": "model-invented.png",
                            "student_name": "Suzy",
                            "prompt_summary": "Write about helpful AI.",
                            "transcription": "AI helps me.",
                            "required_points_covered": 2,
                            "required_points_total": 3,
                            "grammar_errors": ["It help me"],
                            "spelling_errors": ["witer"],
                            "has_clear_structure": False,
                            "has_conclusion": True,
                            "handwriting_legibility": "clear",
                            "overall_score": 99,
                            "dimensions": {
                                "content": 0,
                                "structure": 0,
                                "language": 0,
                                "handwriting": 0,
                            },
                            "strengths": ["Clear ideas."],
                            "improvements": ["Add details."],
                        }

                fake_context = FakeContext()
                events = [
                    event
                    async for event in essay_agent.grade_one._func(
                        fake_context,
                        {
                            "path": str(image_path),
                            "filename": "essay.png",
                            "mime": "image/png",
                            "feedback_language": "ja",
                        },
                    )
                ]
                return events, fake_context.last_text

        events, model_prompt = asyncio.run(run_grade_one())
        grade = events[-1].output
        self.assertEqual(grade.filename, "essay.png")
        self.assertEqual(
            grade.dimensions,
            essay_agent.DimensionScores(
                content=4,
                structure=4,
                language=4,
                handwriting=4,
            ),
        )
        self.assertEqual(grade.overall_score, 16.0)
        self.assertEqual(grade.feedback_language, "ja")
        self.assertIn("feedback_language: ja", model_prompt)

    def test_write_report_uses_structured_markdown_sections(self):
        grade = essay_agent.EssayGrade(
            filename="IMG_3872.JPG",
            student_name="Eve",
            prompt_summary="Write about winter holiday plans.",
            transcription="First line\nSecond line with original errors.",
            overall_score=16.5,
            dimensions=essay_agent.DimensionScores(
                content=5,
                structure=5,
                language=2.5,
                handwriting=4,
            ),
            strengths=["Covers all required points."],
            improvements=["Fix spelling errors."],
        )

        old_reports_dir = essay_agent.REPORTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            essay_agent.REPORTS_DIR = Path(tmpdir)
            try:
                events = list(essay_agent.write_report([grade]))
                reports = list(Path(tmpdir).glob("Eve_*.md"))
                self.assertEqual(len(reports), 1)
                text = reports[0].read_text(encoding="utf-8")
            finally:
                essay_agent.REPORTS_DIR = old_reports_dir

        self.assertEqual(len(events), 1)
        self.assertTrue(text.startswith("---\nschema_version: 1\n"))
        self.assertIn('report_type: "essay_grading"\n', text)
        self.assertIn('student: "Eve"\n', text)
        self.assertIn('feedback_language: "zh-Hans"\n', text)
        self.assertIn("essay_count: 1\n---\n\n", text)
        self.assertIn("| Feedback Language | zh-Hans |", text)

        expected_order = [
            "# Essay Grading Report",
            "## Report Info",
            "## Score Summary",
            "## Essay Details",
            "### 1. IMG_3872.JPG",
            "#### Score Breakdown",
            "#### Prompt",
            "#### Strengths",
            "#### Improvements",
            "#### Transcription",
        ]
        positions = [text.index(section) for section in expected_order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("| IMG_3872.JPG | 16.5/20 | 5/5 | 5/5 | 2.5/5 | 4/5 |", text)
        self.assertIn(
            "```text\nFirst line\nSecond line with original errors.\n```",
            text,
        )


if __name__ == "__main__":
    unittest.main()
