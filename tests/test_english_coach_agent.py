import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from english_coach import agent as coach_agent


class EnglishCoachAgentTest(unittest.TestCase):
    def test_extractor_output_schema_excludes_score_fields(self):
        fields = set(coach_agent.extractor.output_schema.model_fields)

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
                    coach_agent._feedback_language_from_input(user_input),
                    expected,
                )

    def test_default_writing_inputs_dir_is_input(self):
        self.assertEqual(coach_agent.WRITING_INPUTS_DIR.name, "input")

    def test_list_writing_inputs_supports_phone_and_web_image_types(self):
        old_inputs_dir = coach_agent.WRITING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            coach_agent.WRITING_INPUTS_DIR = Path(tmpdir)
            try:
                for filename in [
                    "a.png",
                    "b.jpg",
                    "c.jpeg",
                    "d.webp",
                    "e.heic",
                    "f.heif",
                    "ignore.pdf",
                ]:
                    (Path(tmpdir) / filename).write_bytes(b"fake image bytes")

                items = coach_agent.list_writing_inputs(
                    "please use English feedback"
                )
            finally:
                coach_agent.WRITING_INPUTS_DIR = old_inputs_dir

        self.assertEqual(
            [item["filename"] for item in items],
            ["a.png", "b.jpg", "c.jpeg", "d.webp", "e.heic", "f.heif"],
        )
        self.assertEqual(items[0]["feedback_language"], "en")
        self.assertEqual(items[4]["mime"], "image/heic")

    def test_pick_input_route_emits_classifier_category(self):
        routes = list(
            coach_agent.pick_input_route(
                coach_agent.ImageCategory(
                    category="grammar_training",
                    student_name="Suzy",
                    confidence=0.91,
                    reason="Worksheet with corrected grammar answers.",
                )
            )
        )

        self.assertEqual(routes[-1].actions.route, "grammar_training")

    def test_score_from_evidence_is_deterministic(self):
        evidence = coach_agent.WritingEvidence(
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

        first = coach_agent._score_from_evidence(evidence)
        second = coach_agent._score_from_evidence(evidence)

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            coach_agent.DimensionScores(
                content=4,
                structure=4,
                language=4,
                handwriting=4,
            ),
        )

    def test_process_one_input_routes_writing_to_extractor(self):
        async def run_process_one():
            with tempfile.TemporaryDirectory() as tmpdir:
                image_path = Path(tmpdir) / "Suzy_writing.png"
                image_path.write_bytes(b"fake image bytes")

                class FakeContext:
                    attempt_count = 1
                    node_names = []

                    async def run_node(self, node, node_input=None, **kwargs):
                        self.node_names.append(node.name)
                        if node.name == "classify_input_image":
                            return {
                                "category": "writing",
                                "student_name": "Suzy",
                                "confidence": 0.94,
                                "reason": "Handwritten writing response.",
                            }
                        if node.name == "extractor":
                            return {
                                "student_name": "Suzy",
                                "prompt_summary": "Write about helpful AI.",
                                "transcription": "AI help me study.",
                                "required_points_covered": 2,
                                "required_points_total": 3,
                                "grammar_errors": ["AI help me"],
                                "grammar_error_fixes": [
                                    {
                                        "error": "AI help me",
                                        "suggested_fix": "AI helps me",
                                        "explanation": "Use helps with a singular subject.",
                                    }
                                ],
                                "spelling_errors": [],
                                "has_clear_structure": True,
                                "has_conclusion": False,
                                "handwriting_legibility": "clear",
                                "strengths": ["Clear idea."],
                                "improvements": ["Use subject-verb agreement."],
                            }
                        raise AssertionError(f"unexpected node: {node.name}")

                fake_context = FakeContext()
                events = [
                    event
                    async for event in coach_agent.process_one_input._func(
                        fake_context,
                        {
                            "path": str(image_path),
                            "filename": image_path.name,
                            "mime": "image/png",
                            "feedback_language": "en",
                        },
                    )
                ]
                return events[-1].output, fake_context.node_names

        result, node_names = asyncio.run(run_process_one())

        self.assertEqual(node_names, ["classify_input_image", "extractor"])
        self.assertEqual(result.category, "writing")
        self.assertEqual(result.student_name, "Suzy")
        self.assertIsNotNone(result.feedback)
        self.assertEqual(result.feedback.overall_score, 16.5)
        self.assertEqual(
            [need.skill_tag for need in result.learning_needs],
            ["grammar", "writing_improvement"],
        )
        self.assertEqual(result.learning_needs[0].suggested_fix, "AI helps me")
        for need in result.learning_needs:
            self.assertFalse(need.suggested_fix.startswith("Review and correct:"))
        self.assertEqual(
            result.learning_needs[0].explanation,
            "Use helps with a singular subject.",
        )
        self.assertNotEqual(
            result.learning_needs[1].evidence,
            result.learning_needs[1].suggested_fix,
        )

    def test_process_one_input_routes_grammar_training_to_mistake_extractor(self):
        async def run_process_one():
            with tempfile.TemporaryDirectory() as tmpdir:
                image_path = Path(tmpdir) / "Suzy_grammar.png"
                image_path.write_bytes(b"fake image bytes")

                class FakeContext:
                    attempt_count = 1
                    node_names = []

                    async def run_node(self, node, node_input=None, **kwargs):
                        self.node_names.append(node.name)
                        if node.name == "classify_input_image":
                            return {
                                "category": "grammar_training",
                                "student_name": "Suzy",
                                "confidence": 0.96,
                                "reason": "Grammar correction worksheet.",
                            }
                        if node.name == "grammar_training_extractor":
                            return {
                                "student_name": "unknown",
                                "mistakes": [
                                    {
                                        "skill_tag": "subject_verb_agreement",
                                        "original_answer": "He go to school.",
                                        "correct_answer": "He goes to school.",
                                        "explanation": "Use goes with he.",
                                    }
                                ],
                            }
                        raise AssertionError(f"unexpected node: {node.name}")

                fake_context = FakeContext()
                events = [
                    event
                    async for event in coach_agent.process_one_input._func(
                        fake_context,
                        {
                            "path": str(image_path),
                            "filename": image_path.name,
                            "mime": "image/png",
                            "feedback_language": "zh-Hans",
                        },
                    )
                ]
                return events[-1].output, fake_context.node_names

        result, node_names = asyncio.run(run_process_one())

        self.assertEqual(node_names, ["classify_input_image", "grammar_training_extractor"])
        self.assertEqual(result.category, "grammar_training")
        self.assertEqual(result.student_name, "Suzy")
        self.assertIsNone(result.feedback)
        self.assertEqual(len(result.grammar_training.mistakes), 1)
        self.assertEqual(result.learning_needs[0].source_type, "grammar_training")
        self.assertEqual(result.learning_needs[0].suggested_fix, "He goes to school.")

    def test_build_student_profiles_merges_writing_and_grammar_learning_needs(self):
        writing_result = coach_agent.InputProcessingResult(
            filename="Suzy_writing.png",
            category="writing",
            student_name="Suzy",
            feedback_language="en",
            feedback=coach_agent.EnglishCoachFeedback(
                filename="Suzy_writing.png",
                student_name="Suzy",
                feedback_language="en",
                prompt_summary="Write about AI.",
                transcription="AI help me.",
                overall_score=17.0,
                dimensions=coach_agent.DimensionScores(
                    content=4,
                    structure=4,
                    language=5.0,
                    handwriting=4,
                ),
                strengths=["Clear point."],
                improvements=["Use subject-verb agreement."],
            ),
            grammar_training=None,
            learning_needs=[
                coach_agent.LearningNeed(
                    student_name="Suzy",
                    source_type="writing",
                    filename="Suzy_writing.png",
                    skill_tag="writing_improvement",
                    evidence="Use subject-verb agreement.",
                    suggested_fix="Use subject-verb agreement.",
                    explanation="Writing improvement item.",
                )
            ],
        )
        grammar_result = coach_agent.InputProcessingResult(
            filename="grammar.png",
            category="grammar_training",
            student_name="unknown",
            feedback_language="en",
            feedback=None,
            grammar_training=coach_agent.GrammarTrainingEvidence(
                student_name="unknown",
                mistakes=[],
            ),
            learning_needs=[
                coach_agent.LearningNeed(
                    student_name="unknown",
                    source_type="grammar_training",
                    filename="grammar.png",
                    skill_tag="tense",
                    evidence="I go yesterday.",
                    suggested_fix="I went yesterday.",
                    explanation="Use past tense for yesterday.",
                )
            ],
        )

        profiles = coach_agent.build_student_profiles([writing_result, grammar_result])

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].student_name, "Suzy")
        self.assertEqual([need.student_name for need in profiles[0].learning_needs], ["Suzy", "Suzy"])
        self.assertEqual(len(profiles[0].feedback_items), 1)
        self.assertEqual(len(profiles[0].grammar_trainings), 1)

    def test_write_report_writes_markdown_and_training_json(self):
        profile = coach_agent.StudentLearningProfile(
            student_name="Eve",
            feedback_language="zh-Hans",
            feedback_items=[
                coach_agent.EnglishCoachFeedback(
                    filename="Eve_writing.png",
                    student_name="Eve",
                    feedback_language="zh-Hans",
                    prompt_summary="写寒假计划。",
                    transcription="I go travel.",
                    overall_score=16.5,
                    dimensions=coach_agent.DimensionScores(
                        content=5,
                        structure=4,
                        language=3.5,
                        handwriting=4,
                    ),
                    strengths=["内容完整。"],
                    improvements=["注意过去时。"],
                )
            ],
            grammar_trainings=[
                coach_agent.GrammarTrainingEvidence(
                    student_name="Eve",
                    mistakes=[
                        coach_agent.GrammarTrainingMistake(
                            skill_tag="past_tense",
                            original_answer="I go yesterday.",
                            correct_answer="I went yesterday.",
                            explanation="yesterday 要用过去式。",
                        )
                    ],
                )
            ],
            learning_needs=[
                coach_agent.LearningNeed(
                    student_name="Eve",
                    source_type="grammar_training",
                    filename="grammar.png",
                    skill_tag="past_tense",
                    evidence="I go yesterday.",
                    suggested_fix="I went yesterday.",
                    explanation="yesterday 要用过去式。",
                )
            ],
            skipped=[],
        )

        old_reports_dir = coach_agent.REPORTS_DIR
        old_training_dir = coach_agent.TRAINING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            coach_agent.REPORTS_DIR = Path(tmpdir) / "reports"
            coach_agent.TRAINING_INPUTS_DIR = Path(tmpdir) / "training_inputs"
            try:
                events = list(coach_agent.write_report([profile]))
                report = next(coach_agent.REPORTS_DIR.glob("Eve_*.md"))
                payload_path = next(coach_agent.TRAINING_INPUTS_DIR.glob("Eve_*.json"))
                report_text = report.read_text(encoding="utf-8")
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            finally:
                coach_agent.REPORTS_DIR = old_reports_dir
                coach_agent.TRAINING_INPUTS_DIR = old_training_dir

        self.assertEqual(len(events), 1)
        self.assertTrue(report_text.startswith("---\nschema_version: 2\n"))
        self.assertIn('report_type: "student_learning_profile"\n', report_text)
        self.assertIn("## Grammar Training Mistakes", report_text)
        self.assertIn("## Personalized Training Input", report_text)
        self.assertEqual(payload["student_name"], "Eve")
        self.assertEqual(payload["learning_needs"][0]["skill_tag"], "past_tense")


if __name__ == "__main__":
    unittest.main()
