"""
Workflow: grade handwritten student essays.

Reads ./essays/*.{jpg,jpeg,png}, sends each image to a Gemini evidence
extractor as inline multimodal Parts, scores each evidence record locally,
and writes markdown reports to ./reports/.

Composition (left-to-right execution order):
    list_essays -> orchestrate -> write_report
                       └─ ctx.run_node + asyncio.gather over grade_one ─┐
                                          grade_one -> extractor Agent ──┘

From adk_kit:
    events/event_message.py      (multimodal Part input pattern)
    nodes/agent_structured.py    (Agent + output_schema)
    nodes/node_decorator.py      (@node knobs)
    nodes/function_node.py       (plain def becomes a node)
    context/ctx_run_node.py      (dynamic sub-node execution)
    reliability/retry.py         (RetryConfig on the flaky grading step)
    recipes/dynamic_parallel.py  (runtime fan-out via ctx.run_node + gather)
"""

from __future__ import annotations

import asyncio
import datetime
import re
from pathlib import Path
from typing import Literal

from google.adk import Agent
from google.adk import Context
from google.adk import Event
from google.adk import Workflow
from google.adk.workflow import node
from google.adk.workflow import RetryConfig
from google.genai import types
from pydantic import BaseModel
from pydantic import Field

ESSAYS_DIR = Path(__file__).parent / "essays"
REPORTS_DIR = Path(__file__).parent / "reports"
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class DimensionScores(BaseModel):
  content: int
  structure: int
  language: int
  handwriting: int


class EssayEvidence(BaseModel):
  student_name: str
  prompt_summary: str
  transcription: str
  required_points_covered: int = Field(ge=0)
  required_points_total: int = Field(ge=0)
  grammar_errors: list[str]
  spelling_errors: list[str]
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


class EssayGrade(BaseModel):
  filename: str
  student_name: str
  prompt_summary: str
  transcription: str
  overall_score: int
  dimensions: DimensionScores
  strengths: list[str]
  improvements: list[str]


extractor = Agent(
    name="extractor",
    model="gemini-flash-latest",
    instruction=(
        "You are an experienced writing teacher. The user message contains a"
        " filename label followed by a single image. The image shows, top to"
        " bottom, the printed essay prompt and the student's handwritten"
        " response.\n\n"
        "Read both. Extract stable grading evidence only. Do not assign"
        " scores.\n\n"
        "student_name: the student's name as written on the image, usually at"
        " the top of the page or in a header/label area. Return only the name"
        " itself — strip any label like \"Name:\" / \"姓名:\" / \"Student:\"."
        " If you cannot find a name, return \"unknown\".\n"
        "prompt_summary: one sentence describing what the essay was meant to"
        " address.\n"
        "transcription: the student's handwritten response transcribed"
        " verbatim. Preserve their original words, line breaks, spelling, and"
        " grammar — do NOT silently correct mistakes. Use \\n for line"
        " breaks. Do not include the printed prompt at the top of the image.\n"
        "required_points_total: count the distinct required content points in"
        " the printed prompt.\n"
        "required_points_covered: count how many of those required points the"
        " student's response addresses, even if imperfectly.\n"
        "grammar_errors: list distinct grammar errors found in the student's"
        " response. Use short quoted snippets.\n"
        "spelling_errors: list distinct spelling errors found in the student's"
        " response. Use short quoted snippets.\n"
        "has_clear_structure: true if the response has a clear logical order"
        " or useful transitions.\n"
        "has_conclusion: true if the response has a concluding sentence or"
        " closing thought.\n"
        "handwriting_legibility: choose exactly one of excellent, clear,"
        " readable, hard_to_read, illegible.\n"
        "strengths: 1-3 short bullets.\n"
        "improvements: 1-3 actionable bullets."
    ),
    output_schema=EssayEvidence,
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        seed=0,
    ),
)


def list_essays(node_input: str) -> list[dict[str, str]]:
  """Scan ./essays/ for supported image files. Chat input is ignored."""
  ESSAYS_DIR.mkdir(parents=True, exist_ok=True)
  items: list[dict[str, str]] = []
  for path in sorted(ESSAYS_DIR.iterdir()):
    mime = MIME_BY_SUFFIX.get(path.suffix.lower())
    if mime is None:
      continue
    items.append({"path": str(path), "filename": path.name, "mime": mime})
  return items


def _calculate_overall_score(dimensions: DimensionScores) -> int:
  weighted = (
      dimensions.content * 4
      + dimensions.structure * 4
      + dimensions.language * 6
      + dimensions.handwriting * 6
  ) / 5
  return round(weighted)


def _score_from_evidence(evidence: EssayEvidence) -> DimensionScores:
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
  if language_error_count == 0:
    language = 5
  elif language_error_count <= 2:
    language = 4
  elif language_error_count <= 5:
    language = 3
  elif language_error_count <= 8:
    language = 2
  else:
    language = 1

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


def _evidence_from_output(result) -> EssayEvidence:
  if isinstance(result, EssayEvidence):
    return EssayEvidence.model_validate(result.model_dump())
  if isinstance(result, dict):
    return EssayEvidence.model_validate(result)
  return EssayEvidence.model_validate_json(result)


@node(
    retry_config=RetryConfig(max_attempts=3, initial_delay=2),
    rerun_on_resume=True,
)
async def grade_one(ctx: Context, node_input: dict[str, str]):
  """Grade one essay image. Retries on LLM/parse failure."""
  path = node_input["path"]
  filename = node_input["filename"]
  mime = node_input["mime"]
  yield Event(message=f"Grading {filename} (attempt {ctx.attempt_count})...")

  data = Path(path).read_bytes()
  content = types.Content(
      role="user",
      parts=[
          types.Part.from_text(
              text=f"filename: {filename}\nGrade the essay in this image."
          ),
          types.Part.from_bytes(data=data, mime_type=mime),
      ],
  )
  result = await ctx.run_node(extractor, node_input=content, use_sub_branch=True)

  evidence = _evidence_from_output(result)
  dimensions = _score_from_evidence(evidence)
  grade = EssayGrade(
      filename=filename,
      student_name=evidence.student_name,
      prompt_summary=evidence.prompt_summary,
      transcription=evidence.transcription,
      overall_score=_calculate_overall_score(dimensions),
      dimensions=dimensions,
      strengths=evidence.strengths,
      improvements=evidence.improvements,
  )
  yield Event(output=grade)


@node(rerun_on_resume=True)
async def orchestrate(ctx: Context, node_input: list[dict[str, str]]):
  """Fan out one grade_one sub-node per essay."""
  essays = node_input
  if not essays:
    yield Event(
        message=f"No .jpg/.jpeg/.png files found in {ESSAYS_DIR}."
    )
    yield Event(output=[])
    return

  yield Event(message=f"Dispatching {len(essays)} grader(s)...")
  tasks = [
      ctx.run_node(grade_one, node_input=e, use_sub_branch=True)
      for e in essays
  ]
  grades = await asyncio.gather(*tasks)
  yield Event(output=grades)


def _safe_name(name: str) -> str:
  """Make a student name safe for use as a filename segment."""
  s = (name or "").strip().replace(" ", "_")
  s = re.sub(r'[/\\:*?"<>|]+', "", s)
  return s or "unknown"


def write_report(node_input: list[EssayGrade]):
  grades = node_input
  if not grades:
    yield Event(message="Nothing graded; no report written.")
    return

  now = datetime.datetime.now().astimezone()
  file_ts = now.strftime("%Y-%m-%d_%H-%M-%S")
  display_ts = now.strftime("%Y-%m-%d %H:%M:%S")
  REPORTS_DIR.mkdir(parents=True, exist_ok=True)

  by_student: dict[str, list[EssayGrade]] = {}
  for g in grades:
    by_student.setdefault(g.student_name or "unknown", []).append(g)

  written: list[Path] = []
  for student, student_grades in by_student.items():
    report_path = REPORTS_DIR / f"{_safe_name(student)}_{file_ts}.md"

    lines: list[str] = [
        f"# Essay Grading Report — {student} — {display_ts}",
        "",
        f"Graded {len(student_grades)} essay(s) for {student}.",
        "",
        "## Summary",
        "| Filename | Score | Content | Structure | Language | Handwriting |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for g in student_grades:
      d = g.dimensions
      lines.append(
          f"| {g.filename} | {g.overall_score} | {d.content} |"
          f" {d.structure} | {d.language} | {d.handwriting} |"
      )
    lines.append("")
    lines.append("## Detailed Feedback")
    for g in student_grades:
      lines.append("")
      lines.append(f"### {g.filename}")
      lines.append(f"**Prompt:** {g.prompt_summary}")
      lines.append(f"**Overall:** {g.overall_score}/20")
      lines.append("**Strengths:**")
      for s in g.strengths:
        lines.append(f"- {s}")
      lines.append("**Improvements:**")
      for i in g.improvements:
        lines.append(f"- {i}")
      lines.append("**Transcription:**")
      lines.append("")
      for trans_line in g.transcription.splitlines() or [g.transcription]:
        lines.append(f"> {trans_line}")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written.append(report_path)

  summary = f"Wrote {len(written)} report(s):\n" + "\n".join(
      f"- {p}" for p in written
  )
  yield Event(message=summary)


root_agent = Workflow(
    name="root_agent",
    edges=[("START", list_essays, orchestrate, write_report)],
)
