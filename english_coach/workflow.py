"""
Workflow: produce English coaching feedback and training inputs from screenshots.

Reads ADK Web uploaded images or ./input/* and ./input/tem/* supported images,
classifies each image, sends it to the matching Gemini structured extractor,
renames each processed input from visible student/date evidence, merges all
outputs into per-student learning profiles, and writes both Markdown reports
and JSON training inputs.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import json
import re
from pathlib import Path
from urllib.parse import quote

from google.adk import Context
from google.adk import Event
from google.adk import Workflow
from google.adk.workflow import RetryConfig
from google.adk.workflow import node
from google.genai import types

from . import config
from . import utils
from .agents import (
    classify_input_image,
    extractor,
    grammar_training_extractor,
)
from .constants import (
    PDF_EXPORTS_DIR,
    REPORTS_DIR,
    TRAINING_INPUTS_DIR,
    WRITING_INPUTS_DIR,
)
from .pdf_export import (
    PdfExportError,
    PdfOpenError,
    export_report_pdf,
    open_pdf_in_wps,
)
from .schemas import (
    EnglishCoachFeedback,
    ImageCategory,
    InputProcessingResult,
    StudentLearningProfile,
    WritingEvidence,
    GrammarTrainingEvidence,
)


# ============================================================================
# Input Discovery
# ============================================================================

def _writing_input_scan_dirs() -> list[Path]:
    """Directories to scan for input images."""
    return [WRITING_INPUTS_DIR, WRITING_INPUTS_DIR / "tem"]


def _uploaded_inputs_dir() -> Path:
    """Directory for uploaded inline images."""
    return WRITING_INPUTS_DIR / "uploads"


def _uploaded_inline_image_inputs(
    node_input: object,
    feedback_language: config.FeedbackLanguage,
) -> list[dict[str, str]]:
    """Extract inline images from ADK Web Content input."""
    if not isinstance(node_input, types.Content):
        return []

    items: list[dict[str, str]] = []
    for index, part in enumerate(node_input.parts or [], start=1):
        blob = part.inline_data
        if blob is None:
            continue
        mime = utils.mime_for_inline_image(blob)
        if mime is None:
            continue
        data = utils.blob_bytes(blob.data)
        if not data:
            continue

        upload_dir = _uploaded_inputs_dir()
        upload_dir.mkdir(parents=True, exist_ok=True)
        filename = utils.safe_upload_filename(
            display_name=blob.display_name,
            index=index,
            mime=mime,
        )
        path = utils.unique_upload_path(upload_dir, filename)
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
    feedback_language = utils.feedback_language_from_input(node_input)
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
            mime = config.MIME_BY_SUFFIX.get(path.suffix.lower())
            if mime is None:
                continue
            items.append({
                "path": str(path),
                "filename": path.name,
                "mime": mime,
                "feedback_language": feedback_language,
            })
    return items


# ============================================================================
# Routing
# ============================================================================

def pick_input_route(image_category: ImageCategory):
    """Route key copied from adk-kit's router_intent pick(...) shape."""
    yield Event(route=image_category.category)


# ============================================================================
# Single Input Processing
# ============================================================================

class InputProcessingError(RuntimeError):
    """One input image failed classification or extraction."""


async def _run_extractor(
    ctx: Context,
    *,
    agent,
    schema: type[utils.ModelT],
    path: str,
    filename: str,
    mime: str,
    feedback_language: config.FeedbackLanguage,
    task_description: str,
) -> utils.ModelT:
    """Run one extractor agent and return validated output."""
    raw_output = await ctx.run_node(
        agent,
        node_input=utils.image_content(
            path=path,
            filename=filename,
            mime=mime,
            feedback_language=feedback_language,
            task=utils.task_for_attempt(task_description, ctx.attempt_count),
        ),
        use_sub_branch=True,
    )
    return utils.model_from_output(raw_output, schema)


async def _process_one_input_result(
    ctx: Context,
    *,
    path: str,
    filename: str,
    mime: str,
    feedback_language: config.FeedbackLanguage,
) -> InputProcessingResult:
    """Classify one image, run the matching extractor, and build the result."""
    category = await _run_extractor(
        ctx,
        agent=classify_input_image,
        schema=ImageCategory,
        path=path,
        filename=filename,
        mime=mime,
        feedback_language=feedback_language,
        task_description="Classify this image before any grading or extraction.",
    )
    route_event = next(pick_input_route(category))
    route = route_event.actions.route

    if route == "writing":
        evidence = await _run_extractor(
            ctx,
            agent=extractor,
            schema=WritingEvidence,
            path=path,
            filename=filename,
            mime=mime,
            feedback_language=feedback_language,
            task_description="Grade the writing submission by extracting evidence only.",
        )
        original_evidence = evidence.model_copy(deep=True)
        student_name = utils.resolve_student_name(
            filename=filename,
            category_name=category.student_name,
            evidence_name=evidence.student_name,
        )
        submission_date = utils.resolve_submission_date(
            evidence_date=evidence.submission_date,
            category_date=category.submission_date,
        )
        evidence = evidence.model_copy(
            update={"student_name": student_name, "submission_date": submission_date}
        )
        dimensions = utils.score_from_evidence(evidence)
        feedback = EnglishCoachFeedback(
            filename=filename,
            student_name=student_name,
            submission_date=submission_date,
            feedback_language=feedback_language,
            prompt_summary=evidence.prompt_summary,
            transcription=evidence.transcription,
            model_essay=evidence.model_essay,
            overall_score=utils.calculate_overall_score(dimensions),
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
            classification=category,
            writing_evidence=original_evidence,
            learning_needs=utils.writing_learning_needs(
                filename=filename,
                student_name=student_name,
                evidence=evidence,
            ),
        )

    if route == "grammar_training":
        evidence = await _run_extractor(
            ctx,
            agent=grammar_training_extractor,
            schema=GrammarTrainingEvidence,
            path=path,
            filename=filename,
            mime=mime,
            feedback_language=feedback_language,
            task_description="Extract grammar-training mistakes from this image.",
        )
        original_evidence = evidence.model_copy(deep=True)
        student_name = utils.resolve_student_name(
            filename=filename,
            category_name=category.student_name,
            evidence_name=evidence.student_name,
        )
        submission_date = utils.resolve_submission_date(
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
            classification=category,
            grammar_evidence=original_evidence,
            learning_needs=utils.grammar_learning_needs(
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
        student_name=utils.resolve_student_name(
            filename=filename,
            category_name=category.student_name,
            evidence_name="unknown",
        ),
        submission_date=utils.resolve_submission_date(
            evidence_date="",
            category_date=category.submission_date,
        ),
        feedback_language=feedback_language,
        skipped_reason=reason,
    )


@node(
    retry_config=RetryConfig(
        max_attempts=config.PROCESS_INPUT_MAX_ATTEMPTS,
        initial_delay=config.RETRY_INITIAL_DELAY,
    ),
    rerun_on_resume=True,
)
async def process_one_input(ctx: Context, node_input: dict[str, str]):
    """Classify one input image and run the matching specialist extractor."""
    path = node_input["path"]
    filename = node_input["filename"]
    mime = node_input["mime"]
    feedback_language = utils.feedback_language_from_input(
        node_input.get("feedback_language", config.DEFAULT_FEEDBACK_LANGUAGE)
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
        reason = utils.short_failure_reason(exc)
        if ctx.attempt_count < config.PROCESS_INPUT_MAX_ATTEMPTS:
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
            student_name=utils.student_hint_from_filename(filename),
            submission_date=utils.default_submission_date(),
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
        supported = ", ".join(sorted(config.MIME_BY_SUFFIX))
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


# ============================================================================
# Post-Processing: Rename Files
# ============================================================================

def _coerce_results(
    node_input: list[InputProcessingResult] | list[dict[str, object]],
) -> list[InputProcessingResult]:
    """Convert node input to InputProcessingResult list."""
    return [InputProcessingResult.model_validate(item) for item in node_input]


def _result_with_filename(
    *,
    result: InputProcessingResult,
    filename: str,
    source_path: str,
) -> InputProcessingResult:
    """Update result with new filename."""
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
    """Rename one processed input file to canonical format."""
    if not utils.known_name(result.student_name):
        return result, None

    student_segment = utils.safe_filename_segment(result.student_name)
    if student_segment == "unknown":
        return result, None

    source_path = Path(result.source_path) if result.source_path else None
    if source_path is None or not source_path.is_file():
        return result, None

    target_stem = utils.canonical_input_stem(
        student_name=result.student_name,
        submission_date=(
            utils.normalize_submission_date(result.submission_date)
            or utils.default_submission_date()
        ),
    )
    target_path = utils.unique_input_path(
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


# ============================================================================
# Profile Building
# ============================================================================

def build_student_profiles(
    node_input: list[InputProcessingResult] | list[dict[str, object]],
    *,
    infer_unknown_student: bool = True,
) -> list[StudentLearningProfile]:
    """Aggregate processing results into per-student profiles."""
    results = _coerce_results(node_input)
    known_students = sorted({
        result.student_name
        for result in results
        if utils.known_name(result.student_name)
    })

    profiles: dict[str, StudentLearningProfile] = {}
    for result in results:
        student_name = result.student_name
        if not utils.known_name(student_name):
            if not infer_unknown_student:
                student_name = "unknown"
            elif len(known_students) == 1:
                student_name = known_students[0]
            else:
                hint = utils.student_hint_from_filename(result.filename)
                student_name = hint if utils.known_name(hint) else "unknown"

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


# ============================================================================
# Report Rendering
# ============================================================================

def _render_profile_markdown(
    profile: StudentLearningProfile,
    *,
    display_ts: str,
    training_path: Path,
    review_label: str | None = None,
) -> tuple[list[str], list[str]]:
    """Render one student profile as frontmatter and body lines."""
    student = profile.student_name or "unknown"
    lang = profile.feedback_language

    def plain(value: object) -> str:
        if review_label is None:
            return str(value)
        escaped = html.escape(str(value), quote=False)
        escaped = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", escaped)
        return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")

    def cell(value: object) -> str:
        return plain(value) if review_label is not None else utils.markdown_cell(value)

    def fence(value: str) -> str:
        if review_label is None:
            return "```"
        longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
        return "`" * max(3, longest + 1)

    frontmatter_lines: list[str] = [
        "---",
        "schema_version: 2",
        f"report_type: {utils.yaml_string('student_learning_profile')}",
        f"student: {utils.yaml_string(student)}",
        f"feedback_language: {utils.yaml_string(profile.feedback_language)}",
        f"generated_at: {utils.yaml_string(display_ts)}",
        f"submission_count: {len(profile.feedback_items)}",
        f"grammar_training_count: {len(profile.grammar_trainings)}",
        f"learning_need_count: {len(profile.learning_needs)}",
        f"training_input_json: {utils.yaml_string(str(training_path))}",
        "---",
    ]

    lines: list[str] = [
        "",
        f"# {utils.i18n_report('title', lang)}",
        "",
    ]
    if review_label is not None:
        lines.extend([plain(review_label), "", f"学生：{plain(student)}", ""])

    # Only show Score Summary if there are multiple submissions
    if len(profile.feedback_items) > 1:
        lines.extend([
            f"## {utils.i18n_report('score_summary', lang)}",
            f"| {utils.i18n_report('col_submission', lang)} | Overall | Content | Structure | Language | Handwriting |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ])
        for feedback in profile.feedback_items:
            d = feedback.dimensions
            lines.append(
                f"| {cell(feedback.filename)} | {feedback.overall_score:.1f}/20 |"
                f" {d.content}/5 | {d.structure}/5 | {d.language:.1f}/5 |"
                f" {d.handwriting}/5 |"
            )
        lines.append("")

    if profile.feedback_items:
        lines.append(f"## {utils.i18n_report('submission_details', lang)}")
        for index, feedback in enumerate(profile.feedback_items, start=1):
            d = feedback.dimensions
            lines.extend([
                "",
                f"### {index}. {plain(feedback.filename)}",
                "",
                *([f"提交日期：{plain(feedback.submission_date or '待核对')}", ""] if review_label is not None else []),
                f"#### {utils.i18n_report('score_breakdown', lang)}",
                "| Overall | Content | Structure | Language | Handwriting |",
                "| ---: | ---: | ---: | ---: | ---: |",
                (
                    f"| {feedback.overall_score:.1f}/20 | {d.content}/5 |"
                    f" {d.structure}/5 | {d.language:.1f}/5 | {d.handwriting}/5 |"
                ),
                "",
                f"#### {utils.i18n_report('prompt', lang)}",
                plain(feedback.prompt_summary),
                "",
                f"#### {utils.i18n_report('strengths', lang)}",
            ])
            for strength in feedback.strengths:
                lines.append(f"- {plain(strength)}")
            lines.append("")
            lines.append(f"#### {utils.i18n_report('improvements', lang)}")
            for improvement in feedback.improvements:
                lines.append(f"- {plain(improvement)}")
            lines.extend([
                "",
                f"#### {utils.i18n_report('original', lang)}",
                f"{fence(feedback.transcription)}text",
            ])
            lines.extend(feedback.transcription.splitlines() or [feedback.transcription])
            lines.append(fence(feedback.transcription))
            model_essay = feedback.model_essay.strip()
            if model_essay:
                lines.extend([
                    "",
                    f"#### {utils.i18n_report('model_essay', lang)}",
                    f"{fence(model_essay)}text",
                ])
                lines.extend(model_essay.splitlines() or [model_essay])
                lines.append(fence(model_essay))
            lines.append("")

    date_heading = " 提交日期 |" if review_label is not None else ""
    date_divider = " --- |" if review_label is not None else ""
    lines.extend([
        f"## {utils.i18n_report('grammar_training', lang)}",
        f"|{date_heading} {utils.i18n_report('col_skill', lang)} | {utils.i18n_report('col_original', lang)} | {utils.i18n_report('col_correct', lang)} | {utils.i18n_report('col_explanation', lang)} |",
        f"|{date_divider} --- | --- | --- | --- |",
    ])
    grammar_rows = 0
    for training in profile.grammar_trainings:
        if review_label is not None and not training.mistakes:
            grammar_rows += 1
            lines.append(f"| {cell(training.submission_date or '待核对')} | {utils.i18n_report('no_grammar_data', lang)} | | | |")
        for mistake in training.mistakes:
            grammar_rows += 1
            date_cell = f" {cell(training.submission_date or '待核对')} |" if review_label is not None else ""
            lines.append(
                f"|{date_cell} {cell(mistake.skill_tag)} |"
                f" {cell(mistake.original_answer)} |"
                f" {cell(mistake.correct_answer)} |"
                f" {cell(mistake.explanation)} |"
            )
    if grammar_rows == 0:
        lines.append(f"|{' |' if review_label is not None else ''} {utils.i18n_report('no_grammar_data', lang)} | | | |")
    lines.append("")

    lines.extend([
        f"## {utils.i18n_report('training_input', lang)}",
        f"| {utils.i18n_report('col_source', lang)} | {utils.i18n_report('col_skill', lang)} | {utils.i18n_report('col_evidence', lang)} | {utils.i18n_report('col_fix', lang)} | {utils.i18n_report('col_explanation', lang)} |",
        "| --- | --- | --- | --- | --- |",
    ])
    for need in profile.learning_needs:
        lines.append(
            f"| {cell(need.source_type)} |"
            f" {cell(need.skill_tag)} |"
            f" {cell(need.evidence)} |"
            f" {cell(need.suggested_fix)} |"
            f" {cell(need.explanation)} |"
        )
    if not profile.learning_needs:
        lines.append(f"| {utils.i18n_report('no_training_needs', lang)} | | | | |")

    if profile.skipped:
        lines.extend(["", "## Skipped Images"])
        for skipped in profile.skipped:
            lines.append(f"- {plain(skipped)}")

    return frontmatter_lines, lines


def write_report(
    node_input: list[StudentLearningProfile] | list[dict[str, object]],
    *,
    reports_dir: Path | None = None,
    training_inputs_dir: Path | None = None,
    pdf_exports_dir: Path | None = None,
    open_in_wps: bool = True,
    review_label: str | None = None,
):
    """Write reports, optionally into an isolated Web submission directory."""
    reports_dir = REPORTS_DIR if reports_dir is None else reports_dir
    training_inputs_dir = (
        TRAINING_INPUTS_DIR if training_inputs_dir is None else training_inputs_dir
    )
    pdf_exports_dir = PDF_EXPORTS_DIR if pdf_exports_dir is None else pdf_exports_dir
    profiles = [
        StudentLearningProfile.model_validate(profile) for profile in node_input
    ]
    if not profiles:
        yield Event(message="Nothing processed; no report written.")
        return

    now = datetime.datetime.now().astimezone()
    file_ts = now.strftime("%Y-%m-%d_%H-%M-%S")
    display_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    reports_dir.mkdir(parents=True, exist_ok=True)
    training_inputs_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    pdf_links: list[tuple[Path, str]] = []
    pdf_warnings: list[str] = []
    wps_warnings: list[str] = []
    session_sections: list[list[str]] = []
    report_manifest: list[dict[str, str]] = []

    def available_stem(base_stem: str) -> str:
        """Keep all three artifacts aligned without replacing an existing file."""
        stem = base_stem
        suffix = 2
        while any((directory / f"{stem}{extension}").exists() for directory, extension in (
            (reports_dir, ".md"), (training_inputs_dir, ".json"), (pdf_exports_dir, ".pdf"),
        )):
            stem = f"{base_stem}_{suffix}"
            suffix += 1
        return stem

    for profile_index, profile in enumerate(profiles, start=1):
        student = profile.student_name or "unknown"
        safe_student = utils.safe_filename_segment(student)
        report_stem = available_stem(f"{safe_student}_{file_ts}")
        report_path = reports_dir / f"{report_stem}.md"
        training_path = training_inputs_dir / f"{report_stem}.json"
        training_path.write_text(
            json.dumps(
                profile.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        frontmatter_lines, body_lines = _render_profile_markdown(
            profile,
            display_ts=display_ts,
            training_path=training_path,
            review_label=review_label,
        )

        report_path.write_text(
            "\n".join(frontmatter_lines + body_lines) + "\n", encoding="utf-8"
        )
        written.extend([report_path, training_path])
        session_sections.append(body_lines)

        try:
            pdf_path = export_report_pdf(report_path, output_dir=pdf_exports_dir)
        except PdfExportError as exc:
            pdf_warnings.append(f"{report_path.name}: {exc}")
        else:
            written.append(pdf_path)
            pdf_links.append((pdf_path, f"/reports/{quote(pdf_path.name)}"))
            report_manifest.append({"filename": pdf_path.name, "title": f"{student} · 学习反馈",
                                    "report_key": f"student-{profile_index}"})

    if session_sections:
        session_body: list[str] = []
        for index, section in enumerate(session_sections):
            if index:
                session_body.append('<div style="page-break-before: always;"></div>')
            session_body.extend(section)
        session_lines = [
            "---",
            "schema_version: 2",
            f"report_type: {utils.yaml_string('session_learning_profiles')}",
            f"generated_at: {utils.yaml_string(display_ts)}",
            f"student_count: {len(session_sections)}",
            "---",
            *session_body,
        ]
        session_path = reports_dir / f"{available_stem(f'session_{file_ts}')}.md"
        session_path.write_text("\n".join(session_lines) + "\n", encoding="utf-8")
        written.append(session_path)

        try:
            session_pdf = export_report_pdf(session_path, output_dir=pdf_exports_dir)
        except PdfExportError as exc:
            pdf_warnings.append(f"{session_path.name}: {exc}")
        else:
            written.append(session_pdf)
            pdf_links.append((session_pdf, f"/reports/{quote(session_pdf.name)}"))
            report_manifest.append({"filename": session_pdf.name, "title": "本次作业汇总", "report_key": "session"})
            if open_in_wps:
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
    yield Event(
        message=summary,
        output={"reports": report_manifest, "pdf_failures": len(pdf_warnings),
                "expected_report_count": len(profiles) + 1},
    )


# ============================================================================
# Workflow Definition
# ============================================================================

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

