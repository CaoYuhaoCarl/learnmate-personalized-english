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

    def test_grade_one_sets_filename_and_calculates_overall_score(self):
        async def run_grade_one():
            with tempfile.TemporaryDirectory() as tmpdir:
                image_path = Path(tmpdir) / "essay.png"
                image_path.write_bytes(b"fake image bytes")

                class FakeContext:
                    attempt_count = 1

                    async def run_node(self, *args, **kwargs):
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

                return [
                    event
                    async for event in essay_agent.grade_one._func(
                        FakeContext(),
                        {
                            "path": str(image_path),
                            "filename": "essay.png",
                            "mime": "image/png",
                        },
                    )
                ]

        events = asyncio.run(run_grade_one())
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
        self.assertEqual(grade.overall_score, 16)


if __name__ == "__main__":
    unittest.main()
