"""Validated teacher drafts and deterministic projections into report models.

The editable error list is the single source for both deductions and learning
needs. Model evidence is retained separately by the submission store.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import utils
from .schemas import (
    DimensionScores, EnglishCoachFeedback, FeedbackLanguage,
    GrammarTrainingEvidence, GrammarTrainingMistake, InputProcessingResult,
    LearningNeed, StudentLearningProfile, WritingEvidence,
)

ShortText = Annotated[str, Field(max_length=200)]
Text = Annotated[str, Field(max_length=5000)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
Score = Annotated[float, Field(ge=0, le=5, multiple_of=0.5, allow_inf_nan=False, strict=True)]


class TeacherScores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Score
    structure: Score
    language: Score
    handwriting: Score


class ReviewRubric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_points_covered: int = Field(ge=0, le=1000, strict=True)
    required_points_total: int = Field(ge=0, le=1000, strict=True)
    has_clear_structure: bool = Field(strict=True)
    has_conclusion: bool = Field(strict=True)
    handwriting_legibility: Literal[
        "excellent", "clear", "readable", "hard_to_read", "illegible",
    ]

    @model_validator(mode="after")
    def check_coverage(self):
        if self.required_points_covered > self.required_points_total:
            raise ValueError("已覆盖要点不能多于总要点")
        return self


class ReviewError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    kind: Literal["grammar", "spelling", "grammar_training"]
    skill_tag: ShortText = "grammar"
    original_answer: Text
    correct_answer: Text
    explanation: Text = ""


class ReviewFocus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    skill_tag: ShortText
    evidence: Text = ""
    suggested_fix: Text = ""
    explanation: Text = ""


class ReviewItem(BaseModel):
    # Read-only API additions (such as image_url) are never persisted here.
    model_config = ConfigDict(extra="ignore")
    id: Identifier
    input_id: Identifier
    category: Literal["writing", "grammar_training"]
    filename: ShortText
    student_name: ShortText
    submission_date: str = Field(default="", max_length=10)
    feedback_language: FeedbackLanguage = "zh-Hans"
    transcription: str = Field(default="", max_length=50000)
    prompt_summary: Text = ""
    errors: list[ReviewError] = Field(default_factory=list, max_length=200)
    focuses: list[ReviewFocus] = Field(default_factory=list, max_length=100)
    strengths: list[Text] = Field(default_factory=list, max_length=100)
    improvements: list[Text] = Field(default_factory=list, max_length=100)
    rubric: ReviewRubric | None = None
    teacher_scores: TeacherScores | None = None
    score_reason: Text = ""
    system_scores: TeacherScores | None = None
    overall_score: float | None = Field(default=None, ge=0, le=20, allow_inf_nan=False)
    legacy: bool = False

    @model_validator(mode="after")
    def check_item(self):
        if not self.student_name.strip():
            raise ValueError("请填写学生姓名，未知可填 unknown")
        if self.submission_date:
            date.fromisoformat(self.submission_date)
        identifiers = [error.id for error in self.errors] + [focus.id for focus in self.focuses]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("错题和练习重点编号不能重复")
        if self.category == "writing":
            if any(error.kind == "grammar_training" for error in self.errors):
                raise ValueError("作文错误类型不正确")
            if not self.legacy and self.rubric is None:
                raise ValueError("作文评分依据缺失")
        else:
            if any(error.kind != "grammar_training" for error in self.errors):
                raise ValueError("语法练习错误类型不正确")
            if self.rubric is not None or self.teacher_scores is not None:
                raise ValueError("语法练习没有作文评分")
        return self


def _evidence(item: ReviewItem) -> WritingEvidence:
    """Use the current evidence, never the initial error count, for suggestions."""
    if item.rubric is None:
        raise ValueError("历史结果缺少完整评分依据")
    return WritingEvidence(
        student_name=item.student_name, submission_date=item.submission_date,
        prompt_summary=item.prompt_summary, transcription=item.transcription,
        grammar_errors=[error.original_answer for error in item.errors if error.kind == "grammar"],
        spelling_errors=[error.original_answer for error in item.errors if error.kind == "spelling"],
        strengths=item.strengths, improvements=item.improvements,
        **item.rubric.model_dump(),
    )


def _calculated(item: ReviewItem) -> dict:
    if item.category == "writing":
        if item.rubric is not None:
            item.system_scores = TeacherScores.model_validate(utils.score_from_evidence(_evidence(item)).model_dump())
        chosen = item.teacher_scores or item.system_scores
        if chosen is None:
            raise ValueError("作文评分缺失")
        item.overall_score = utils.calculate_overall_score(DimensionScores(**chosen.model_dump()))
    else:
        item.system_scores = None
        item.overall_score = None
    return item.model_dump(mode="json")


def validate_items(items: list[dict], previous: list[dict]) -> list[dict]:
    """Validate a full draft and retain server-owned identity and legacy scores."""
    if not isinstance(items, list) or len(items) != len(previous) or not 1 <= len(items) <= 10:
        raise ValueError("请保留本次作业的全部校对项目")
    old = {item["id"]: item for item in previous}
    if len(old) != len(previous) or any(not isinstance(item, dict) for item in items):
        raise ValueError("校对项目格式不正确")
    if [item.get("id") for item in items] != [item["id"] for item in previous]:
        raise ValueError("不能更换、删除或重排作业项目")
    normalized = []
    for incoming in items:
        prior = old[incoming["id"]]
        for key in ("id", "input_id", "category", "filename", "feedback_language", "legacy"):
            if incoming.get(key) != prior.get(key):
                raise ValueError("作业来源信息不能修改")
        if (incoming.get("rubric") is None) != (prior.get("rubric") is None):
            raise ValueError("不能添加或删除原有评分依据")
        trusted = {**incoming, "system_scores": prior.get("system_scores"), "overall_score": None}
        try:
            item = ReviewItem.model_validate(trusted)
            normalized.append(_calculated(item))
        except (ValidationError, ValueError, TypeError) as exc:
            # Do not include user content in exception strings or logs.
            raise ValueError("内容格式不正确：请检查必填项、日期、分值范围、要点数量和文本长度。") from None
    return normalized


def items_from_results(results: list[InputProcessingResult | dict], input_ids: list[str]) -> list[dict]:
    """Keep all-result/input ordering, then omit unsuccessful extractions."""
    if len(results) != len(input_ids) or len(set(input_ids)) != len(input_ids):
        raise ValueError("输入与结果对应关系不正确")
    items = []
    for raw, input_id in zip(results, input_ids, strict=True):
        result = InputProcessingResult.model_validate(raw)
        if result.feedback is None and result.grammar_training is None:
            continue
        data = dict(
            id=input_id, input_id=input_id, category=result.category,
            filename=result.filename, student_name=result.student_name,
            submission_date=result.submission_date, feedback_language=result.feedback_language,
        )
        errors = []
        focuses = []
        if result.feedback is not None:
            feedback = result.feedback
            data.update(transcription=feedback.transcription, prompt_summary=feedback.prompt_summary,
                        strengths=feedback.strengths, improvements=feedback.improvements,
                        system_scores=feedback.dimensions.model_dump())
            evidence = result.writing_evidence
            if evidence is not None:
                data["rubric"] = {key: getattr(evidence, key) for key in ReviewRubric.model_fields}
                # The existing scorer clamps excessive coverage. Preserve the raw
                # evidence separately, while presenting its effective coverage.
                data["rubric"]["required_points_covered"] = min(evidence.required_points_covered, evidence.required_points_total)
                for kind in ("grammar", "spelling"):
                    for index, error in enumerate(getattr(evidence, f"{kind}_errors")):
                        fix, explanation = utils.writing_issue_fix(
                            error=error, index=index, fixes=getattr(evidence, f"{kind}_error_fixes"),
                            fallback_suggested_fix="请核对并补充正确表达。",
                            fallback_explanation="请结合原图核对。",
                        )
                        errors.append(dict(id=f"{kind}-{index + 1}", kind=kind, skill_tag=kind,
                                           original_answer=error, correct_answer=fix, explanation=explanation))
                focuses = [dict(id=f"focus-{index + 1}", **focus.model_dump())
                           for index, focus in enumerate(evidence.writing_training_focuses)]
            else:
                data["legacy"] = True
                focuses = [dict(id=f"focus-{index + 1}", skill_tag=need.skill_tag, evidence=need.evidence,
                                suggested_fix=need.suggested_fix, explanation=need.explanation)
                           for index, need in enumerate(result.learning_needs)]
        elif result.grammar_training is not None:
            errors = [dict(id=f"error-{index + 1}", kind="grammar_training", **mistake.model_dump())
                      for index, mistake in enumerate(result.grammar_training.mistakes)]
        data.update(errors=errors, focuses=focuses)
        items.append(_calculated(ReviewItem.model_validate(data)))
    return items


def profiles_from_items(items: list[dict]) -> list[StudentLearningProfile]:
    """Project a saved draft/snapshot; no model calls or filesystem effects."""
    profiles: dict[tuple[bool, str, str], StudentLearningProfile] = {}
    for raw in items:
        item = ReviewItem.model_validate(raw)
        calculated = _calculated(item)
        item = ReviewItem.model_validate(calculated)
        name = item.student_name.strip()
        key = (utils.known_name(name), name if utils.known_name(name) else item.id, item.feedback_language)
        profile = profiles.setdefault(key, StudentLearningProfile(student_name=name, feedback_language=item.feedback_language))
        if item.category == "writing":
            chosen = item.teacher_scores or item.system_scores
            profile.feedback_items.append(EnglishCoachFeedback(
                filename=item.filename, student_name=name, submission_date=item.submission_date,
                feedback_language=item.feedback_language,
                prompt_summary=item.prompt_summary, transcription=item.transcription,
                overall_score=item.overall_score, dimensions=DimensionScores(**chosen.model_dump()),
                strengths=item.strengths, improvements=item.improvements,
            ))
        else:
            profile.grammar_trainings.append(GrammarTrainingEvidence(
                student_name=name, submission_date=item.submission_date,
                mistakes=[GrammarTrainingMistake(**error.model_dump(exclude={"id", "kind"})) for error in item.errors],
            ))
        profile.learning_needs.extend(LearningNeed(
            student_name=name, source_type=item.category, filename=item.filename,
            skill_tag=error.skill_tag, evidence=error.original_answer,
            suggested_fix=error.correct_answer, explanation=error.explanation,
        ) for error in item.errors)
        profile.learning_needs.extend(LearningNeed(
            student_name=name, source_type=item.category, filename=item.filename,
            **focus.model_dump(exclude={"id"}),
        ) for focus in item.focuses)
    return list(profiles.values())


def items_from_profiles(profiles: list[dict | StudentLearningProfile]) -> list[dict]:
    """Allow legacy feedback edits without inventing missing scoring evidence."""
    results = []
    for raw in profiles:
        profile = StudentLearningProfile.model_validate(raw)
        used_needs: set[int] = set()
        for feedback in profile.feedback_items:
            matching = [(index, need) for index, need in enumerate(profile.learning_needs)
                        if need.source_type == "writing" and need.filename == feedback.filename]
            used_needs.update(index for index, _ in matching)
            results.append(InputProcessingResult(
                filename=feedback.filename, category="writing", student_name=profile.student_name,
                submission_date=feedback.submission_date,
                feedback_language=profile.feedback_language, feedback=feedback,
                learning_needs=[need for _, need in matching],
            ))
        for index, grammar in enumerate(profile.grammar_trainings):
            matching = [(n, need) for n, need in enumerate(profile.learning_needs)
                        if n not in used_needs and need.source_type == "grammar_training"
                        and any(need.evidence == error.original_answer and need.suggested_fix == error.correct_answer
                                for error in grammar.mistakes)]
            used_needs.update(n for n, _ in matching)
            results.append(InputProcessingResult(
                filename=matching[0][1].filename if matching else f"历史语法练习-{index + 1}",
                category="grammar_training", student_name=profile.student_name,
                submission_date=grammar.submission_date, feedback_language=profile.feedback_language,
                grammar_training=grammar,
            ))
    items = items_from_results(results, [f"legacy-{index + 1}" for index in range(len(results))])
    for item in items:
        item["legacy"] = True
    return items
