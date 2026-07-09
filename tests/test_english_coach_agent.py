import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from google.genai import types

from english_coach import agent as coach_agent
from english_coach import pdf_export


class EnglishCoachAgentTest(unittest.TestCase):
    def test_extractor_output_schema_excludes_score_fields(self):
        fields = set(coach_agent.extractor.output_schema.model_fields)

        self.assertIn("model_essay", fields)
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

    def test_list_writing_inputs_also_scans_tem_staging_dir(self):
        old_inputs_dir = coach_agent.WRITING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tem = root / "tem"
            tem.mkdir()
            (root / "top.jpg").write_bytes(b"fake image bytes")
            (tem / "staged.png").write_bytes(b"fake image bytes")
            (tem / "ignore.txt").write_text("nope", encoding="utf-8")

            coach_agent.WRITING_INPUTS_DIR = root
            try:
                items = coach_agent.list_writing_inputs("")
            finally:
                coach_agent.WRITING_INPUTS_DIR = old_inputs_dir

        self.assertEqual(
            [Path(item["path"]).name for item in items],
            ["top.jpg", "staged.png"],
        )
        self.assertEqual([item["filename"] for item in items], ["top.jpg", "staged.png"])

    def test_list_writing_inputs_prefers_adk_web_uploaded_images(self):
        old_inputs_dir = coach_agent.WRITING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "old.png").write_bytes(b"old image bytes")
            coach_agent.WRITING_INPUTS_DIR = root
            try:
                message = types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text="please use English feedback"),
                        types.Part(
                            inline_data=types.Blob(
                                data=b"uploaded image bytes",
                                display_name="../Suzy homework.png",
                                mime_type="image/png",
                            )
                        ),
                    ],
                )

                items = coach_agent.list_writing_inputs(message)
                uploaded_bytes = Path(items[0]["path"]).read_bytes()
            finally:
                coach_agent.WRITING_INPUTS_DIR = old_inputs_dir

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["filename"], "Suzy_homework.png")
        self.assertEqual(items[0]["mime"], "image/png")
        self.assertEqual(items[0]["feedback_language"], "en")
        self.assertEqual(uploaded_bytes, b"uploaded image bytes")
        self.assertEqual(Path(items[0]["path"]).parent.name, "uploads")

    def test_list_writing_inputs_keeps_multiple_uploaded_images(self):
        old_inputs_dir = coach_agent.WRITING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            coach_agent.WRITING_INPUTS_DIR = Path(tmpdir)
            try:
                message = types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text="请用中文反馈"),
                        types.Part(
                            inline_data=types.Blob(
                                data=b"first image",
                                display_name="essay.png",
                                mime_type="image/png",
                            )
                        ),
                        types.Part(
                            inline_data=types.Blob(
                                data=b"second image",
                                display_name="essay.png",
                                mime_type="image/png",
                            )
                        ),
                    ],
                )

                items = coach_agent.list_writing_inputs(message)
                uploaded_bytes = [
                    Path(item["path"]).read_bytes() for item in items
                ]
            finally:
                coach_agent.WRITING_INPUTS_DIR = old_inputs_dir

        self.assertEqual(
            [item["filename"] for item in items],
            ["essay.png", "essay_2.png"],
        )
        self.assertEqual(
            [item["feedback_language"] for item in items],
            ["zh-Hans", "zh-Hans"],
        )
        self.assertEqual(uploaded_bytes, [b"first image", b"second image"])

    def test_list_writing_inputs_ignores_non_image_uploads(self):
        old_inputs_dir = coach_agent.WRITING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "top.jpg").write_bytes(b"fake image bytes")
            coach_agent.WRITING_INPUTS_DIR = root
            try:
                message = types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text="please use English feedback"),
                        types.Part(
                            inline_data=types.Blob(
                                data=b"not an image",
                                display_name="notes.pdf",
                                mime_type="application/pdf",
                            )
                        ),
                    ],
                )

                items = coach_agent.list_writing_inputs(message)
            finally:
                coach_agent.WRITING_INPUTS_DIR = old_inputs_dir

        self.assertEqual([item["filename"] for item in items], ["top.jpg"])
        self.assertEqual(items[0]["feedback_language"], "en")

    def test_submission_date_normalization_and_priority(self):
        self.assertEqual(
            coach_agent._normalize_submission_date("Date: 2026.6.6"),
            "2026-06-06",
        )
        self.assertEqual(
            coach_agent._normalize_submission_date("2026/06/07"),
            "2026-06-07",
        )
        self.assertEqual(coach_agent._normalize_submission_date("2026-13-07"), "")
        with mock.patch.object(
            coach_agent,
            "_default_submission_date",
            return_value="2026-06-08",
        ):
            self.assertEqual(
                coach_agent._resolve_submission_date(
                    evidence_date="2026.6.6",
                    category_date="2026/6/5",
                ),
                "2026-06-06",
            )
            self.assertEqual(
                coach_agent._resolve_submission_date(
                    evidence_date="",
                    category_date="2026/6/5",
                ),
                "2026-06-05",
            )
            self.assertEqual(
                coach_agent._resolve_submission_date(
                    evidence_date="missing",
                    category_date="missing",
                ),
                "2026-06-08",
            )

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

    def test_writing_learning_needs_do_not_reuse_feedback_improvements(self):
        evidence = coach_agent.WritingEvidence(
            student_name="Suzy",
            prompt_summary="Write about helpful AI.",
            transcription="AI help me study. I like it.",
            required_points_covered=1,
            required_points_total=3,
            grammar_errors=["AI help me"],
            grammar_error_fixes=[
                coach_agent.WritingIssueFix(
                    error="AI help me",
                    suggested_fix="AI helps me",
                    explanation="Use helps with a singular subject.",
                )
            ],
            spelling_errors=[],
            has_clear_structure=True,
            has_conclusion=False,
            handwriting_legibility="clear",
            strengths=["Clear main idea."],
            improvements=["Use subject-verb agreement."],
            writing_training_focuses=[
                coach_agent.WritingTrainingFocus(
                    skill_tag="content_development",
                    evidence="The response gives only one brief idea.",
                    suggested_fix="Add one reason and one concrete example.",
                    explanation="Detailed support makes the response stronger.",
                )
            ],
        )

        needs = coach_agent._writing_learning_needs(
            filename="Suzy_writing.png",
            student_name="Suzy",
            evidence=evidence,
        )

        self.assertEqual(
            [need.skill_tag for need in needs],
            ["grammar", "content_development"],
        )
        self.assertNotIn(
            "Use subject-verb agreement.",
            [need.suggested_fix for need in needs],
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
                                "submission_date": "2026/6/5",
                                "confidence": 0.94,
                                "reason": "Handwritten writing response.",
                            }
                        if node.name == "extractor":
                            return {
                                "student_name": "Suzy",
                                "submission_date": "2026.6.6",
                                "prompt_summary": "Write about helpful AI.",
                                "transcription": "AI help me study.",
                                "model_essay": (
                                    "AI helps me study when I have difficult"
                                    " homework."
                                ),
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
                                "writing_training_focuses": [
                                    {
                                        "skill_tag": "content_development",
                                        "evidence": (
                                            "Only one brief idea is developed."
                                        ),
                                        "suggested_fix": (
                                            "Add one reason and one example to"
                                            " support the main idea."
                                        ),
                                        "explanation": (
                                            "More support makes the paragraph"
                                            " more complete."
                                        ),
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
                            "feedback_language": "en",
                        },
                    )
                ]
                return events[-1].output, fake_context.node_names

        result, node_names = asyncio.run(run_process_one())

        self.assertEqual(node_names, ["classify_input_image", "extractor"])
        self.assertEqual(result.category, "writing")
        self.assertEqual(result.student_name, "Suzy")
        self.assertEqual(result.submission_date, "2026-06-06")
        self.assertTrue(result.source_path.endswith("Suzy_writing.png"))
        self.assertIsNotNone(result.feedback)
        self.assertEqual(
            result.feedback.model_essay,
            "AI helps me study when I have difficult homework.",
        )
        self.assertEqual(result.feedback.overall_score, 16.5)
        self.assertEqual(
            [need.skill_tag for need in result.learning_needs],
            ["grammar", "content_development"],
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
                                "submission_date": "2026/6/6",
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
        self.assertEqual(result.submission_date, "2026-06-06")
        self.assertIsNone(result.feedback)
        self.assertEqual(len(result.grammar_training.mistakes), 1)
        self.assertEqual(result.grammar_training.submission_date, "2026-06-06")
        self.assertEqual(result.learning_needs[0].source_type, "grammar_training")
        self.assertEqual(result.learning_needs[0].suggested_fix, "He goes to school.")

    def test_task_for_attempt_adds_retry_nudge_only_after_first_attempt(self):
        base = "Classify this image before any grading or extraction."

        self.assertEqual(coach_agent._task_for_attempt(base, 1), base)
        nudged = coach_agent._task_for_attempt(base, 2)
        self.assertIn(base, nudged)
        self.assertIn("Retry attempt 2", nudged)

    def test_process_one_input_reraises_subnode_failure_for_node_retry(self):
        async def run_process_one():
            with tempfile.TemporaryDirectory() as tmpdir:
                image_path = Path(tmpdir) / "Suzy_writing.png"
                image_path.write_bytes(b"fake image bytes")

                class FakeContext:
                    attempt_count = 1

                    async def run_node(self, node, node_input=None, **kwargs):
                        raise ValueError(
                            "Invalid JSON: EOF while parsing an object"
                        )

                async for _ in coach_agent.process_one_input._func(
                    FakeContext(),
                    {
                        "path": str(image_path),
                        "filename": image_path.name,
                        "mime": "image/png",
                        "feedback_language": "en",
                    },
                ):
                    pass

        with self.assertRaises(coach_agent.InputProcessingError):
            asyncio.run(run_process_one())

    def test_process_one_input_skips_input_after_final_attempt(self):
        async def run_process_one():
            with tempfile.TemporaryDirectory() as tmpdir:
                image_path = Path(tmpdir) / "Suzy_writing.png"
                image_path.write_bytes(b"fake image bytes")

                class FakeContext:
                    attempt_count = coach_agent._PROCESS_INPUT_MAX_ATTEMPTS

                    async def run_node(self, node, node_input=None, **kwargs):
                        raise ValueError(
                            "Invalid JSON: EOF while parsing an object"
                        )

                events = [
                    event
                    async for event in coach_agent.process_one_input._func(
                        FakeContext(),
                        {
                            "path": str(image_path),
                            "filename": image_path.name,
                            "mime": "image/png",
                            "feedback_language": "en",
                        },
                    )
                ]
                return events[-1].output

        result = asyncio.run(run_process_one())

        self.assertEqual(result.category, "unsupported")
        self.assertEqual(result.student_name, "Suzy")
        self.assertIsNone(result.feedback)
        self.assertIn("Invalid JSON", result.skipped_reason)

    def test_rename_processed_inputs_updates_nested_filenames_and_avoids_collision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "IMG_1001.JPG"
            source.write_bytes(b"fake image bytes")
            existing = root / "Suzy_2026-06-06.JPG"
            existing.write_bytes(b"existing image bytes")
            result = coach_agent.InputProcessingResult(
                filename=source.name,
                source_path=str(source),
                category="writing",
                student_name="Suzy",
                submission_date="2026.6.6",
                feedback_language="en",
                feedback=coach_agent.EnglishCoachFeedback(
                    filename=source.name,
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
                learning_needs=[
                    coach_agent.LearningNeed(
                        student_name="Suzy",
                        source_type="writing",
                        filename=source.name,
                        skill_tag="grammar",
                        evidence="AI help me",
                        suggested_fix="AI helps me",
                        explanation="Use helps with a singular subject.",
                    )
                ],
            )

            events = list(coach_agent.rename_processed_inputs([result]))
            output = events[-1].output[0]
            renamed_path = root / "Suzy_2026-06-06_2.JPG"

            self.assertFalse(source.exists())
            self.assertTrue(existing.exists())
            self.assertTrue(renamed_path.exists())
            self.assertEqual(output.filename, renamed_path.name)
            self.assertEqual(output.source_path, str(renamed_path))
            self.assertEqual(output.feedback.filename, renamed_path.name)
            self.assertEqual(output.learning_needs[0].filename, renamed_path.name)
            self.assertIn(
                "IMG_1001.JPG -> Suzy_2026-06-06_2.JPG",
                events[0].message.parts[0].text,
            )

    def test_rename_processed_inputs_skips_unknown_student(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "IMG_1001.JPG"
            source.write_bytes(b"fake image bytes")
            result = coach_agent.InputProcessingResult(
                filename=source.name,
                source_path=str(source),
                category="unsupported",
                student_name="unknown",
                submission_date="2026-06-06",
                skipped_reason="Unreadable.",
            )

            events = list(coach_agent.rename_processed_inputs([result]))
            output = events[-1].output[0]

            self.assertTrue(source.exists())
            self.assertEqual(output.filename, source.name)
            self.assertEqual(len(events), 1)

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
                    skill_tag="content_development",
                    evidence="Only one example is used.",
                    suggested_fix="Add one more concrete example.",
                    explanation="Content development item.",
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
                    model_essay=(
                        "During the winter holiday, I will study every day and"
                        " travel with my family."
                    ),
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
                def fake_export(report_path, output_dir):
                    pdf_path = Path(output_dir) / f"{Path(report_path).stem}.pdf"
                    pdf_path.parent.mkdir(parents=True, exist_ok=True)
                    pdf_path.write_bytes(b"%PDF-1.4\n")
                    return pdf_path

                with mock.patch.object(
                    coach_agent,
                    "export_report_pdf",
                    side_effect=fake_export,
                ) as export_mock, mock.patch.object(
                    coach_agent,
                    "open_pdf_in_wps",
                ) as open_mock:
                    events = list(coach_agent.write_report([profile]))
                report = next(coach_agent.REPORTS_DIR.glob("Eve_*.md"))
                payload_path = next(coach_agent.TRAINING_INPUTS_DIR.glob("Eve_*.json"))
                pdf_path = next((coach_agent.REPORTS_DIR / "pdf_exports").glob("Eve_*.pdf"))
                report_text = report.read_text(encoding="utf-8")
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            finally:
                coach_agent.REPORTS_DIR = old_reports_dir
                coach_agent.TRAINING_INPUTS_DIR = old_training_dir

        self.assertEqual(len(events), 1)
        self.assertEqual(export_mock.call_count, 2)
        self.assertEqual(open_mock.call_count, 1)
        self.assertTrue(report_text.startswith("---\nschema_version: 2\n"))
        self.assertIn('report_type: "student_learning_profile"\n', report_text)
        self.assertIn("#### 范文", report_text)
        self.assertIn("#### 题目", report_text)
        self.assertIn("#### 优点", report_text)
        self.assertIn("#### 改进建议", report_text)
        self.assertIn("\n#### 原文\n```text\n", report_text)
        self.assertNotIn("#### Prompt", report_text)
        self.assertNotIn("#### Strengths", report_text)
        self.assertNotIn("#### Improvements", report_text)
        self.assertNotIn("#### 原文转录", report_text)
        self.assertNotIn("#### Transcription", report_text)
        self.assertIn(
            "During the winter holiday, I will study every day and travel with my family.",
            report_text,
        )
        self.assertIn("## Grammar Training Mistakes", report_text)
        self.assertIn("## Personalized Training Input", report_text)
        self.assertEqual(payload["student_name"], "Eve")
        self.assertNotIn("model_essay", payload["feedback_items"][0])
        self.assertEqual(payload["learning_needs"][0]["skill_tag"], "past_tense")
        self.assertIn(str(pdf_path), events[0].message.parts[0].text)
        self.assertIn(f"/reports/{pdf_path.name}", events[0].message.parts[0].text)
        self.assertIn(
            f"/reports/{pdf_path.name}?download=1",
            events[0].message.parts[0].text,
        )

    def test_write_report_merges_session_pdf_per_session(self):
        def make_profile(name: str) -> "coach_agent.StudentLearningProfile":
            return coach_agent.StudentLearningProfile(
                student_name=name,
                feedback_language="zh-Hans",
                feedback_items=[],
                grammar_trainings=[],
                learning_needs=[],
                skipped=[],
            )

        profiles = [make_profile("Eve"), make_profile("Suzy")]

        old_reports_dir = coach_agent.REPORTS_DIR
        old_training_dir = coach_agent.TRAINING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            coach_agent.REPORTS_DIR = Path(tmpdir) / "reports"
            coach_agent.TRAINING_INPUTS_DIR = Path(tmpdir) / "training_inputs"
            try:
                def fake_export(report_path, output_dir):
                    pdf_path = Path(output_dir) / f"{Path(report_path).stem}.pdf"
                    pdf_path.parent.mkdir(parents=True, exist_ok=True)
                    pdf_path.write_bytes(b"%PDF-1.4\n")
                    return pdf_path

                with mock.patch.object(
                    coach_agent,
                    "export_report_pdf",
                    side_effect=fake_export,
                ) as export_mock, mock.patch.object(
                    coach_agent,
                    "open_pdf_in_wps",
                ) as open_mock:
                    events = list(coach_agent.write_report(profiles))
                session_md = next(coach_agent.REPORTS_DIR.glob("session_*.md"))
                session_pdf = next(
                    (coach_agent.REPORTS_DIR / "pdf_exports").glob("session_*.pdf")
                )
                session_text = session_md.read_text(encoding="utf-8")
            finally:
                coach_agent.REPORTS_DIR = old_reports_dir
                coach_agent.TRAINING_INPUTS_DIR = old_training_dir

        # One PDF per student plus a single combined session PDF.
        self.assertEqual(export_mock.call_count, 3)
        open_mock.assert_called_once_with(session_pdf)
        self.assertTrue(
            session_text.startswith("---\nschema_version: 2\n")
        )
        self.assertIn('report_type: "session_learning_profiles"', session_text)
        self.assertIn("student_count: 2", session_text)
        # Exactly one page break separates the two students.
        self.assertEqual(
            session_text.count('<div style="page-break-before: always;"></div>'),
            1,
        )
        self.assertEqual(session_text.count("# Student Learning Profile"), 2)
        message = events[0].message.parts[0].text
        self.assertIn(f"/reports/{session_pdf.name}", message)

    def test_write_report_warns_when_pdf_export_fails(self):
        profile = coach_agent.StudentLearningProfile(
            student_name="Eve",
            feedback_language="zh-Hans",
            feedback_items=[],
            grammar_trainings=[],
            learning_needs=[],
            skipped=[],
        )

        old_reports_dir = coach_agent.REPORTS_DIR
        old_training_dir = coach_agent.TRAINING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            coach_agent.REPORTS_DIR = Path(tmpdir) / "reports"
            coach_agent.TRAINING_INPUTS_DIR = Path(tmpdir) / "training_inputs"
            try:
                with mock.patch.object(
                    coach_agent,
                    "export_report_pdf",
                    side_effect=coach_agent.PdfExportError("pandoc missing"),
                ):
                    events = list(coach_agent.write_report([profile]))
                report = next(coach_agent.REPORTS_DIR.glob("Eve_*.md"))
                payload_path = next(coach_agent.TRAINING_INPUTS_DIR.glob("Eve_*.json"))
                report_exists = report.is_file()
                payload_exists = payload_path.is_file()
            finally:
                coach_agent.REPORTS_DIR = old_reports_dir
                coach_agent.TRAINING_INPUTS_DIR = old_training_dir

        self.assertTrue(report_exists)
        self.assertTrue(payload_exists)
        self.assertIn("PDF export warning(s):", events[0].message.parts[0].text)
        self.assertIn("pandoc missing", events[0].message.parts[0].text)

    def test_write_report_warns_when_wps_open_fails(self):
        profile = coach_agent.StudentLearningProfile(
            student_name="Eve",
            feedback_language="zh-Hans",
            feedback_items=[],
            grammar_trainings=[],
            learning_needs=[],
            skipped=[],
        )

        old_reports_dir = coach_agent.REPORTS_DIR
        old_training_dir = coach_agent.TRAINING_INPUTS_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            coach_agent.REPORTS_DIR = Path(tmpdir) / "reports"
            coach_agent.TRAINING_INPUTS_DIR = Path(tmpdir) / "training_inputs"
            try:
                def fake_export(report_path, output_dir):
                    pdf_path = Path(output_dir) / f"{Path(report_path).stem}.pdf"
                    pdf_path.parent.mkdir(parents=True, exist_ok=True)
                    pdf_path.write_bytes(b"%PDF-1.4\n")
                    return pdf_path

                with mock.patch.object(
                    coach_agent,
                    "export_report_pdf",
                    side_effect=fake_export,
                ), mock.patch.object(
                    coach_agent,
                    "open_pdf_in_wps",
                    side_effect=coach_agent.PdfOpenError("WPS unavailable"),
                ) as open_mock:
                    events = list(coach_agent.write_report([profile]))
                session_pdf = next(
                    (coach_agent.REPORTS_DIR / "pdf_exports").glob("session_*.pdf")
                )
                session_pdf_exists = session_pdf.is_file()
            finally:
                coach_agent.REPORTS_DIR = old_reports_dir
                coach_agent.TRAINING_INPUTS_DIR = old_training_dir

        open_mock.assert_called_once_with(session_pdf)
        self.assertTrue(session_pdf_exists)
        message = events[0].message.parts[0].text
        self.assertIn("WPS open warning(s):", message)
        self.assertIn("WPS unavailable", message)
        self.assertNotIn("PDF export warning(s):", message)

    def test_reports_for_date_matches_markdown_reports_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir)
            today = reports_dir / "A_2026-06-06_09-00-00.md"
            other_day = reports_dir / "A_2026-06-05_09-00-00.md"
            pdf = reports_dir / "A_2026-06-06_09-00-00.pdf"
            today.write_text("# A\n", encoding="utf-8")
            other_day.write_text("# A\n", encoding="utf-8")
            pdf.write_bytes(b"%PDF-1.4\n")

            matches = pdf_export.reports_for_date("2026-06-06", reports_dir)

        self.assertEqual(matches, [today])

    def test_open_pdf_in_wps_uses_bundle_id_on_macos(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "session.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            with mock.patch.object(pdf_export.sys, "platform", "darwin"):
                with mock.patch.object(pdf_export.subprocess, "run") as run_mock:
                    pdf_export.open_pdf_in_wps(pdf_path)

        run_mock.assert_called_once_with(
            [
                "open",
                "-b",
                "com.kingsoft.wpsoffice.mac",
                str(pdf_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_open_pdf_in_wps_skips_non_macos(self):
        with mock.patch.object(pdf_export.sys, "platform", "linux"):
            with mock.patch.object(pdf_export.subprocess, "run") as run_mock:
                pdf_export.open_pdf_in_wps("session.pdf")

        run_mock.assert_not_called()

    def test_open_pdf_in_wps_reports_launch_failure(self):
        failure = subprocess.CalledProcessError(
            1,
            ["open"],
            stderr="Application not found",
        )
        with mock.patch.object(pdf_export.sys, "platform", "darwin"):
            with mock.patch.object(
                pdf_export.subprocess,
                "run",
                side_effect=failure,
            ):
                with self.assertRaises(pdf_export.PdfOpenError) as error:
                    pdf_export.open_pdf_in_wps("session.pdf")

        self.assertIn("Application not found", str(error.exception))

    def test_export_report_pdf_invokes_pandoc_with_print_options(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            markdown = root / "Eve_2026-06-06_09-00-00.md"
            css = root / "report_print.css"
            output_dir = root / "pdf_exports"
            markdown.write_text("# Student Learning Profile\n", encoding="utf-8")
            css.write_text("@page { size: A4; }\n", encoding="utf-8")

            def fake_which(name):
                return f"/tools/{name}"

            def fake_run(command, **kwargs):
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_bytes(b"%PDF-1.4\n")

            with mock.patch.object(pdf_export.shutil, "which", side_effect=fake_which):
                with mock.patch.object(
                    pdf_export.subprocess,
                    "run",
                    side_effect=fake_run,
                ) as run_mock:
                    pdf_path = pdf_export.export_report_pdf(
                        markdown,
                        output_dir=output_dir,
                        css_path=css,
                    )
                    output_dir_exists = output_dir.is_dir()

        command = run_mock.call_args.args[0]
        self.assertEqual(pdf_path, output_dir / "Eve_2026-06-06_09-00-00.pdf")
        self.assertEqual(command[0], "/tools/pandoc")
        self.assertIn(str(markdown), command)
        self.assertIn(f"--css={css}", command)
        self.assertIn("--pdf-engine", command)
        self.assertIn("/tools/wkhtmltopdf", command)
        self.assertIn("--pdf-engine-opt=--enable-local-file-access", command)
        self.assertIn("--pdf-engine-opt=A4", command)
        self.assertIn(str(pdf_path), command)
        self.assertTrue(output_dir_exists)
        self.assertTrue(run_mock.call_args.kwargs["check"])
        self.assertTrue(run_mock.call_args.kwargs["capture_output"])
        self.assertTrue(run_mock.call_args.kwargs["text"])

    def test_export_report_pdf_runs_pandoc_from_writable_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            markdown = root / "Eve_2026-06-06_09-00-00.md"
            css = root / "report_print.css"
            output_dir = root / "pdf_exports"
            markdown.write_text("# Student Learning Profile\n", encoding="utf-8")
            css.write_text("@page { size: A4; }\n", encoding="utf-8")

            def fake_which(name):
                return f"/tools/{name}"

            def fake_run(command, **kwargs):
                cwd = Path(kwargs["cwd"])
                self.assertTrue(cwd.is_dir())
                self.assertEqual(cwd.parent, output_dir)
                self.assertEqual(kwargs["env"]["TMPDIR"], str(cwd))
                self.assertEqual(kwargs["env"]["TEMP"], str(cwd))
                self.assertEqual(kwargs["env"]["TMP"], str(cwd))
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_bytes(b"%PDF-1.4\n")

            with mock.patch.object(pdf_export.shutil, "which", side_effect=fake_which):
                with mock.patch.object(
                    pdf_export.subprocess,
                    "run",
                    side_effect=fake_run,
                ):
                    pdf_path = pdf_export.export_report_pdf(
                        markdown,
                        output_dir=output_dir,
                        css_path=css,
                    )

        self.assertEqual(pdf_path, output_dir / "Eve_2026-06-06_09-00-00.pdf")

    def test_export_report_pdf_reports_missing_tool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            markdown = root / "Eve_2026-06-06_09-00-00.md"
            css = root / "report_print.css"
            markdown.write_text("# Student Learning Profile\n", encoding="utf-8")
            css.write_text("@page { size: A4; }\n", encoding="utf-8")

            def fake_which(name):
                if name == "pandoc":
                    return "/tools/pandoc"
                return None

            with mock.patch.object(pdf_export.shutil, "which", side_effect=fake_which):
                with self.assertRaises(pdf_export.PdfExportError) as error:
                    pdf_export.export_report_pdf(markdown, css_path=css)

        self.assertIn("wkhtmltopdf", str(error.exception))

    def test_export_report_pdf_reports_missing_output_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            markdown = root / "Eve_2026-06-06_09-00-00.md"
            css = root / "report_print.css"
            output_dir = root / "pdf_exports"
            markdown.write_text("# Student Learning Profile\n", encoding="utf-8")
            css.write_text("@page { size: A4; }\n", encoding="utf-8")

            def fake_which(name):
                return f"/tools/{name}"

            with mock.patch.object(pdf_export.shutil, "which", side_effect=fake_which):
                with mock.patch.object(pdf_export.subprocess, "run"):
                    with self.assertRaises(pdf_export.PdfExportError) as error:
                        pdf_export.export_report_pdf(
                            markdown,
                            output_dir=output_dir,
                            css_path=css,
                        )

        self.assertIn("did not create output", str(error.exception))

    def test_pdf_export_main_defaults_to_todays_reports(self):
        report = Path("english_coach/reports/Eve_2026-06-06_09-00-00.md")
        pdf_path = Path("english_coach/reports/pdf_exports/Eve_2026-06-06_09-00-00.pdf")
        with mock.patch.object(pdf_export, "_default_date", return_value="2026-06-06"):
            with mock.patch.object(
                pdf_export,
                "reports_for_date",
                return_value=[report],
            ) as reports_mock:
                with mock.patch.object(
                    pdf_export,
                    "export_report_pdf",
                    return_value=pdf_path,
                ) as export_mock:
                    with mock.patch("builtins.print") as print_mock:
                        result = pdf_export.main([])

        self.assertEqual(result, 0)
        reports_mock.assert_called_once_with("2026-06-06")
        export_mock.assert_called_once_with(report)
        print_mock.assert_called_once_with(pdf_path)


if __name__ == "__main__":
    unittest.main()
