"""
Utility functions for English Coach workflow.

Provides helper functions for:
- Text extraction and language detection
- File naming and path management
- Data validation and transformation
- Scoring calculations
- Internationalization
"""

from __future__ import annotations

import base64
import datetime
import json
import re
from pathlib import Path
from typing import TypeVar

from google.genai import types
from pydantic import BaseModel

from . import config
from .schemas import (
    DimensionScores,
    FeedbackLanguage,
    GrammarTrainingEvidence,
    LearningNeed,
    WritingEvidence,
    WritingIssueFix,
)


# ============================================================================
# Type Variables
# ============================================================================

ModelT = TypeVar("ModelT", bound=BaseModel)


# ============================================================================
# Content & Text Extraction
# ============================================================================

def _content_text(value: types.Content) -> str:
    """Extract plain text from Content, excluding thought parts."""
    return "".join(
        part.text
        for part in value.parts or []
        if part.text and not getattr(part, "thought", False)
    )


def _node_input_text(node_input: object) -> str:
    """Convert node input to plain text string."""
    if isinstance(node_input, types.Content):
        return _content_text(node_input)
    return str(node_input or "")


# ============================================================================
# Language Detection
# ============================================================================

def _has_ascii_alias(text: str, aliases: tuple[str, ...]) -> bool:
    """Check if text contains any ASCII alias as a standalone word."""
    for alias in aliases:
        pattern = rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)"
        if re.search(pattern, text):
            return True
    return False


def feedback_language_from_input(node_input: object) -> FeedbackLanguage:
    """Detect feedback language from user input text.

    Checks for language keywords in both English and native scripts.
    Returns DEFAULT_FEEDBACK_LANGUAGE if no match found.
    """
    text = _node_input_text(node_input).lower()

    if _has_ascii_alias(text, ("en", "eng", "english")) or any(
        alias in text for alias in ("英文", "英语")
    ):
        return "en"

    if _has_ascii_alias(text, ("ja", "jp", "japanese")) or any(
        alias in text for alias in ("日文", "日语", "日本語", "日本语")
    ):
        return "ja"

    if _has_ascii_alias(text, ("ko", "kr", "korean")) or any(
        alias in text for alias in ("韩文", "韓文", "韩语", "韓語", "한국어")
    ):
        return "ko"

    if _has_ascii_alias(
        text, ("zh", "zh-cn", "zh-hans", "cn", "chinese")
    ) or any(alias in text for alias in ("中文", "汉语", "漢語", "简体", "簡體")):
        return "zh-Hans"

    return config.DEFAULT_FEEDBACK_LANGUAGE


# ============================================================================
# Blob & Image Handling
# ============================================================================

def blob_bytes(data: object) -> bytes:
    """Convert various blob data formats to bytes."""
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return base64.b64decode(data)
    return bytes(data)


def mime_for_inline_image(blob: types.Blob) -> str | None:
    """Determine MIME type from blob or its display name suffix."""
    mime = (blob.mime_type or "").lower()
    if mime in config.SUFFIX_BY_MIME:
        return mime
    suffix = Path(blob.display_name or "").suffix.lower()
    return config.MIME_BY_SUFFIX.get(suffix)


def image_content(
    *,
    path: str,
    filename: str,
    mime: str,
    feedback_language: FeedbackLanguage,
    task: str,
) -> types.Content:
    """Build multimodal Content for agent input with image and metadata."""
    data = Path(path).read_bytes()
    return types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=(
                    f"filename: {filename}\n"
                    f"feedback_language: {feedback_language}\n"
                    f"{task}"
                )
            ),
            types.Part.from_bytes(data=data, mime_type=mime),
        ],
    )


# ============================================================================
# File Naming & Path Management
# ============================================================================

def safe_filename_segment(name: str) -> str:
    """Make a name safe for use as a filename segment."""
    s = (name or "").strip().replace(" ", "_")
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', "", s)
    return s or "unknown"


def canonical_input_stem(student_name: str, submission_date: str) -> str:
    """Generate canonical filename stem: <student>_<date>."""
    segment = safe_filename_segment(student_name)
    segment = re.sub(r"^[._-]+|[._-]+$", "", segment)
    segment = segment or "unknown"
    return f"{segment}_{submission_date}"


def safe_upload_filename(
    *,
    display_name: str | None,
    index: int,
    mime: str,
) -> str:
    """Generate safe upload filename from display name or index."""
    suffix = config.SUFFIX_BY_MIME.get(mime, ".png")
    raw_name = Path(display_name or "").name.strip()
    if not raw_name:
        return f"uploaded_{index}{suffix}"

    raw_name = safe_filename_segment(raw_name)
    raw_path = Path(raw_name)
    raw_suffix = raw_path.suffix.lower()
    if raw_suffix in config.MIME_BY_SUFFIX:
        suffix = raw_suffix

    stem = raw_path.stem.strip(" ._-")
    stem = re.sub(r"\s+", "_", stem)
    stem = stem or f"uploaded_{index}"
    return f"{stem}{suffix}"


def unique_path(
    *,
    directory: Path,
    stem: str,
    suffix: str,
    exclude: Path | None = None,
) -> Path:
    """Find a unique path in directory with the given stem and suffix.

    If exclude is provided and matches the candidate, return exclude unchanged.
    """
    candidate = directory / f"{stem}{suffix}"
    if candidate == exclude:
        return exclude
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if candidate == exclude:
            return exclude
        if not candidate.exists():
            return candidate
        index += 1


def unique_upload_path(directory: Path, filename: str) -> Path:
    """Generate unique upload path for a filename."""
    return unique_path(
        directory=directory,
        stem=Path(filename).stem,
        suffix=Path(filename).suffix,
        exclude=None,
    )


def unique_input_path(
    *,
    source_path: Path,
    target_stem: str,
) -> Path:
    """Generate unique input path with new stem in same directory."""
    return unique_path(
        directory=source_path.parent,
        stem=target_stem,
        suffix=source_path.suffix,
        exclude=source_path,
    )


# ============================================================================
# Student Name & Date Resolution
# ============================================================================

def known_name(value: str | None) -> bool:
    """Check if a student name is known (not empty or 'unknown')."""
    return bool(value and value.strip() and value.strip().lower() != "unknown")


def student_hint_from_filename(filename: str) -> str:
    """Extract potential student name from filename first segment."""
    stem = Path(filename).stem.strip()
    if not stem:
        return "unknown"
    first = re.split(r"[_\-\s]+", stem, maxsplit=1)[0].strip()
    if not first:
        return "unknown"
    generic = {
        "img",
        "image",
        "writing",
        "essay",
        "grammar",
        "screenshot",
        "scan",
        "photo",
    }
    if first.lower() in generic or re.fullmatch(r"\d+", first):
        return "unknown"
    return first


def resolve_student_name(
    *,
    filename: str,
    category_name: str,
    evidence_name: str,
) -> str:
    """Resolve student name from multiple sources with priority."""
    if known_name(evidence_name):
        return evidence_name.strip()
    if known_name(category_name):
        return category_name.strip()
    return student_hint_from_filename(filename)


def normalize_submission_date(value: str | None) -> str:
    """Parse and normalize submission date to YYYY-MM-DD format."""
    text = (value or "").strip()
    if not text:
        return ""
    match = re.search(
        r"(?P<year>\d{4})\s*(?:[-./]|年)\s*"
        r"(?P<month>\d{1,2})\s*(?:[-./]|月)\s*"
        r"(?P<day>\d{1,2})",
        text,
    )
    if not match:
        return ""
    try:
        return datetime.date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        ).isoformat()
    except ValueError:
        return ""


def default_submission_date() -> str:
    """Get current date as default submission date."""
    return datetime.datetime.now().astimezone().date().isoformat()


def resolve_submission_date(
    *,
    evidence_date: str | None,
    category_date: str | None,
) -> str:
    """Resolve submission date from multiple sources with fallback."""
    return (
        normalize_submission_date(evidence_date)
        or normalize_submission_date(category_date)
        or default_submission_date()
    )


# ============================================================================
# Model Output Conversion
# ============================================================================

def model_from_output(value: object, model_type: type[ModelT]) -> ModelT:
    """Convert various output formats to a Pydantic model instance."""
    if isinstance(value, model_type):
        return model_type.model_validate(value.model_dump())
    if isinstance(value, types.Content):
        return model_type.model_validate_json(_content_text(value))
    if isinstance(value, dict):
        return model_type.model_validate(value)
    if isinstance(value, str):
        return model_type.model_validate_json(value)
    return model_type.model_validate(value)


# ============================================================================
# Scoring Calculations
# ============================================================================

def calculate_overall_score(dimensions: DimensionScores) -> float:
    """Calculate overall score from dimension scores (max 20)."""
    total = (
        dimensions.content
        + dimensions.structure
        + dimensions.language
        + dimensions.handwriting
    )
    return round(total, 1)


def score_from_evidence(evidence: WritingEvidence) -> DimensionScores:
    """Calculate dimension scores from writing evidence."""
    # Content score (0-5) based on required points coverage
    total = evidence.required_points_total
    covered = min(evidence.required_points_covered, total)
    if total <= 0:
        content = 3
    else:
        ratio = covered / total
        if ratio >= 1:
            content = 5
        elif ratio >= 2 / 3:
            content = 4
        elif ratio >= 1 / 3:
            content = 3
        elif covered > 0:
            content = 2
        else:
            content = 1

    # Structure score (0-5) based on clarity and conclusion
    structure = min(
        5,
        3 + int(evidence.has_clear_structure) + int(evidence.has_conclusion),
    )

    # Language score (0-5) penalized by error count
    language_error_count = (
        len(evidence.grammar_errors) + len(evidence.spelling_errors)
    )
    language = max(0.0, 5 - language_error_count * 0.5)

    # Handwriting score (1-5) from legibility rating
    handwriting = {
        "excellent": 5,
        "clear": 4,
        "readable": 3,
        "hard_to_read": 2,
        "illegible": 1,
    }[evidence.handwriting_legibility]

    return DimensionScores(
        content=content,
        structure=structure,
        language=language,
        handwriting=handwriting,
    )


# ============================================================================
# Writing Issue Processing
# ============================================================================

def issue_key(text: str) -> str:
    """Normalize issue text for comparison."""
    cleaned = text.strip()
    cleaned = cleaned.removeprefix("Review and correct:").strip()
    cleaned = cleaned.strip("\"'""")
    return re.sub(r"\s+", " ", cleaned).casefold()


def is_same_issue_text(left: str, right: str) -> bool:
    """Check if two issue texts refer to the same issue."""
    return issue_key(left) == issue_key(right)


def writing_issue_fix(
    *,
    error: str,
    index: int,
    fixes: list[WritingIssueFix],
    fallback_suggested_fix: str,
    fallback_explanation: str,
) -> tuple[str, str]:
    """Find matching fix for an error, or return fallback."""
    error_key = issue_key(error)
    candidate: WritingIssueFix | None = None
    for fix in fixes:
        if issue_key(fix.error) == error_key:
            candidate = fix
            break
    if candidate is None and index < len(fixes):
        candidate = fixes[index]

    if candidate is None:
        return fallback_suggested_fix, fallback_explanation

    suggested_fix = candidate.suggested_fix.strip()
    explanation = candidate.explanation.strip() or fallback_explanation
    if not suggested_fix or is_same_issue_text(error, suggested_fix):
        return fallback_suggested_fix, fallback_explanation
    return suggested_fix, explanation


# ============================================================================
# Learning Needs Generation
# ============================================================================

def writing_learning_needs(
    *,
    filename: str,
    student_name: str,
    evidence: WritingEvidence,
) -> list[LearningNeed]:
    """Generate learning needs from writing evidence."""
    needs: list[LearningNeed] = []

    # Grammar errors
    for index, error in enumerate(evidence.grammar_errors):
        suggested_fix, explanation = writing_issue_fix(
            error=error,
            index=index,
            fixes=evidence.grammar_error_fixes,
            fallback_suggested_fix="Rewrite this snippet with correct grammar.",
            fallback_explanation="Grammar error found in writing.",
        )
        needs.append(
            LearningNeed(
                student_name=student_name,
                source_type="writing",
                filename=filename,
                skill_tag="grammar",
                evidence=error,
                suggested_fix=suggested_fix,
                explanation=explanation,
            )
        )

    # Spelling errors
    for index, error in enumerate(evidence.spelling_errors):
        suggested_fix, explanation = writing_issue_fix(
            error=error,
            index=index,
            fixes=evidence.spelling_error_fixes,
            fallback_suggested_fix="Use the correct spelling for this word.",
            fallback_explanation="Spelling error found in writing.",
        )
        needs.append(
            LearningNeed(
                student_name=student_name,
                source_type="writing",
                filename=filename,
                skill_tag="spelling",
                evidence=error,
                suggested_fix=suggested_fix,
                explanation=explanation,
            )
        )

    # Writing training focuses
    for focus in evidence.writing_training_focuses:
        needs.append(
            LearningNeed(
                student_name=student_name,
                source_type="writing",
                filename=filename,
                skill_tag=focus.skill_tag,
                evidence=focus.evidence,
                suggested_fix=focus.suggested_fix,
                explanation=focus.explanation,
            )
        )

    return needs


def grammar_learning_needs(
    *,
    filename: str,
    student_name: str,
    evidence: GrammarTrainingEvidence,
) -> list[LearningNeed]:
    """Generate learning needs from grammar training evidence."""
    return [
        LearningNeed(
            student_name=student_name,
            source_type="grammar_training",
            filename=filename,
            skill_tag=m.skill_tag,
            evidence=m.original_answer,
            suggested_fix=m.correct_answer,
            explanation=m.explanation,
        )
        for m in evidence.mistakes
    ]


# ============================================================================
# Error Handling
# ============================================================================

def short_failure_reason(exc: BaseException) -> str:
    """Extract short failure reason from exception."""
    inner = getattr(exc, "error", None)
    if isinstance(inner, BaseException):
        exc = inner
    reason = " ".join(f"{type(exc).__name__}: {exc}".split())
    if len(reason) > 300:
        return f"{reason[:300]}..."
    return reason


def task_for_attempt(task: str, attempt_count: int) -> str:
    """Vary the prompt on retries to avoid deterministic replay.

    With temperature=0 + seed=0, identical prompts produce identical outputs,
    including failures. This adds variation to break failure loops.
    """
    if attempt_count <= 1:
        return task
    return (
        f"{task}\n"
        f"Retry attempt {attempt_count}: the previous attempt produced invalid"
        " output. Respond with exactly one complete, valid JSON object, keep"
        " every field concise, and never repeat the same sentence or phrase."
    )


# ============================================================================
# Report Rendering Utilities
# ============================================================================

def yaml_string(value: str) -> str:
    """Format string for YAML frontmatter."""
    return json.dumps(value, ensure_ascii=False)


def markdown_cell(value: object) -> str:
    """Escape value for use in Markdown table cell."""
    return str(value).replace("\n", "<br>").replace("|", "\\|")


def i18n_report(key: str, lang: FeedbackLanguage) -> str:
    """Return localized report section heading or label."""
    translations = {
        "title": {
            "zh-Hans": "学生学习档案",
            "en": "Student Learning Profile",
            "ja": "学習プロファイル",
            "ko": "학습 프로필",
        },
        "score_summary": {
            "zh-Hans": "分数汇总",
            "en": "Score Summary",
            "ja": "スコアサマリー",
            "ko": "점수 요약",
        },
        "submission_details": {
            "zh-Hans": "作业详情",
            "en": "Submission Details",
            "ja": "提出物の詳細",
            "ko": "제출 세부사항",
        },
        "score_breakdown": {
            "zh-Hans": "分数明细",
            "en": "Score Breakdown",
            "ja": "スコア内訳",
            "ko": "점수 세부내역",
        },
        "prompt": {
            "zh-Hans": "题目",
            "en": "Prompt",
            "ja": "課題",
            "ko": "주제",
        },
        "strengths": {
            "zh-Hans": "优点",
            "en": "Strengths",
            "ja": "良い点",
            "ko": "장점",
        },
        "improvements": {
            "zh-Hans": "改进建议",
            "en": "Areas for Improvement",
            "ja": "改善点",
            "ko": "개선 사항",
        },
        "original": {
            "zh-Hans": "原文",
            "en": "Original Text",
            "ja": "原文",
            "ko": "원문",
        },
        "model_essay": {
            "zh-Hans": "范文",
            "en": "Model Essay",
            "ja": "模範解答",
            "ko": "모범 답안",
        },
        "grammar_training": {
            "zh-Hans": "语法训练错题",
            "en": "Grammar Training Mistakes",
            "ja": "文法トレーニング誤答",
            "ko": "문법 훈련 오답",
        },
        "training_input": {
            "zh-Hans": "个性化训练建议",
            "en": "Personalized Training Input",
            "ja": "パーソナライズトレーニング",
            "ko": "맞춤형 훈련 제안",
        },
        "no_grammar_data": {
            "zh-Hans": "暂无语法训练数据",
            "en": "No grammar training data yet",
            "ja": "文法トレーニングデータなし",
            "ko": "아직 문법 훈련 데이터 없음",
        },
        "no_training_needs": {
            "zh-Hans": "暂无训练建议",
            "en": "No training recommendations yet",
            "ja": "トレーニング提案なし",
            "ko": "아직 훈련 제안 없음",
        },
        "col_submission": {
            "zh-Hans": "作业",
            "en": "Submission",
            "ja": "提出物",
            "ko": "제출물",
        },
        "col_skill": {
            "zh-Hans": "技能点",
            "en": "Skill",
            "ja": "スキル",
            "ko": "기술",
        },
        "col_original": {
            "zh-Hans": "原答案",
            "en": "Original",
            "ja": "元の解答",
            "ko": "원래 답",
        },
        "col_correct": {
            "zh-Hans": "正确答案",
            "en": "Correct",
            "ja": "正解",
            "ko": "정답",
        },
        "col_explanation": {
            "zh-Hans": "解释",
            "en": "Explanation",
            "ja": "説明",
            "ko": "설명",
        },
        "col_source": {
            "zh-Hans": "来源",
            "en": "Source",
            "ja": "ソース",
            "ko": "출처",
        },
        "col_evidence": {
            "zh-Hans": "原文",
            "en": "Evidence",
            "ja": "証拠",
            "ko": "근거",
        },
        "col_fix": {
            "zh-Hans": "改进方向",
            "en": "Suggested Fix",
            "ja": "改善案",
            "ko": "개선 방향",
        },
    }
    return translations.get(key, {}).get(lang, translations[key]["en"])
