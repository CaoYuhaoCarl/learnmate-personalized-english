"""
Workflow: produce English coaching feedback and training inputs from screenshots.

Reads ./input/*.{jpg,jpeg,png,webp,heic,heif}, classifies each image, sends it
to the matching Gemini structured extractor, merges all outputs into per-student
learning profiles, and writes both Markdown reports and JSON training inputs.

Composition:
    list_writing_inputs -> orchestrate -> build_student_profiles -> write_report
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
import datetime
import json
import re
from pathlib import Path
from typing import Literal
from typing import TypeVar

from google.adk import Agent
from google.adk import Context
from google.adk import Event
from google.adk import Workflow
from google.adk.workflow import RetryConfig
from google.adk.workflow import node
from google.genai import types
from pydantic import BaseModel
from pydantic import Field

WRITING_INPUTS_DIR = Path(__file__).parent / "input"
REPORTS_DIR = Path(__file__).parent / "reports"
TRAINING_INPUTS_DIR = Path(__file__).parent / "training_inputs"
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

FeedbackLanguage = Literal["zh-Hans", "en", "ja", "ko"]
InputRoute = Literal["writing", "grammar_training", "unsupported"]
LearningSource = Literal["writing", "grammar_training"]
DEFAULT_FEEDBACK_LANGUAGE: FeedbackLanguage = "zh-Hans"
ModelT = TypeVar("ModelT", bound=BaseModel)


def _has_ascii_alias(text: str, aliases: tuple[str, ...]) -> bool:
  for alias in aliases:
    pattern = rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)"
    if re.search(pattern, text):
      return True
  return False


def _feedback_language_from_input(node_input: object) -> FeedbackLanguage:
  text = str(node_input or "").lower()
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


class WritingEvidence(BaseModel):
  student_name: str
  prompt_summary: str
  transcription: str
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


class EnglishCoachFeedback(BaseModel):
  filename: str
  student_name: str
  feedback_language: FeedbackLanguage = DEFAULT_FEEDBACK_LANGUAGE
  prompt_summary: str
  transcription: str
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
  category: InputRoute
  student_name: str
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
        " \"unknown\". confidence: 0 to 1. reason: one short explanation."
    ),
    output_schema=ImageCategory,
    output_key="image_category",
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        seed=0,
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
        "prompt_summary: one sentence describing what the writing task was meant"
        " to address.\n"
        "transcription: the student's handwritten response transcribed"
        " verbatim. Preserve their original words, line breaks, spelling, and"
        " grammar; do not silently correct mistakes. Use \\n for line breaks."
        " Do not include the printed prompt.\n"
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
        " grammar_error_fixes.explanation, and spelling_error_fixes.explanation"
        " in that feedback_language.\n"
        "strengths: 1-3 short bullets.\n"
        "improvements: 1-3 actionable bullets."
    ),
    output_schema=WritingEvidence,
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        seed=0,
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
    ),
)


def list_writing_inputs(node_input: str) -> list[dict[str, str]]:
  """Scan ./input/ for supported image files."""
  WRITING_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
  feedback_language = _feedback_language_from_input(node_input)
  items: list[dict[str, str]] = []
  for path in sorted(WRITING_INPUTS_DIR.iterdir()):
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


def _content_text(value: types.Content) -> str:
  return "".join(
      part.text
      for part in value.parts or []
      if part.text and not getattr(part, "thought", False)
  )


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
  for improvement in evidence.improvements:
    needs.append(
        LearningNeed(
            student_name=student_name,
            source_type="writing",
            filename=filename,
            skill_tag="writing_improvement",
            evidence="Broader improvement area from teacher feedback.",
            suggested_fix=improvement,
            explanation="Actionable writing improvement from teacher feedback.",
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


@node(
    retry_config=RetryConfig(max_attempts=3, initial_delay=2),
    rerun_on_resume=True,
)
async def process_one_input(ctx: Context, node_input: dict[str, str]):
  """Classify one input image and run the matching specialist extractor."""
  path = node_input["path"]
  filename = node_input["filename"]
  mime = node_input["mime"]
  feedback_language = _feedback_language_from_input(
      node_input.get("feedback_language", DEFAULT_FEEDBACK_LANGUAGE)
  )
  yield Event(message=f"Processing {filename} (attempt {ctx.attempt_count})...")

  category_raw = await ctx.run_node(
      classify_input_image,
      node_input=_image_content(
          path=path,
          filename=filename,
          mime=mime,
          feedback_language=feedback_language,
          task="Classify this image before any grading or extraction.",
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
            task="Grade the writing submission by extracting evidence only.",
        ),
        use_sub_branch=True,
    )
    evidence = _model_from_output(evidence_raw, WritingEvidence)
    student_name = _resolve_student_name(
        filename=filename,
        category_name=category.student_name,
        evidence_name=evidence.student_name,
    )
    evidence = evidence.model_copy(update={"student_name": student_name})
    dimensions = _score_from_evidence(evidence)
    feedback = EnglishCoachFeedback(
        filename=filename,
        student_name=student_name,
        feedback_language=feedback_language,
        prompt_summary=evidence.prompt_summary,
        transcription=evidence.transcription,
        overall_score=_calculate_overall_score(dimensions),
        dimensions=dimensions,
        strengths=evidence.strengths,
        improvements=evidence.improvements,
    )
    yield Event(
        output=InputProcessingResult(
            filename=filename,
            category="writing",
            student_name=student_name,
            feedback_language=feedback_language,
            feedback=feedback,
            learning_needs=_writing_learning_needs(
                filename=filename,
                student_name=student_name,
                evidence=evidence,
            ),
        )
    )
    return

  if route == "grammar_training":
    grammar_raw = await ctx.run_node(
        grammar_training_extractor,
        node_input=_image_content(
            path=path,
            filename=filename,
            mime=mime,
            feedback_language=feedback_language,
            task="Extract grammar-training mistakes from this image.",
        ),
        use_sub_branch=True,
    )
    evidence = _model_from_output(grammar_raw, GrammarTrainingEvidence)
    student_name = _resolve_student_name(
        filename=filename,
        category_name=category.student_name,
        evidence_name=evidence.student_name,
    )
    evidence = evidence.model_copy(update={"student_name": student_name})
    yield Event(
        output=InputProcessingResult(
            filename=filename,
            category="grammar_training",
            student_name=student_name,
            feedback_language=feedback_language,
            grammar_training=evidence,
            learning_needs=_grammar_learning_needs(
                filename=filename,
                student_name=student_name,
                evidence=evidence,
            ),
        )
    )
    return

  reason = category.reason or "Image was not recognized as writing or grammar training."
  yield Event(
      output=InputProcessingResult(
          filename=filename,
          category="unsupported",
          student_name=_resolve_student_name(
              filename=filename,
              category_name=category.student_name,
              evidence_name="unknown",
          ),
          feedback_language=feedback_language,
          skipped_reason=reason,
      )
  )


@node(rerun_on_resume=True)
async def orchestrate(ctx: Context, node_input: list[dict[str, str]]):
  """Fan out one process_one_input sub-node per supported screenshot."""
  inputs = node_input
  if not inputs:
    supported = ", ".join(sorted(MIME_BY_SUFFIX))
    yield Event(
        message=f"No supported image files found in {WRITING_INPUTS_DIR}: {supported}."
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
  REPORTS_DIR.mkdir(parents=True, exist_ok=True)
  TRAINING_INPUTS_DIR.mkdir(parents=True, exist_ok=True)

  written: list[Path] = []
  for profile in profiles:
    student = profile.student_name or "unknown"
    safe_student = _safe_name(student)
    report_path = REPORTS_DIR / f"{safe_student}_{file_ts}.md"
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

    lines: list[str] = [
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
        "",
        "# Student Learning Profile",
        "",
        "## Report Info",
        "| Field | Value |",
        "| --- | --- |",
        f"| Student | {_markdown_cell(student)} |",
        f"| Feedback Language | {_markdown_cell(profile.feedback_language)} |",
        f"| Generated At | {_markdown_cell(display_ts)} |",
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
            "#### Prompt",
            feedback.prompt_summary,
            "",
            "#### Strengths",
        ])
        for strength in feedback.strengths:
          lines.append(f"- {strength}")
        lines.append("")
        lines.append("#### Improvements")
        for improvement in feedback.improvements:
          lines.append(f"- {improvement}")
        lines.extend([
            "",
            "#### Transcription",
            "```text",
        ])
        lines.extend(feedback.transcription.splitlines() or [feedback.transcription])
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

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written.extend([report_path, training_path])

  summary = f"Wrote {len(written)} file(s):\n" + "\n".join(
      f"- {path}" for path in written
  )
  yield Event(message=summary)


root_agent = Workflow(
    name="root_agent",
    edges=[
        ("START", list_writing_inputs, orchestrate, build_student_profiles, write_report),
    ],
)
