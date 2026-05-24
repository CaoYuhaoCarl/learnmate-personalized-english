from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import unquote, urlparse

from google.adk import Agent, Context, Workflow
from google.adk.events import RequestInput
from google.adk.workflow import node
from google.genai import types
from pydantic import AliasChoices, BaseModel, ConfigDict, Field


ModelT = TypeVar("ModelT", bound=BaseModel)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_id() -> str:
    return f"correction_{uuid.uuid4().hex}"


class CompositionCorrectionInput(BaseModel):
    image_path: str = Field(description="Path or upload reference for the handwriting image")
    record_id: str = Field(default_factory=_record_id)


ImageCorrectionInput = CompositionCorrectionInput


class OcrExtractionResult(BaseModel):
    image_path: str
    raw_text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    provider: str = "sidecar_stub"
    notes: list[str] = Field(default_factory=list)


class OcrQualityReviewResult(BaseModel):
    raw_text: str
    suggested_text: str
    quality_notes: list[str] = Field(default_factory=list)
    needs_human_review: bool = True


class HumanOcrReview(BaseModel):
    approved: bool = True
    reviewed_text: str
    reviewer_notes: str = "Approved without edits."


class CorrectionIssue(BaseModel):
    original: str
    suggestion: str
    explanation: str


class VocabularyImprovement(BaseModel):
    original: str
    suggestion: str
    explanation: str


class SentenceSuggestion(BaseModel):
    original: str
    suggestion: str
    explanation: str


class GrammarCorrectionInput(BaseModel):
    text: str


class GrammarCorrectionResult(BaseModel):
    corrected_composition: str
    grammar_mistakes: list[CorrectionIssue] = Field(default_factory=list)
    spelling_punctuation_issues: list[CorrectionIssue] = Field(default_factory=list)


class VocabularyImprovementInput(BaseModel):
    text: str


class VocabularyImprovementResult(BaseModel):
    vocabulary_improvements: list[VocabularyImprovement] = Field(default_factory=list)


class SentenceStyleImprovementInput(BaseModel):
    text: str


class SentenceStyleImprovementResult(BaseModel):
    sentence_level_suggestions: list[SentenceSuggestion] = Field(default_factory=list)
    structure_style_feedback: str


class ScoringCommentInput(BaseModel):
    reviewed_text: str
    corrected_composition: str
    grammar_mistakes: list[CorrectionIssue] = Field(default_factory=list)
    spelling_punctuation_issues: list[CorrectionIssue] = Field(default_factory=list)
    vocabulary_improvements: list[VocabularyImprovement] = Field(default_factory=list)
    sentence_level_suggestions: list[SentenceSuggestion] = Field(default_factory=list)
    structure_style_feedback: str


class ScoringCommentResult(BaseModel):
    overall_score: int = Field(ge=0, le=100)
    teacher_style_final_comment: str


class FinalReportSynthesisInput(BaseModel):
    image_path: str
    raw_ocr_text: str
    human_reviewed_text: str
    grammar_result: GrammarCorrectionResult
    vocabulary_result: VocabularyImprovementResult
    style_result: SentenceStyleImprovementResult
    scoring_result: ScoringCommentResult


class CompositionCorrectionReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    original_image_path: str = Field(
        validation_alias=AliasChoices("original_image_path", "image_path")
    )
    raw_ocr_text: str
    human_reviewed_text: str
    corrected_composition: str
    grammar_mistakes: list[CorrectionIssue] = Field(default_factory=list)
    spelling_punctuation_issues: list[CorrectionIssue] = Field(default_factory=list)
    vocabulary_improvements: list[VocabularyImprovement] = Field(default_factory=list)
    sentence_level_suggestions: list[SentenceSuggestion] = Field(default_factory=list)
    structure_style_feedback: str
    overall_score: int = Field(ge=0, le=100)
    teacher_style_final_comment: str

    @property
    def image_path(self) -> str:
        return self.original_image_path


CorrectionReport = CompositionCorrectionReport


class CorrectionRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    record_id: str
    image_path: str
    ocr_result: OcrExtractionResult
    reviewed_text: str
    final_correction_report: CompositionCorrectionReport = Field(alias="final_report")
    timestamp: str
    status: str
    path: str


class CorrectionPersistenceInput(BaseModel):
    image_path: str
    ocr_result: OcrExtractionResult
    reviewed_text: str
    final_report: CompositionCorrectionReport


class WorkflowRunResult(BaseModel):
    report: CompositionCorrectionReport
    record: CorrectionRecord


class LocalSidecarOcrAgent:
    """Offline OCR agent stub that reads sample OCR text beside the image."""

    name = "local_sidecar_ocr_agent"

    def run(self, node_input: CompositionCorrectionInput) -> OcrExtractionResult:
        image = Path(node_input.image_path)
        sidecar = image.with_suffix(".txt")

        if not sidecar.exists():
            return OcrExtractionResult(
                image_path=str(image),
                raw_text="",
                confidence=0.0,
                provider=self.name,
                notes=[
                    "No sidecar OCR text was found. Run the ADK ocr_extraction_agent "
                    "with a multimodal model for production handwriting OCR."
                ],
            )

        return OcrExtractionResult(
            image_path=str(image),
            raw_text=sidecar.read_text(encoding="utf-8").strip(),
            confidence=0.95,
            provider=self.name,
            notes=[
                f"Local OCR agent stub loaded text from sidecar file {sidecar}. "
                "Use the ADK ocr_extraction_agent with an uploaded image for production OCR."
            ],
        )


class CorrectionRecordStore:
    def __init__(self, storage_dir: str | Path):
        self.storage_dir = Path(storage_dir)

    def save_completed_record(
        self,
        *,
        image_path: str,
        raw_ocr_text: str,
        reviewed_text: str,
        final_report: CompositionCorrectionReport,
        ocr_result: OcrExtractionResult | None = None,
        record_id: str | None = None,
    ) -> CorrectionRecord:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        record_id = record_id or _record_id()
        path = self.storage_dir / f"{record_id}.json"
        record = CorrectionRecord(
            record_id=record_id,
            image_path=image_path,
            ocr_result=ocr_result
            or OcrExtractionResult(
                image_path=image_path,
                raw_text=raw_ocr_text,
                confidence=None,
                provider="unknown",
            ),
            reviewed_text=reviewed_text,
            final_report=final_report,
            timestamp=_utc_timestamp(),
            status="completed",
            path=str(path),
        )
        path.write_text(
            json.dumps(record.model_dump(mode="json", by_alias=True), indent=2),
            encoding="utf-8",
        )
        return record

    def load(self, record_id: str) -> CorrectionRecord:
        path = self.storage_dir / f"{record_id}.json"
        return CorrectionRecord.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass
class LocalWorkflowConfig:
    storage_dir: str | Path = Path("tmp/composition_correction_records")
    human_review_response: Any = "APPROVE"
    log_steps: bool = True
    ocr_agent: LocalSidecarOcrAgent | None = None
    logger: Callable[[str], None] = print
    record_store: CorrectionRecordStore = field(init=False)
    last_record: CorrectionRecord | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.storage_dir = Path(self.storage_dir)
        self.ocr_agent = self.ocr_agent or LocalSidecarOcrAgent()
        self.record_store = CorrectionRecordStore(self.storage_dir)


def _content_text(value: types.Content) -> str:
    return "".join(
        part.text
        for part in value.parts or []
        if part.text and not getattr(part, "thought", False)
    )


def _content_to_json_model(value: Any, model_type: type[ModelT]) -> ModelT:
    if isinstance(value, model_type):
        return value
    if isinstance(value, types.Content):
        return model_type.model_validate_json(_content_text(value))
    if isinstance(value, str):
        return model_type.model_validate_json(value)
    return model_type.model_validate(value)


def _report_from_output(value: Any) -> CompositionCorrectionReport:
    return _content_to_json_model(value, CompositionCorrectionReport)


def _normalize_image_reference(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return value


def _suffix_for_mime_type(mime_type: str | None) -> str:
    suffixes = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
    }
    return suffixes.get((mime_type or "").lower(), ".bin")


def _save_inline_image(part: types.Part) -> str | None:
    if not part.inline_data or not part.inline_data.data:
        return None

    inline_data = part.inline_data
    suffix = Path(inline_data.display_name or "").suffix
    if not suffix:
        suffix = _suffix_for_mime_type(inline_data.mime_type)

    upload_dir = Path("tmp") / "composition_correction_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    image_path = upload_dir / f"upload_{uuid.uuid4().hex}{suffix}"
    image_path.write_bytes(inline_data.data)
    return str(image_path)


def _input_from_content(content: types.Content) -> CompositionCorrectionInput:
    for part in content.parts or []:
        if part.file_data and part.file_data.file_uri:
            return CompositionCorrectionInput(
                image_path=_normalize_image_reference(part.file_data.file_uri)
            )
        inline_image_path = _save_inline_image(part)
        if inline_image_path:
            return CompositionCorrectionInput(image_path=inline_image_path)

    text = _content_text(content).strip()
    if not text:
        raise ValueError(
            "Composition correction requires an uploaded image or an image path."
        )

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return CompositionCorrectionInput(image_path=_normalize_image_reference(text))

    if isinstance(parsed, dict):
        if "image_path" in parsed and isinstance(parsed["image_path"], str):
            parsed["image_path"] = _normalize_image_reference(parsed["image_path"])
        return CompositionCorrectionInput.model_validate(parsed)

    return CompositionCorrectionInput(image_path=_normalize_image_reference(text))


def _review_response_to_text(response: Any) -> str:
    if isinstance(response, dict) and "result" in response:
        response = response["result"]
    return str(response).strip()


def _review_response_to_result(response: Any, *, raw_text: str) -> HumanOcrReview:
    if isinstance(response, HumanOcrReview):
        return response
    if isinstance(response, dict) and "result" in response:
        response = response["result"]
    if isinstance(response, dict):
        reviewed_text = str(response.get("reviewed_text", raw_text)).strip()
        reviewer_notes = str(
            response.get("reviewer_notes", "Edited by human reviewer.")
        ).strip()
        return HumanOcrReview(
            approved=True,
            reviewed_text=reviewed_text or raw_text,
            reviewer_notes=reviewer_notes,
        )

    text = _review_response_to_text(response)
    if text.lower() in {"approve", "approved", "yes", "y", "ok"}:
        return HumanOcrReview(
            approved=True,
            reviewed_text=raw_text,
            reviewer_notes="Approved without edits.",
        )
    return HumanOcrReview(
        approved=True,
        reviewed_text=text,
        reviewer_notes="Edited by human reviewer.",
    )


ocr_extraction_agent = Agent(
    name="ocr_extraction_agent",
    model="gemini-flash-latest",
    output_schema=OcrExtractionResult,
    instruction=(
        "You are the OCR extraction agent. Inspect the uploaded handwriting image "
        "or image reference supplied by the workflow, transcribe only the student's "
        "English composition, and return image_path, raw_text, confidence, provider, "
        "and notes. Do not correct grammar or vocabulary in this step."
    ),
)

ocr_quality_review_agent = Agent(
    name="ocr_quality_review_agent",
    model="gemini-flash-latest",
    output_schema=OcrQualityReviewResult,
    instruction=(
        "Review the OCR text for obvious recognition artifacts. Do not correct "
        "student grammar yet. Suggest only OCR-focused edits and flag that human "
        "review is required before correction."
    ),
)

grammar_correction_agent = Agent(
    name="grammar_correction_agent",
    model="gemini-flash-latest",
    output_schema=GrammarCorrectionResult,
    instruction=(
        "Correct grammar in the reviewed composition. Return a corrected "
        "composition plus specific grammar and spelling or punctuation issues."
    ),
)

vocabulary_improvement_agent = Agent(
    name="vocabulary_improvement_agent",
    model="gemini-flash-latest",
    output_schema=VocabularyImprovementResult,
    instruction=(
        "Suggest vocabulary improvements for a student English composition. "
        "Keep suggestions teacher-friendly and age-appropriate."
    ),
)

sentence_style_improvement_agent = Agent(
    name="sentence_style_improvement_agent",
    model="gemini-flash-latest",
    output_schema=SentenceStyleImprovementResult,
    instruction=(
        "Give sentence-level and structure/style improvement suggestions. "
        "Do not replace the full essay; provide actionable teacher feedback."
    ),
)

scoring_comment_agent = Agent(
    name="scoring_comment_agent",
    model="gemini-flash-latest",
    output_schema=ScoringCommentResult,
    instruction=(
        "Score the corrected composition from 0 to 100 and write a short "
        "teacher-style final comment."
    ),
)

final_report_synthesis_agent = Agent(
    name="final_report_synthesis_agent",
    model="gemini-flash-latest",
    output_schema=CompositionCorrectionReport,
    instruction=(
        "Synthesize all agent outputs into the final structured correction report. "
        "Include the original image path, raw OCR text, human-reviewed text, "
        "corrected composition, issues, improvements, score, and final comment."
    ),
)


@node(name="request_ocr_text_review", rerun_on_resume=False)
async def request_ocr_text_review(node_input: OcrQualityReviewResult):
    yield RequestInput(
        message=(
            "Review the OCR text below before correction. Reply APPROVE to use it "
            "as-is, or reply with edited OCR text.\n\n"
            f"{node_input.suggested_text}"
        ),
        payload=node_input.model_dump(mode="json"),
        response_schema=str,
    )


@node(name="persist_correction_record")
def persist_correction_record(node_input: CorrectionPersistenceInput):
    store = CorrectionRecordStore(Path("tmp/composition_correction_records"))
    return store.save_completed_record(
        image_path=node_input.image_path,
        raw_ocr_text=node_input.ocr_result.raw_text,
        reviewed_text=node_input.reviewed_text,
        final_report=node_input.final_report,
        ocr_result=node_input.ocr_result,
    )


def _input_from_node_input(node_input: Any) -> CompositionCorrectionInput:
    if isinstance(node_input, CompositionCorrectionInput):
        return node_input
    if isinstance(node_input, types.Content):
        return _input_from_content(node_input)
    if isinstance(node_input, str):
        return CompositionCorrectionInput(image_path=_normalize_image_reference(node_input))
    return CompositionCorrectionInput.model_validate(node_input)


async def _composition_correction_workflow_impl(
    ctx: Context, node_input: Any
) -> CompositionCorrectionReport:
    request = _input_from_node_input(node_input)

    ocr_raw = await ctx.run_node(ocr_extraction_agent, request)
    ocr_result = _content_to_json_model(ocr_raw, OcrExtractionResult)

    quality_raw = await ctx.run_node(ocr_quality_review_agent, ocr_result)
    quality_review = _content_to_json_model(quality_raw, OcrQualityReviewResult)

    human_review_response = await ctx.run_node(request_ocr_text_review, quality_review)
    human_review = _review_response_to_result(
        human_review_response,
        raw_text=quality_review.suggested_text or ocr_result.raw_text,
    )

    grammar_raw = await ctx.run_node(
        grammar_correction_agent,
        GrammarCorrectionInput(text=human_review.reviewed_text),
    )
    grammar_result = _content_to_json_model(grammar_raw, GrammarCorrectionResult)

    vocabulary_raw = await ctx.run_node(
        vocabulary_improvement_agent,
        VocabularyImprovementInput(text=grammar_result.corrected_composition),
    )
    vocabulary_result = _content_to_json_model(
        vocabulary_raw, VocabularyImprovementResult
    )

    style_raw = await ctx.run_node(
        sentence_style_improvement_agent,
        SentenceStyleImprovementInput(text=grammar_result.corrected_composition),
    )
    style_result = _content_to_json_model(style_raw, SentenceStyleImprovementResult)

    scoring_raw = await ctx.run_node(
        scoring_comment_agent,
        ScoringCommentInput(
            reviewed_text=human_review.reviewed_text,
            corrected_composition=grammar_result.corrected_composition,
            grammar_mistakes=grammar_result.grammar_mistakes,
            spelling_punctuation_issues=grammar_result.spelling_punctuation_issues,
            vocabulary_improvements=vocabulary_result.vocabulary_improvements,
            sentence_level_suggestions=style_result.sentence_level_suggestions,
            structure_style_feedback=style_result.structure_style_feedback,
        ),
    )
    scoring_result = _content_to_json_model(scoring_raw, ScoringCommentResult)

    final_raw = await ctx.run_node(
        final_report_synthesis_agent,
        FinalReportSynthesisInput(
            image_path=request.image_path,
            raw_ocr_text=ocr_result.raw_text,
            human_reviewed_text=human_review.reviewed_text,
            grammar_result=grammar_result,
            vocabulary_result=vocabulary_result,
            style_result=style_result,
            scoring_result=scoring_result,
        ),
    )
    report = _report_from_output(final_raw)

    await ctx.run_node(
        persist_correction_record,
        CorrectionPersistenceInput(
            image_path=request.image_path,
            ocr_result=ocr_result,
            reviewed_text=human_review.reviewed_text,
            final_report=report,
        ),
    )
    return report


@node(name="composition_correction_workflow", rerun_on_resume=True)
async def composition_correction_workflow(ctx: Context, node_input: Any):
    return await _composition_correction_workflow_impl(ctx, node_input)


root_agent = Workflow(
    name="composition_correction_root_agent",
    edges=[("START", composition_correction_workflow)],
)


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _replace_phrase(
    text: str,
    original: str,
    suggestion: str,
    explanation: str,
    issues: list[CorrectionIssue],
) -> str:
    pattern = re.compile(re.escape(original), re.IGNORECASE)
    if pattern.search(text):
        issues.append(
            CorrectionIssue(
                original=original,
                suggestion=suggestion,
                explanation=explanation,
            )
        )
        return pattern.sub(suggestion, text)
    return text


def _local_ocr_quality_review(ocr_result: OcrExtractionResult) -> OcrQualityReviewResult:
    suggested_text = _collapse_spaces(ocr_result.raw_text)
    notes = ["OCR text normalized for whitespace."]
    if not suggested_text:
        notes.append(
            "OCR text is empty. Run the OCR extraction agent with a real multimodal model before production use."
        )
    return OcrQualityReviewResult(
        raw_text=ocr_result.raw_text,
        suggested_text=suggested_text,
        quality_notes=notes,
        needs_human_review=True,
    )


def _local_grammar_correction(
    node_input: GrammarCorrectionInput,
) -> GrammarCorrectionResult:
    corrected = _collapse_spaces(node_input.text)
    grammar_issues: list[CorrectionIssue] = []
    spelling_punctuation_issues: list[CorrectionIssue] = []

    corrected = _replace_phrase(
        corrected,
        "I has",
        "I have",
        "Use 'have' with the subject 'I'.",
        grammar_issues,
    )
    corrected = _replace_phrase(
        corrected,
        "a apple",
        "an apple",
        "Use 'an' before a word that starts with a vowel sound.",
        grammar_issues,
    )
    corrected = _replace_phrase(
        corrected,
        "She go",
        "She goes",
        "Use the third-person singular verb form with 'she'.",
        grammar_issues,
    )
    corrected = _replace_phrase(
        corrected,
        "I go to park",
        "I went to the park",
        "Use past tense and include the article before 'park'.",
        grammar_issues,
    )
    corrected = _replace_phrase(
        corrected,
        "I see many peoples",
        "I saw many people",
        "Use past tense and the plural noun 'people'.",
        grammar_issues,
    )

    if corrected and corrected[-1] not in ".!?":
        spelling_punctuation_issues.append(
            CorrectionIssue(
                original=corrected,
                suggestion=f"{corrected}.",
                explanation="End the composition with punctuation.",
            )
        )
        corrected = f"{corrected}."

    return GrammarCorrectionResult(
        corrected_composition=corrected,
        grammar_mistakes=grammar_issues,
        spelling_punctuation_issues=spelling_punctuation_issues,
    )


def _local_vocabulary_improvement(
    node_input: VocabularyImprovementInput,
) -> VocabularyImprovementResult:
    improvements: list[VocabularyImprovement] = []
    if re.search(r"\bvery fun\b", node_input.text, re.IGNORECASE):
        improvements.append(
            VocabularyImprovement(
                original="very fun",
                suggestion="very enjoyable",
                explanation="Use a more precise adjective phrase.",
            )
        )
    if re.search(r"\bgood\b", node_input.text, re.IGNORECASE):
        improvements.append(
            VocabularyImprovement(
                original="good",
                suggestion="positive",
                explanation="A more specific adjective can make the writing clearer.",
            )
        )
    return VocabularyImprovementResult(vocabulary_improvements=improvements)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def _local_sentence_style_improvement(
    node_input: SentenceStyleImprovementInput,
) -> SentenceStyleImprovementResult:
    sentences = _sentences(node_input.text)
    suggestions: list[SentenceSuggestion] = []
    if sentences:
        first = sentences[0]
        suggestions.append(
            SentenceSuggestion(
                original=first,
                suggestion=f"{first} Add one detail about when, where, or why it happened.",
                explanation="A supporting detail makes the composition more vivid.",
            )
        )
    feedback = (
        "The composition has a clear basic sequence. Add more supporting details "
        "and link ideas with simple transitions."
    )
    return SentenceStyleImprovementResult(
        sentence_level_suggestions=suggestions,
        structure_style_feedback=feedback,
    )


def _local_scoring_comment(node_input: ScoringCommentInput) -> ScoringCommentResult:
    issue_count = (
        len(node_input.grammar_mistakes)
        + len(node_input.spelling_punctuation_issues)
        + len(node_input.vocabulary_improvements)
        + len(node_input.sentence_level_suggestions)
    )
    score = max(0, min(100, 95 - issue_count * 5))
    if node_input.grammar_mistakes:
        focus = node_input.grammar_mistakes[0].explanation
    elif node_input.vocabulary_improvements:
        focus = node_input.vocabulary_improvements[0].explanation
    else:
        focus = "Keep adding specific details to develop your ideas."
    return ScoringCommentResult(
        overall_score=score,
        teacher_style_final_comment=(
            "Good effort. Your meaning is understandable, and the corrected version "
            f"is clearer. Next time, focus on this point: {focus}"
        ),
    )


def _local_final_report_synthesis(
    node_input: FinalReportSynthesisInput,
) -> CompositionCorrectionReport:
    corrected = node_input.grammar_result.corrected_composition
    for improvement in node_input.vocabulary_result.vocabulary_improvements:
        corrected = re.sub(
            re.escape(improvement.original),
            improvement.suggestion,
            corrected,
            flags=re.IGNORECASE,
        )

    return CompositionCorrectionReport(
        original_image_path=node_input.image_path,
        raw_ocr_text=node_input.raw_ocr_text,
        human_reviewed_text=node_input.human_reviewed_text,
        corrected_composition=corrected,
        grammar_mistakes=node_input.grammar_result.grammar_mistakes,
        spelling_punctuation_issues=(
            node_input.grammar_result.spelling_punctuation_issues
        ),
        vocabulary_improvements=(
            node_input.vocabulary_result.vocabulary_improvements
        ),
        sentence_level_suggestions=node_input.style_result.sentence_level_suggestions,
        structure_style_feedback=node_input.style_result.structure_style_feedback,
        overall_score=node_input.scoring_result.overall_score,
        teacher_style_final_comment=(
            node_input.scoring_result.teacher_style_final_comment
        ),
    )


def _brief(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    text = json.dumps(value, ensure_ascii=True, default=str)
    return text if len(text) <= 500 else f"{text[:497]}..."


class LocalWorkflowContext:
    def __init__(self, config: LocalWorkflowConfig):
        self.config = config

    def _log(self, message: str) -> None:
        if self.config.log_steps:
            self.config.logger(message)

    async def run_node(self, node_like: Any, node_input: Any) -> Any:
        name = getattr(node_like, "name", str(node_like))
        self._log(f"[{name}] input: {_brief(node_input)}")

        if name == ocr_extraction_agent.name:
            request = _input_from_node_input(node_input)
            output = self.config.ocr_agent.run(request)
        elif name == ocr_quality_review_agent.name:
            output = _local_ocr_quality_review(
                _content_to_json_model(node_input, OcrExtractionResult)
            )
        elif name == request_ocr_text_review.name:
            output = self.config.human_review_response
        elif name == grammar_correction_agent.name:
            output = _local_grammar_correction(
                _content_to_json_model(node_input, GrammarCorrectionInput)
            )
        elif name == vocabulary_improvement_agent.name:
            output = _local_vocabulary_improvement(
                _content_to_json_model(node_input, VocabularyImprovementInput)
            )
        elif name == sentence_style_improvement_agent.name:
            output = _local_sentence_style_improvement(
                _content_to_json_model(node_input, SentenceStyleImprovementInput)
            )
        elif name == scoring_comment_agent.name:
            output = _local_scoring_comment(
                _content_to_json_model(node_input, ScoringCommentInput)
            )
        elif name == final_report_synthesis_agent.name:
            output = _local_final_report_synthesis(
                _content_to_json_model(node_input, FinalReportSynthesisInput)
            )
        elif name == persist_correction_record.name:
            persist_input = _content_to_json_model(node_input, CorrectionPersistenceInput)
            output = self.config.record_store.save_completed_record(
                image_path=persist_input.image_path,
                raw_ocr_text=persist_input.ocr_result.raw_text,
                reviewed_text=persist_input.reviewed_text,
                final_report=persist_input.final_report,
                ocr_result=persist_input.ocr_result,
            )
            self.config.last_record = output
        else:
            raise ValueError(f"Local workflow has no stub for node {name!r}")

        self._log(f"[{name}] output: {_brief(output)}")
        return output


async def run_local_correction_workflow(
    node_input: Any,
    *,
    config: LocalWorkflowConfig | None = None,
) -> CompositionCorrectionReport:
    config = config or LocalWorkflowConfig()
    ctx = LocalWorkflowContext(config)
    return await _composition_correction_workflow_impl(ctx, node_input)


def run_sample_workflow(
    node_input: Any,
    *,
    storage_dir: str | Path = Path("tmp/composition_correction_records"),
    human_review_response: Any = "APPROVE",
    log_steps: bool = True,
) -> WorkflowRunResult:
    config = LocalWorkflowConfig(
        storage_dir=storage_dir,
        human_review_response=human_review_response,
        log_steps=log_steps,
    )
    report = asyncio.run(run_local_correction_workflow(node_input, config=config))
    if config.last_record is None:
        raise RuntimeError("Correction workflow finished without persisting a record.")
    return WorkflowRunResult(report=report, record=config.last_record)
