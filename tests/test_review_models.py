"""Teacher changes must survive deterministic projections without stale facts."""

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from english_coach import review_models, utils, workflow
from english_coach.schemas import EnglishCoachFeedback, InputProcessingResult, WritingEvidence


def writing_result(name="Demo Learner"):
    evidence = WritingEvidence(
        student_name=name, submission_date="2026-11-06", prompt_summary="Describe a school day.",
        transcription="He go to school.", required_points_covered=2, required_points_total=3,
        grammar_errors=["He go to school."],
        grammar_error_fixes=[dict(error="He go to school.", suggested_fix="He goes to school.", explanation="Subject agreement.")],
        spelling_errors=[], has_clear_structure=True, has_conclusion=False,
        handwriting_legibility="clear", strengths=["Clear topic."], improvements=["Check verbs."],
    )
    dimensions = utils.score_from_evidence(evidence)
    return InputProcessingResult(
        filename="synthetic.png", category="writing", student_name=name,
        submission_date=evidence.submission_date, writing_evidence=evidence,
        feedback=EnglishCoachFeedback(filename="synthetic.png", student_name=name,
            transcription=evidence.transcription, prompt_summary=evidence.prompt_summary,
            overall_score=utils.calculate_overall_score(dimensions), dimensions=dimensions,
            strengths=evidence.strengths, improvements=evidence.improvements),
    )


class ReviewModelsTest(unittest.TestCase):
    def setUp(self):
        self.original = review_models.items_from_results([writing_result()], ["source-one"])

    def test_deleting_false_error_updates_score_and_removes_derived_need(self):
        edited = deepcopy(self.original)
        edited[0]["transcription"] = "He goes to school."
        edited[0]["errors"] = []
        saved = review_models.validate_items(edited, self.original)
        self.assertEqual(self.original[0]["system_scores"]["language"], 4.5)
        self.assertEqual(saved[0]["system_scores"]["language"], 5)
        profiles = review_models.profiles_from_items(saved)
        self.assertEqual(profiles[0].feedback_items[0].transcription, "He goes to school.")
        self.assertEqual(profiles[0].learning_needs, [])
        self.assertEqual(len(self.original[0]["errors"]), 1)

    def test_teacher_half_scores_survive_recomputed_suggestion_and_total_is_server_owned(self):
        edited = deepcopy(self.original)
        edited[0].update(teacher_scores=dict(content=3.5, structure=4, language=4, handwriting=3), overall_score=20)
        saved = review_models.validate_items(edited, self.original)
        saved[0]["errors"] = []
        again = review_models.validate_items(saved, self.original)
        feedback = review_models.profiles_from_items(again)[0].feedback_items[0]
        self.assertEqual(again[0]["system_scores"]["language"], 5)
        self.assertEqual(feedback.dimensions.content, 3.5)
        self.assertEqual(feedback.dimensions.language, 4)
        self.assertEqual(feedback.overall_score, 14.5)

    def test_forged_identity_derived_scores_and_image_path_cannot_change_projection(self):
        for key, value in (("id", "other"), ("input_id", "other"), ("filename", "other.png"),
                           ("legacy", True), ("category", "grammar_training")):
            with self.subTest(key=key):
                edited = deepcopy(self.original)
                edited[0][key] = value
                with self.assertRaises(ValueError):
                    review_models.validate_items(edited, self.original)
        edited = deepcopy(self.original)
        edited[0].update(system_scores=dict(content=0, structure=0, language=0, handwriting=0),
                         overall_score=0, image_url="file:///private", source_path="../../private")
        saved = review_models.validate_items(edited, self.original)
        self.assertEqual(saved[0]["system_scores"], self.original[0]["system_scores"])
        self.assertNotIn("image_url", saved[0])
        self.assertNotIn("source_path", saved[0])

    def test_invalid_values_rejected_without_leaking_user_content(self):
        changes = [
            {"teacher_scores": dict(content=-1, structure=1, language=1, handwriting=1)},
            {"teacher_scores": dict(content=5.1, structure=1, language=1, handwriting=1)},
            {"teacher_scores": dict(content=2.25, structure=1, language=1, handwriting=1)},
            {"teacher_scores": dict(content=float("nan"), structure=1, language=1, handwriting=1)},
            {"teacher_scores": dict(content=True, structure=1, language=1, handwriting=1)},
            {"submission_date": "2026-02-30"}, {"student_name": ""},
            {"transcription": "private-synthetic-text" * 3000},
            {"rubric": {**self.original[0]["rubric"], "required_points_covered": 4}},
        ]
        for change in changes:
            with self.subTest(field=next(iter(change))):
                edited = deepcopy(self.original)
                edited[0].update(change)
                with self.assertRaises(ValueError) as exc:
                    review_models.validate_items(edited, self.original)
                self.assertNotIn("private-synthetic-text", str(exc.exception))

    def test_add_error_and_focus_then_reassign_student_updates_all_results(self):
        edited = deepcopy(self.original)
        edited[0]["student_name"] = "Another Demo"
        edited[0]["submission_date"] = "2026-11-07"
        edited[0]["errors"].append(dict(id="new-error", kind="spelling", skill_tag="spelling",
            original_answer="skool", correct_answer="school", explanation="Spelling."))
        edited[0]["focuses"].append(dict(id="new-focus", skill_tag="cohesion", evidence="Short sentences.",
            suggested_fix="Connect two ideas.", explanation="Show the relationship."))
        saved = review_models.validate_items(edited, self.original)
        profile = review_models.profiles_from_items(saved)[0]
        self.assertEqual(profile.student_name, "Another Demo")
        self.assertEqual(profile.feedback_items[0].student_name, "Another Demo")
        self.assertEqual(profile.feedback_items[0].submission_date, "2026-11-07")
        self.assertEqual(len(profile.learning_needs), 3)
        self.assertTrue(all(need.student_name == "Another Demo" for need in profile.learning_needs))
        self.assertEqual(saved[0]["system_scores"]["language"], 4)

    def test_duplicate_error_ids_and_removed_items_rejected(self):
        edited = deepcopy(self.original)
        edited[0]["errors"] *= 2
        with self.assertRaises(ValueError):
            review_models.validate_items(edited, self.original)
        with self.assertRaises(ValueError):
            review_models.validate_items([], self.original)

    def test_unknown_submissions_stay_separate_from_each_other_and_named_student(self):
        items = review_models.items_from_results(
            [writing_result("unknown"), writing_result("unknown"), writing_result()], ["one", "two", "three"])
        profiles = review_models.profiles_from_items(items)
        self.assertEqual(len(profiles), 3)
        self.assertTrue(all(len(profile.feedback_items) == 1 for profile in profiles))

    def test_legacy_edit_preserves_original_score_without_fabricating_evidence(self):
        profiles = review_models.profiles_from_items(self.original)
        legacy = review_models.items_from_profiles(profiles)
        self.assertTrue(legacy[0]["legacy"])
        self.assertIsNone(legacy[0]["rubric"])
        edited = deepcopy(legacy)
        edited[0]["system_scores"] = dict(content=0, structure=0, language=0, handwriting=0)
        edited[0]["strengths"] = ["Teacher revision"]
        saved = review_models.validate_items(edited, legacy)
        self.assertEqual(saved[0]["system_scores"], legacy[0]["system_scores"])
        self.assertEqual(review_models.profiles_from_items(saved)[0].feedback_items[0].strengths, ["Teacher revision"])
        edited[0]["rubric"] = self.original[0]["rubric"]
        with self.assertRaises(ValueError):
            review_models.validate_items(edited, legacy)

    @unittest.skipUnless(shutil.which("pandoc"), "Pandoc required for rendered Markdown safety check")
    def test_confirmed_report_escapes_markup_and_preserves_raw_structured_content(self):
        edited = deepcopy(self.original)
        attack = '<img src="file:///synthetic-secret"><style>@import "https://example.invalid/x";</style>![x](https://example.invalid/image)'
        edited[0]["student_name"] = attack[:200]
        edited[0]["prompt_summary"] = attack
        edited[0]["strengths"] = [attack]
        edited[0]["errors"][0]["explanation"] = attack
        edited[0]["transcription"] = "Text\n```\n````\n" + attack
        profiles = review_models.profiles_from_items(review_models.validate_items(edited, self.original))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            def export(path, output_dir):
                target = Path(output_dir) / (Path(path).stem + ".pdf")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"%PDF-synthetic")
                return target
            with mock.patch.object(workflow, "export_report_pdf", side_effect=export):
                list(workflow.write_report(profiles, reports_dir=directory / "reports",
                    training_inputs_dir=directory / "json", pdf_exports_dir=directory / "pdf",
                    open_in_wps=False, review_label="教师确认版本 v3 · 2026-11-06"))
            stored = json.loads(next((directory / "json").glob("*.json")).read_text())
            self.assertEqual(stored["feedback_items"][0]["prompt_summary"], attack)
            for report in (directory / "reports").glob("*.md"):
                markdown = report.read_text()
                self.assertIn("教师确认版本 v3", markdown)
                self.assertIn("提交日期：2026\\-11\\-06", markdown)
                self.assertIn("`````text", markdown)
                rendered = subprocess.run(["pandoc", "--from=gfm+yaml_metadata_block", "--to=html5"],
                                          input=markdown, capture_output=True, text=True, check=True).stdout
                tags = []
                class Tags(HTMLParser):
                    def handle_starttag(self, tag, attrs):
                        tags.append(tag)
                Tags().feed(rendered)
                self.assertNotIn("img", tags)
                self.assertNotIn("style", tags)
                self.assertNotIn("script", tags)


if __name__ == "__main__":
    unittest.main()
