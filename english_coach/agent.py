"""
Workflow: produce English coaching feedback and training inputs from screenshots.

Reads ADK Web uploaded images or ./input/* and ./input/tem/* supported images,
classifies each image, sends it to the matching Gemini structured extractor,
renames each processed input from visible student/date evidence, merges all
outputs into per-student learning profiles, and writes both Markdown reports
and JSON training inputs.

Composition:
    list_writing_inputs -> orchestrate -> rename_processed_inputs
        -> build_student_profiles -> write_report
                       └─ ctx.run_node + asyncio.gather over process_one_input
                            ├─ classify_input_image -> extractor
                            ├─ classify_input_image -> grammar_training_extractor
                            └─ classify_input_image -> unsupported fallback

From adk_kit:
    recipes/router_intent.py    (classify -> pick route -> specialist)
    recipes/dynamic_parallel.py (runtime fan-out via ctx.run_node + gather)
    events/event_message.py     (multimodal Part input pattern)
    nodes/agent_structured.py   (Agent + output_schema)
    nodes/node_decorator.py     (@node knobs)
    context/ctx_run_node.py     (dynamic sub-node execution)
    reliability/retry.py        (RetryConfig on flaky multimodal steps)
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import re
from pathlib import Path
from typing import Literal
from typing import TypeVar
from urllib.parse import quote

from google.adk import Agent
from google.adk import Context
from google.adk import Event
from google.adk import Workflow
from google.adk.workflow import RetryConfig
from google.adk.workflow import node
from google.genai import types
from pydantic import BaseModel
from pydantic import Field

from . import history_store
from . import report_layout
from .pdf_export import PdfExportError
from .pdf_export import PdfOpenError
from .pdf_export import export_report_pdf
from .pdf_export import open_pdf_in_wps

WRITING_INPUTS_DIR = history_store.resolve_writing_inputs_dir()
REPORTS_DIR = history_store.resolve_reports_dir()
TRAINING_INPUTS_DIR = history_store.resolve_training_inputs_dir()
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
SUFFIX_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

FeedbackLanguage = Literal["zh-Hans", "en", "ja", "ko"]
InputRoute = Literal["writing", "grammar_training", "unsupported"]
LearningSource = Literal["writing", "grammar_training"]
WritingTrainingSkill = Literal[
    "writing_structure",
    "content_development",
    "cohesion",
    "vocabulary_precision",
]
DEFAULT_FEEDBACK_LANGUAGE: FeedbackLanguage = "zh-Hans"
ModelT = TypeVar("ModelT", bound=BaseModel)


def _content_text(value: types.Content) -> str:
  return "".join(
      part.text
      for part in value.parts or []
      if part.text and not getattr(part, "thought", False)
  )


def _node_input_text(node_input: object) -> str:
  if isinstance(node_input, types.Content):
    return _content_text(node_input)
  return str(node_input or "")


def _has_ascii_alias(text: str, aliases: tuple[str, ...]) -> bool:
  for alias in aliases:
    pattern = rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)"
    if re.search(pattern, text):
      return True
  return False


def _feedback_language_from_input(node_input: object) -> FeedbackLanguage:
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
  return DEFAULT_FEEDBACK_LANGUAGE


class ImageCategory(BaseModel):
  category: InputRoute
  student_name: str = "unknown"
  submission_date: str = ""
  confidence: float = Field(ge=0, le=1)
  reason: str


class DimensionScores(BaseModel):
  content: int
  structure: int
  language: float
  handwriting: int


class WritingIssueFix(BaseModel):
  error: str
  suggested_fix: str
  explanation: str


class WritingTrainingFocus(BaseModel):
  skill_tag: WritingTrainingSkill
  evidence: str
  suggested_fix: str
  explanation: str


class WritingEvidence(BaseModel):
  student_name: str
  submission_date: str = ""
  prompt_summary: str
  transcription: str
  model_essay: str = ""
  required_points_covered: int = Field(ge=0)
  required_points_total: int = Field(ge=0)
  grammar_errors: list[str]
  grammar_error_fixes: list[WritingIssueFix] = Field(default_factory=list)
  spelling_errors: list[str]
  spelling_error_fixes: list[WritingIssueFix] = Field(default_factory=list)
  has_clear_structure: bool
  has_conclusion: bool
  handwriting_legibility: Literal[
      "excellent",
      "clear",
      "readable",
      "hard_to_read",
      "illegible",
  ]
  strengths: list[str]
  improvements: list[str]
  writing_training_focuses: list[WritingTrainingFocus] = Field(
      default_factory=list
  )


class EnglishCoachFeedback(BaseModel):
  filename: str
  student_name: str
  feedback_language: FeedbackLanguage = DEFAULT_FEEDBACK_LANGUAGE
  prompt_summary: str
  transcription: str
  model_essay: str = Field(default="", exclude=True)
  overall_score: float
  dimensions: DimensionScores
  strengths: list[str]
  improvements: list[str]


class GrammarTrainingMistake(BaseModel):
  skill_tag: str
  original_answer: str
  correct_answer: str
  explanation: str


class GrammarTrainingEvidence(BaseModel):
  student_name: str = "unknown"
  submission_date: str = ""
  mistakes: list[GrammarTrainingMistake] = Field(default_factory=list)


class LearningNeed(BaseModel):
  student_name: str
  source_type: LearningSource
  filename: str
  skill_tag: str
  evidence: str
  suggested_fix: str
  explanation: str


class InputProcessingResult(BaseModel):
  filename: str
  source_path: str = ""
  category: InputRoute
  student_name: str
  submission_date: str = ""
  feedback_language: FeedbackLanguage = DEFAULT_FEEDBACK_LANGUAGE
  feedback: EnglishCoachFeedback | None = None
  grammar_training: GrammarTrainingEvidence | None = None
  learning_needs: list[LearningNeed] = Field(default_factory=list)
  skipped_reason: str | None = None


class StudentLearningProfile(BaseModel):
  student_name: str
  feedback_language: FeedbackLanguage = DEFAULT_FEEDBACK_LANGUAGE
  feedback_items: list[EnglishCoachFeedback] = Field(default_factory=list)
  grammar_trainings: list[GrammarTrainingEvidence] = Field(default_factory=list)
  learning_needs: list[LearningNeed] = Field(default_factory=list)
  skipped: list[str] = Field(default_factory=list)


classify_input_image = Agent(
    name="classify_input_image",
    model="gemini-flash-latest",
    instruction=(
        "You are routing a teacher's uploaded English-learning screenshot."
        " The user message contains a filename label followed by one image.\n\n"
        "Return category as exactly one of:\n"
        "- writing: a writing prompt plus a student's handwritten response.\n"
        "- grammar_training: grammar practice, corrected grammar exercises,"
        " fill-in grammar answers, or visible grammar mistakes to review.\n"
        "- unsupported: anything else, unreadable images, or non-English work.\n\n"
        "student_name: return the student's name if visible, otherwise"
        " \"unknown\". submission_date: return the date visibly written or"
        " printed on the image, normalized as YYYY-MM-DD when possible;"
        " otherwise return an empty string. confidence: 0 to 1. reason: one"
        " short explanation."
    ),
    output_schema=ImageCategory,
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        seed=0,
        max_output_tokens=4096,
    ),
)


extractor = Agent(
    name="extractor",
    model="gemini-flash-latest",
    instruction=(
        "You are an experienced writing teacher. The user message contains a"
        " filename label followed by a single image. The image shows, top to"
        " bottom, the printed writing prompt and the student's handwritten"
        " response.\n\n"
        "Read both. Extract stable grading evidence only. Do not assign"
        " scores.\n\n"
        "student_name: the student's name as written on the image, usually at"
        " the top of the page or in a header/label area. Return only the name"
        " itself; strip labels like \"Name:\" / \"姓名:\" / \"Student:\"."
        " If you cannot find a name, return \"unknown\".\n"
        "submission_date: the date visibly written or printed on the image,"
        " usually near the top/header. Return it normalized as YYYY-MM-DD when"
        " possible. If no date is visible, return an empty string.\n"
        "prompt_summary: one sentence describing what the writing task was meant"
        " to address.\n"
        "transcription: the student's handwritten response transcribed"
        " verbatim. Preserve their original words, line breaks, spelling, and"
        " grammar; do not silently correct mistakes. Use \\n for line breaks."
        " Do not include the printed prompt.\n"
        "model_essay: an English high-scoring rewritten sample based on the"
        " prompt and the student's transcription. Preserve the student's main"
        " ideas when relevant, correct grammar and spelling, add missing"
        " required points if needed, and keep it age-appropriate. Return only"
        " the model essay text; do not include a title, bullets, or"
        " explanation. Use \\n for paragraph breaks.\n"
        "required_points_total: count distinct required content points in the"
        " prompt.\n"
        "required_points_covered: count how many required points the response"
        " addresses, even if imperfectly.\n"
        "grammar_errors: list distinct grammar errors in the response. Use"
        " short quoted snippets.\n"
        "grammar_error_fixes: one item per grammar_errors entry. error must"
        " exactly repeat the matching grammar_errors snippet. suggested_fix"
        " must be the corrected wording only, not an instruction to review."
        " explanation should briefly explain the correction.\n"
        "spelling_errors: list distinct spelling errors. Use short snippets.\n"
        "spelling_error_fixes: one item per spelling_errors entry. error must"
        " exactly repeat the matching spelling_errors snippet. suggested_fix"
        " must be the correct spelling only, not an instruction to review."
        " explanation should briefly explain the correction.\n"
        "has_clear_structure: true if the response has logical order or useful"
        " transitions.\n"
        "has_conclusion: true if it has a closing thought.\n"
        "handwriting_legibility: choose exactly one of excellent, clear,"
        " readable, hard_to_read, illegible.\n"
        "The user message includes feedback_language as one of zh-Hans, en,"
        " ja, or ko. Write prompt_summary, strengths, improvements,"
        " grammar_error_fixes.explanation, spelling_error_fixes.explanation,"
        " writing_training_focuses.suggested_fix, and"
        " writing_training_focuses.explanation in that feedback_language.\n"
        "strengths: 1-3 short bullets.\n"
        "improvements: 1-3 actionable bullets for teacher feedback.\n"
        "writing_training_focuses: 0-3 broader writing skill needs for future"
        " personalized practice. Do not repeat any item already captured in"
        " grammar_errors or spelling_errors. Use skill_tag exactly as one of"
        " writing_structure, content_development, cohesion, or"
        " vocabulary_precision. Use writing_structure for organization,"
        " paragraphing, or conclusions; content_development for missing prompt"
        " points, weak reasons, or thin examples; cohesion for transitions and"
        " logical links; vocabulary_precision for word choice, collocation, or"
        " naturalness that is not a spelling error or single grammar-form"
        " correction. evidence should be a short quote or observation;"
        " suggested_fix should be the practice focus; explanation should briefly"
        " explain why it matters. If all improvements are only grammar or"
        " spelling repeats, return an empty list."
    ),
    output_schema=WritingEvidence,
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        seed=0,
        # Caps runaway repetition loops; far above any legitimate essay
        # transcription + model essay + fixes, which stay under ~4k tokens.
        max_output_tokens=16384,
    ),
)


grammar_training_extractor = Agent(
    name="grammar_training_extractor",
    model="gemini-flash-latest",
    instruction=(
        "You are an English grammar teacher. The user message contains a"
        " filename label followed by one grammar-training screenshot.\n\n"
        "Extract only grammar mistakes or wrong answers that should become"
        " future personalized practice. Do not include correct answers with no"
        " error.\n\n"
        "student_name: the student's name if visible; otherwise \"unknown\".\n"
        "submission_date: the date visibly written or printed on the image,"
        " normalized as YYYY-MM-DD when possible; otherwise an empty string.\n"
        "mistakes: one item per distinct grammar error or wrong answer.\n"
        "skill_tag: short lowercase English label, such as tense,"
        " subject_verb_agreement, article, plural, preposition, word_order, or"
        " punctuation.\n"
        "original_answer: the student's wrong answer or mistaken text exactly"
        " as visible.\n"
        "correct_answer: the corrected answer.\n"
        "explanation: concise teacher explanation. The user message includes"
        " feedback_language as zh-Hans, en, ja, or ko; write explanations in"
        " that language."
    ),
    output_schema=GrammarTrainingEvidence,
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        seed=0,
        max_output_tokens=8192,
    ),
)


def _writing_input_scan_dirs() -> list[Path]:
  return [WRITING_INPUTS_DIR, WRITING_INPUTS_DIR / "tem"]


def _uploaded_inputs_dir() -> Path:
  return WRITING_INPUTS_DIR / "uploads"


def _blob_bytes(data: object) -> bytes:
  if data is None:
    return b""
  if isinstance(data, bytes):
    return data
  if isinstance(data, bytearray):
    return bytes(data)
  if isinstance(data, str):
    return base64.b64decode(data)
  return bytes(data)


def _mime_for_inline_image(blob: types.Blob) -> str | None:
  mime = (blob.mime_type or "").lower()
  if mime in SUFFIX_BY_MIME:
    return mime
  suffix = Path(blob.display_name or "").suffix.lower()
  return MIME_BY_SUFFIX.get(suffix)


def _safe_upload_filename(
    *,
    display_name: str | None,
    index: int,
    mime: str,
) -> str:
  suffix = SUFFIX_BY_MIME.get(mime, ".png")
  raw_name = Path(display_name or "").name.strip()
  if not raw_name:
    return f"uploaded_{index}{suffix}"

  raw_name = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', "_", raw_name)
  raw_path = Path(raw_name)
  raw_suffix = raw_path.suffix.lower()
  if raw_suffix in MIME_BY_SUFFIX:
    suffix = raw_suffix

  stem = raw_path.stem.strip(" ._-")
  stem = re.sub(r"\s+", "_", stem)
  stem = stem or f"uploaded_{index}"
  return f"{stem}{suffix}"


def _unique_upload_path(directory: Path, filename: str) -> Path:
  candidate = directory / filename
  if not candidate.exists():
    return candidate

  stem = Path(filename).stem
  suffix = Path(filename).suffix
  index = 2
  while True:
    candidate = directory / f"{stem}_{index}{suffix}"
    if not candidate.exists():
      return candidate
    index += 1


def _uploaded_inline_image_inputs(
    node_input: object,
    feedback_language: FeedbackLanguage,
) -> list[dict[str, str]]:
  if not isinstance(node_input, types.Content):
    return []

  items: list[dict[str, str]] = []
  for index, part in enumerate(node_input.parts or [], start=1):
    blob = part.inline_data
    if blob is None:
      continue
    mime = _mime_for_inline_image(blob)
    if mime is None:
      continue
    data = _blob_bytes(blob.data)
    if not data:
      continue

    upload_dir = _uploaded_inputs_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_upload_filename(
        display_name=blob.display_name,
        index=index,
        mime=mime,
    )
    path = _unique_upload_path(upload_dir, filename)
    path.write_bytes(data)
    items.append({
        "path": str(path),
        "filename": path.name,
        "mime": mime,
        "feedback_language": feedback_language,
    })
  return items


def list_writing_inputs(node_input: object) -> list[dict[str, str]]:
  """Read uploaded ADK Web images, or scan ./input/ and ./input/tem/."""
  WRITING_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
  feedback_language = _feedback_language_from_input(node_input)
  uploaded_items = _uploaded_inline_image_inputs(node_input, feedback_language)
  if uploaded_items:
    return uploaded_items

  items: list[dict[str, str]] = []
  for directory in _writing_input_scan_dirs():
    if not directory.is_dir():
      continue
    for path in sorted(directory.iterdir()):
      if not path.is_file():
        continue
      mime = MIME_BY_SUFFIX.get(path.suffix.lower())
      if mime is None:
        continue
      items.append({
          "path": str(path),
          "filename": path.name,
          "mime": mime,
          "feedback_language": feedback_language,
      })
  return items


def pick_input_route(image_category: ImageCategory):
  """Route key copied from adk-kit's router_intent pick(...) shape."""
  yield Event(route=image_category.category)


def _model_from_output(value: object, model_type: type[ModelT]) -> ModelT:
  if isinstance(value, model_type):
    return model_type.model_validate(value.model_dump())
  if isinstance(value, types.Content):
    return model_type.model_validate_json(_content_text(value))
  if isinstance(value, dict):
    return model_type.model_validate(value)
  if isinstance(value, str):
    return model_type.model_validate_json(value)
  return model_type.model_validate(value)


def _calculate_overall_score(dimensions: DimensionScores) -> float:
  total = (
      dimensions.content
      + dimensions.structure
      + dimensions.language
      + dimensions.handwriting
  )
  return round(total, 1)


def _score_from_evidence(evidence: WritingEvidence) -> DimensionScores:
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

  structure = min(
      5,
      3 + int(evidence.has_clear_structure) + int(evidence.has_conclusion),
  )

  language_error_count = (
      len(evidence.grammar_errors) + len(evidence.spelling_errors)
  )
  language = max(0.0, 5 - language_error_count * 0.5)

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


def _safe_name(name: str) -> str:
  """Make a student name safe for use as a filename segment."""
  s = (name or "").strip().replace(" ", "_")
  s = re.sub(r'[/\\:*?"<>|]+', "", s)
  return s or "unknown"


def _known_name(value: str | None) -> bool:
  return bool(value and value.strip() and value.strip().lower() != "unknown")


def _student_hint_from_filename(filename: str) -> str:
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


def _resolve_student_name(
    *,
    filename: str,
    category_name: str,
    evidence_name: str,
) -> str:
  if _known_name(evidence_name):
    return evidence_name.strip()
  if _known_name(category_name):
    return category_name.strip()
  return _student_hint_from_filename(filename)


def _normalize_submission_date(value: str | None) -> str:
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


def _default_submission_date() -> str:
  return datetime.datetime.now().astimezone().date().isoformat()


def _resolve_submission_date(
    *,
    evidence_date: str | None,
    category_date: str | None,
) -> str:
  return (
      _normalize_submission_date(evidence_date)
      or _normalize_submission_date(category_date)
      or _default_submission_date()
  )


def _safe_input_student_segment(name: str) -> str:
  segment = _safe_name(name)
  segment = re.sub(r"^[._-]+|[._-]+$", "", segment)
  return segment or "unknown"


def _canonical_input_stem(student_name: str, submission_date: str) -> str:
  return f"{_safe_input_student_segment(student_name)}_{submission_date}"


def _unique_input_path(
    *,
    source_path: Path,
    target_stem: str,
) -> Path:
  target_dir = source_path.parent
  suffix = source_path.suffix
  candidate = target_dir / f"{target_stem}{suffix}"
  if candidate == source_path:
    return source_path

  index = 2
  while candidate.exists():
    if candidate == source_path:
      return source_path
    candidate = target_dir / f"{target_stem}_{index}{suffix}"
    index += 1
  return candidate


def _image_content(
    *,
    path: str,
    filename: str,
    mime: str,
    feedback_language: FeedbackLanguage,
    task: str,
) -> types.Content:
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


def _issue_key(text: str) -> str:
  cleaned = text.strip()
  cleaned = cleaned.removeprefix("Review and correct:").strip()
  cleaned = cleaned.strip("\"'“”")
  return re.sub(r"\s+", " ", cleaned).casefold()


def _is_same_issue_text(left: str, right: str) -> bool:
  return _issue_key(left) == _issue_key(right)


def _writing_issue_fix(
    *,
    error: str,
    index: int,
    fixes: list[WritingIssueFix],
    fallback_suggested_fix: str,
    fallback_explanation: str,
) -> tuple[str, str]:
  error_key = _issue_key(error)
  candidate: WritingIssueFix | None = None
  for fix in fixes:
    if _issue_key(fix.error) == error_key:
      candidate = fix
      break
  if candidate is None and index < len(fixes):
    candidate = fixes[index]

  if candidate is None:
    return fallback_suggested_fix, fallback_explanation

  suggested_fix = candidate.suggested_fix.strip()
  explanation = candidate.explanation.strip() or fallback_explanation
  if not suggested_fix or _is_same_issue_text(error, suggested_fix):
    return fallback_suggested_fix, fallback_explanation
  return suggested_fix, explanation


def _writing_learning_needs(
    *,
    filename: str,
    student_name: str,
    evidence: WritingEvidence,
) -> list[LearningNeed]:
  needs: list[LearningNeed] = []
  for index, error in enumerate(evidence.grammar_errors):
    suggested_fix, explanation = _writing_issue_fix(
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
  for index, error in enumerate(evidence.spelling_errors):
    suggested_fix, explanation = _writing_issue_fix(
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


def _grammar_learning_needs(
    *,
    filename: str,
    student_name: str,
    evidence: GrammarTrainingEvidence,
) -> list[LearningNeed]:
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


_PROCESS_INPUT_MAX_ATTEMPTS = 3


class InputProcessingError(RuntimeError):
  """One input image failed classification or extraction."""


def _short_failure_reason(exc: BaseException) -> str:
  inner = getattr(exc, "error", None)
  if isinstance(inner, BaseException):
    exc = inner
  reason = " ".join(f"{type(exc).__name__}: {exc}".split())
  if len(reason) > 300:
    return f"{reason[:300]}..."
  return reason


def _task_for_attempt(task: str, attempt_count: int) -> str:
  """Vary the prompt on retries: temperature=0 + seed=0 makes generation
  deterministic, so retrying an identical request replays the exact same
  failure (e.g. a repetition loop that truncates the JSON output)."""
  if attempt_count <= 1:
    return task
  return (
      f"{task}\n"
      f"Retry attempt {attempt_count}: the previous attempt produced invalid"
      " output. Respond with exactly one complete, valid JSON object, keep"
      " every field concise, and never repeat the same sentence or phrase."
  )


async def _process_one_input_result(
    ctx: Context,
    *,
    path: str,
    filename: str,
    mime: str,
    feedback_language: FeedbackLanguage,
) -> InputProcessingResult:
  """Classify one image, run the matching extractor, and build the result."""
  category_raw = await ctx.run_node(
      classify_input_image,
      node_input=_image_content(
          path=path,
          filename=filename,
          mime=mime,
          feedback_language=feedback_language,
          task=_task_for_attempt(
              "Classify this image before any grading or extraction.",
              ctx.attempt_count,
          ),
      ),
      use_sub_branch=True,
  )
  category = _model_from_output(category_raw, ImageCategory)
  route_event = next(pick_input_route(category))
  route = route_event.actions.route

  if route == "writing":
    evidence_raw = await ctx.run_node(
        extractor,
        node_input=_image_content(
            path=path,
            filename=filename,
            mime=mime,
            feedback_language=feedback_language,
            task=_task_for_attempt(
                "Grade the writing submission by extracting evidence only.",
                ctx.attempt_count,
            ),
        ),
        use_sub_branch=True,
    )
    evidence = _model_from_output(evidence_raw, WritingEvidence)
    student_name = _resolve_student_name(
        filename=filename,
        category_name=category.student_name,
        evidence_name=evidence.student_name,
    )
    submission_date = _resolve_submission_date(
        evidence_date=evidence.submission_date,
        category_date=category.submission_date,
    )
    evidence = evidence.model_copy(
        update={"student_name": student_name, "submission_date": submission_date}
    )
    dimensions = _score_from_evidence(evidence)
    feedback = EnglishCoachFeedback(
        filename=filename,
        student_name=student_name,
        feedback_language=feedback_language,
        prompt_summary=evidence.prompt_summary,
        transcription=evidence.transcription,
        model_essay=evidence.model_essay,
        overall_score=_calculate_overall_score(dimensions),
        dimensions=dimensions,
        strengths=evidence.strengths,
        improvements=evidence.improvements,
    )
    return InputProcessingResult(
        filename=filename,
        source_path=path,
        category="writing",
        student_name=student_name,
        submission_date=submission_date,
        feedback_language=feedback_language,
        feedback=feedback,
        learning_needs=_writing_learning_needs(
            filename=filename,
            student_name=student_name,
            evidence=evidence,
        ),
    )

  if route == "grammar_training":
    grammar_raw = await ctx.run_node(
        grammar_training_extractor,
        node_input=_image_content(
            path=path,
            filename=filename,
            mime=mime,
            feedback_language=feedback_language,
            task=_task_for_attempt(
                "Extract grammar-training mistakes from this image.",
                ctx.attempt_count,
            ),
        ),
        use_sub_branch=True,
    )
    evidence = _model_from_output(grammar_raw, GrammarTrainingEvidence)
    student_name = _resolve_student_name(
        filename=filename,
        category_name=category.student_name,
        evidence_name=evidence.student_name,
    )
    submission_date = _resolve_submission_date(
        evidence_date=evidence.submission_date,
        category_date=category.submission_date,
    )
    evidence = evidence.model_copy(
        update={"student_name": student_name, "submission_date": submission_date}
    )
    return InputProcessingResult(
        filename=filename,
        source_path=path,
        category="grammar_training",
        student_name=student_name,
        submission_date=submission_date,
        feedback_language=feedback_language,
        grammar_training=evidence,
        learning_needs=_grammar_learning_needs(
            filename=filename,
            student_name=student_name,
            evidence=evidence,
        ),
    )

  reason = category.reason or "Image was not recognized as writing or grammar training."
  return InputProcessingResult(
      filename=filename,
      source_path=path,
      category="unsupported",
      student_name=_resolve_student_name(
          filename=filename,
          category_name=category.student_name,
          evidence_name="unknown",
      ),
      submission_date=_resolve_submission_date(
          evidence_date="",
          category_date=category.submission_date,
      ),
      feedback_language=feedback_language,
      skipped_reason=reason,
  )


@node(
    retry_config=RetryConfig(
        max_attempts=_PROCESS_INPUT_MAX_ATTEMPTS,
        initial_delay=2,
    ),
    rerun_on_resume=True,
)
async def process_one_input(ctx: Context, node_input: dict[str, str]):
  """Classify one input image and run the matching specialist extractor.

  ADK never retries DynamicNodeFailError raised from ctx.run_node
  sub-nodes, so their failures would bypass this node's retry_config and
  kill the whole workflow run. Sub-node failures are therefore caught
  here and re-raised as InputProcessingError to engage this node's own
  retries; once the retry budget is spent, the image degrades to a
  skipped result instead of failing the run.
  """
  path = node_input["path"]
  filename = node_input["filename"]
  mime = node_input["mime"]
  feedback_language = _feedback_language_from_input(
      node_input.get("feedback_language", DEFAULT_FEEDBACK_LANGUAGE)
  )
  yield Event(message=f"Processing {filename} (attempt {ctx.attempt_count})...")

  try:
    result = await _process_one_input_result(
        ctx,
        path=path,
        filename=filename,
        mime=mime,
        feedback_language=feedback_language,
    )
  except Exception as exc:
    reason = _short_failure_reason(exc)
    if ctx.attempt_count < _PROCESS_INPUT_MAX_ATTEMPTS:
      raise InputProcessingError(
          f"{filename} failed on attempt {ctx.attempt_count}: {reason}"
      ) from exc
    yield Event(
        message=(
            f"Skipping {filename} after {ctx.attempt_count} failed"
            f" attempt(s): {reason}"
        )
    )
    result = InputProcessingResult(
        filename=filename,
        source_path=path,
        category="unsupported",
        student_name=_student_hint_from_filename(filename),
        submission_date=_default_submission_date(),
        feedback_language=feedback_language,
        skipped_reason=(
            f"Processing failed after {ctx.attempt_count} attempt(s): {reason}"
        ),
    )
  yield Event(output=result)


@node(rerun_on_resume=True)
async def orchestrate(ctx: Context, node_input: list[dict[str, str]]):
  """Fan out one process_one_input sub-node per supported screenshot."""
  inputs = node_input
  if not inputs:
    supported = ", ".join(sorted(MIME_BY_SUFFIX))
    yield Event(
        message=(
            "No supported uploaded images or input files found. "
            f"Checked ADK Web message attachments and {WRITING_INPUTS_DIR}: "
            f"{supported}."
        )
    )
    yield Event(output=[])
    return

  yield Event(message=f"Dispatching {len(inputs)} coach task(s)...")
  tasks = [
      ctx.run_node(process_one_input, node_input=item, use_sub_branch=True)
      for item in inputs
  ]
  results = await asyncio.gather(*tasks)
  yield Event(output=results)


def _coerce_results(
    node_input: list[InputProcessingResult] | list[dict[str, object]],
) -> list[InputProcessingResult]:
  return [InputProcessingResult.model_validate(item) for item in node_input]


def _result_with_filename(
    *,
    result: InputProcessingResult,
    filename: str,
    source_path: str,
) -> InputProcessingResult:
  feedback = result.feedback
  if feedback is not None:
    feedback = feedback.model_copy(update={"filename": filename})

  learning_needs = [
      need.model_copy(update={"filename": filename})
      for need in result.learning_needs
  ]

  return result.model_copy(
      update={
          "filename": filename,
          "source_path": source_path,
          "feedback": feedback,
          "learning_needs": learning_needs,
      }
  )


def _rename_processed_input(
    result: InputProcessingResult,
) -> tuple[InputProcessingResult, str | None]:
  if not _known_name(result.student_name):
    return result, None

  student_segment = _safe_input_student_segment(result.student_name)
  if student_segment == "unknown":
    return result, None

  source_path = Path(result.source_path) if result.source_path else None
  if source_path is None or not source_path.is_file():
    return result, None

  target_stem = _canonical_input_stem(
      student_name=result.student_name,
      submission_date=(
          _normalize_submission_date(result.submission_date)
          or _default_submission_date()
      ),
  )
  target_path = _unique_input_path(
      source_path=source_path,
      target_stem=target_stem,
  )

  if target_path != source_path:
    source_path.rename(target_path)
    message = f"{source_path.name} -> {target_path.name}"
  else:
    message = None

  return _result_with_filename(
      result=result,
      filename=target_path.name,
      source_path=str(target_path),
  ), message


def rename_processed_inputs(
    node_input: list[InputProcessingResult] | list[dict[str, object]],
):
  """Serially rename source images after parallel extraction has finished."""
  results = _coerce_results(node_input)
  renamed: list[str] = []
  updated_results: list[InputProcessingResult] = []
  for result in results:
    updated, message = _rename_processed_input(result)
    updated_results.append(updated)
    if message:
      renamed.append(message)

  if renamed:
    yield Event(
        message="Renamed input file(s):\n" + "\n".join(
            f"- {item}" for item in renamed
        )
    )
  yield Event(output=updated_results)


def build_student_profiles(
    node_input: list[InputProcessingResult] | list[dict[str, object]],
) -> list[StudentLearningProfile]:
  results = _coerce_results(node_input)
  known_students = sorted({
      result.student_name
      for result in results
      if _known_name(result.student_name)
  })

  profiles: dict[str, StudentLearningProfile] = {}
  for result in results:
    student_name = result.student_name
    if not _known_name(student_name):
      if len(known_students) == 1:
        student_name = known_students[0]
      else:
        hint = _student_hint_from_filename(result.filename)
        student_name = hint if _known_name(hint) else "unknown"

    profile = profiles.setdefault(
        student_name,
        StudentLearningProfile(
            student_name=student_name,
            feedback_language=result.feedback_language,
        ),
    )

    if result.feedback:
      profile.feedback_items.append(
          result.feedback.model_copy(update={"student_name": student_name})
      )
    if result.grammar_training:
      profile.grammar_trainings.append(
          result.grammar_training.model_copy(update={"student_name": student_name})
      )
    if result.skipped_reason:
      profile.skipped.append(f"{result.filename}: {result.skipped_reason}")

    for need in result.learning_needs:
      profile.learning_needs.append(need.model_copy(update={"student_name": student_name}))

  return list(profiles.values())


def _yaml_string(value: str) -> str:
  return json.dumps(value, ensure_ascii=False)


def _markdown_cell(value: object) -> str:
  return str(value).replace("\n", "<br>").replace("|", "\\|")


def write_report(
    node_input: list[StudentLearningProfile] | list[dict[str, object]],
):
  profiles = [
      StudentLearningProfile.model_validate(profile) for profile in node_input
  ]
  if not profiles:
    yield Event(message="Nothing processed; no report written.")
    return

  now = datetime.datetime.now().astimezone()
  file_ts = now.strftime("%Y-%m-%d_%H-%M-%S")
  display_ts = now.strftime("%Y-%m-%d %H:%M:%S")
  student_reports_dir = report_layout.student_markdown_dir(REPORTS_DIR)
  session_reports_dir = report_layout.session_markdown_dir(REPORTS_DIR)
  pdf_reports_dir = report_layout.pdf_dir(REPORTS_DIR)
  student_reports_dir.mkdir(parents=True, exist_ok=True)
  session_reports_dir.mkdir(parents=True, exist_ok=True)
  TRAINING_INPUTS_DIR.mkdir(parents=True, exist_ok=True)

  written: list[Path] = []
  pdf_links: list[tuple[Path, str]] = []
  pdf_warnings: list[str] = []
  wps_warnings: list[str] = []
  db_warnings: list[str] = []
  session_sections: list[list[str]] = []
  for profile in profiles:
    student = profile.student_name or "unknown"
    safe_student = _safe_name(student)
    report_path = student_reports_dir / f"{safe_student}_{file_ts}.md"
    training_path = TRAINING_INPUTS_DIR / f"{safe_student}_{file_ts}.json"
    training_path.write_text(
        json.dumps(
            profile.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    frontmatter_lines: list[str] = [
        "---",
        "schema_version: 2",
        f"report_type: {_yaml_string('student_learning_profile')}",
        f"student: {_yaml_string(student)}",
        f"feedback_language: {_yaml_string(profile.feedback_language)}",
        f"generated_at: {_yaml_string(display_ts)}",
        f"submission_count: {len(profile.feedback_items)}",
        f"grammar_training_count: {len(profile.grammar_trainings)}",
        f"learning_need_count: {len(profile.learning_needs)}",
        f"training_input_json: {_yaml_string(str(training_path))}",
        "---",
    ]

    lines: list[str] = [
        "",
        "# Student Learning Profile",
        "",
        "## Report Info",
        "| Field | Value |",
        "| --- | --- |",
        f"| Student | {_markdown_cell(student)} |",
        f"| Feedback Language | {_markdown_cell(profile.feedback_language)} |",
        f"| Feedback At | {_markdown_cell(display_ts)} |",
        f"| Writing Submissions | {len(profile.feedback_items)} |",
        f"| Grammar Trainings | {len(profile.grammar_trainings)} |",
        f"| Learning Needs | {len(profile.learning_needs)} |",
        f"| Training JSON | {_markdown_cell(training_path)} |",
        "",
    ]

    if profile.feedback_items:
      lines.extend([
          "## Score Summary",
          "| Submission | Overall | Content | Structure | Language | Handwriting |",
          "| --- | ---: | ---: | ---: | ---: | ---: |",
      ])
      for feedback in profile.feedback_items:
        d = feedback.dimensions
        lines.append(
            f"| {_markdown_cell(feedback.filename)} | {feedback.overall_score:.1f}/20 |"
            f" {d.content}/5 | {d.structure}/5 | {d.language:.1f}/5 |"
            f" {d.handwriting}/5 |"
        )
      lines.append("")
      lines.append("## Submission Details")
      for index, feedback in enumerate(profile.feedback_items, start=1):
        d = feedback.dimensions
        lines.extend([
            "",
            f"### {index}. {feedback.filename}",
            "",
            "#### Score Breakdown",
            "| Overall | Content | Structure | Language | Handwriting |",
            "| ---: | ---: | ---: | ---: | ---: |",
            (
                f"| {feedback.overall_score:.1f}/20 | {d.content}/5 |"
                f" {d.structure}/5 | {d.language:.1f}/5 | {d.handwriting}/5 |"
            ),
            "",
            "#### 题目",
            feedback.prompt_summary,
            "",
            "#### 优点",
        ])
        for strength in feedback.strengths:
          lines.append(f"- {strength}")
        lines.append("")
        lines.append("#### 改进建议")
        for improvement in feedback.improvements:
          lines.append(f"- {improvement}")
        lines.extend([
            "",
            "#### 原文",
            "```text",
        ])
        lines.extend(feedback.transcription.splitlines() or [feedback.transcription])
        lines.append("```")
        model_essay = feedback.model_essay.strip()
        if model_essay:
          lines.extend([
              "",
              "#### 范文",
              "```text",
          ])
          lines.extend(model_essay.splitlines() or [model_essay])
          lines.append("```")
        lines.append("")

    lines.extend([
        "## Grammar Training Mistakes",
        "| Skill | Original | Correct | Explanation |",
        "| --- | --- | --- | --- |",
    ])
    grammar_rows = 0
    for training in profile.grammar_trainings:
      for mistake in training.mistakes:
        grammar_rows += 1
        lines.append(
            f"| {_markdown_cell(mistake.skill_tag)} |"
            f" {_markdown_cell(mistake.original_answer)} |"
            f" {_markdown_cell(mistake.correct_answer)} |"
            f" {_markdown_cell(mistake.explanation)} |"
        )
    if grammar_rows == 0:
      lines.append("| - | - | - | - |")
    lines.append("")

    lines.extend([
        "## Personalized Training Input",
        "| Source | Skill | Evidence | Suggested Fix | Explanation |",
        "| --- | --- | --- | --- | --- |",
    ])
    for need in profile.learning_needs:
      lines.append(
          f"| {_markdown_cell(need.source_type)} |"
          f" {_markdown_cell(need.skill_tag)} |"
          f" {_markdown_cell(need.evidence)} |"
          f" {_markdown_cell(need.suggested_fix)} |"
          f" {_markdown_cell(need.explanation)} |"
      )
    if not profile.learning_needs:
      lines.append("| - | - | - | - | - |")

    if profile.skipped:
      lines.extend(["", "## Skipped Images"])
      for skipped in profile.skipped:
        lines.append(f"- {skipped}")

    report_path.write_text(
        "\n".join(frontmatter_lines + lines) + "\n", encoding="utf-8"
    )
    written.extend([report_path, training_path])
    session_sections.append(lines)
    try:
      pdf_path = export_report_pdf(report_path, output_dir=pdf_reports_dir)
    except PdfExportError as exc:
      pdf_warnings.append(f"{report_path.name}: {exc}")
    else:
      written.append(pdf_path)
      pdf_links.append((pdf_path, f"/reports/{quote(pdf_path.name)}"))

    try:
      history_store.record_student_profile(
          profile.model_dump(mode="json"),
          report_path=report_path,
          training_path=training_path,
          recorded_at=now,
      )
    except Exception as exc:  # noqa: BLE001 - persistence must never abort report writing
      db_warnings.append(f"{report_path.name}: {exc}")

  if session_sections:
    session_body: list[str] = []
    for index, section in enumerate(session_sections):
      if index:
        session_body.append('<div style="page-break-before: always;"></div>')
      session_body.extend(section)
    session_lines = [
        "---",
        "schema_version: 2",
        f"report_type: {_yaml_string('session_learning_profiles')}",
        f"generated_at: {_yaml_string(display_ts)}",
        f"student_count: {len(session_sections)}",
        "---",
        *session_body,
    ]
    session_path = session_reports_dir / f"session_{file_ts}.md"
    session_path.write_text("\n".join(session_lines) + "\n", encoding="utf-8")
    written.append(session_path)
    try:
      session_pdf = export_report_pdf(
          session_path, output_dir=pdf_reports_dir
      )
    except PdfExportError as exc:
      pdf_warnings.append(f"{session_path.name}: {exc}")
    else:
      written.append(session_pdf)
      pdf_links.append((session_pdf, f"/reports/{quote(session_pdf.name)}"))
      try:
        open_pdf_in_wps(session_pdf)
      except PdfOpenError as exc:
        wps_warnings.append(f"{session_pdf.name}: {exc}")

  summary = f"Wrote {len(written)} file(s):\n" + "\n".join(
      f"- {path}" for path in written
  )
  if pdf_links:
    summary += "\n\nPDF report links:\n" + "\n".join(
        f"- {path.name}\n"
        f"  View / print: {url}\n"
        f"  Download: {url}?download=1"
        for path, url in pdf_links
    )
  if pdf_warnings:
    summary += "\n\nPDF export warning(s):\n" + "\n".join(
        f"- {warning}" for warning in pdf_warnings
    )
  if wps_warnings:
    summary += "\n\nWPS open warning(s):\n" + "\n".join(
        f"- {warning}" for warning in wps_warnings
    )
  if db_warnings:
    summary += "\n\nHistory DB warning(s):\n" + "\n".join(
        f"- {warning}" for warning in db_warnings
    )
  yield Event(message=summary)


root_agent = Workflow(
    name="root_agent",
    edges=[
        (
            "START",
            list_writing_inputs,
            orchestrate,
            rename_processed_inputs,
            build_student_profiles,
            write_report,
        ),
    ],
)
