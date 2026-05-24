"""
Workflow: grade handwritten student essays.

Reads ./essays/*.{jpg,jpeg,png}, sends each image to a Gemini grader as inline
multimodal Parts, collects structured EssayGrade results in parallel, and
writes a markdown aggregate to ./reports/<UTC-timestamp>.md.

Composition (left-to-right execution order):
    list_essays -> orchestrate -> write_report
                       └─ ctx.run_node + asyncio.gather over grade_one ─┐
                                            grade_one -> grader Agent ──┘

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
import json
from pathlib import Path

from google.adk import Agent
from google.adk import Context
from google.adk import Event
from google.adk import Workflow
from google.adk.workflow import node
from google.adk.workflow import RetryConfig
from google.genai import types
from pydantic import BaseModel

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


class EssayGrade(BaseModel):
  filename: str
  prompt_summary: str
  transcription: str
  overall_score: int
  dimensions: DimensionScores
  strengths: list[str]
  improvements: list[str]


grader = Agent(
    name="grader",
    model="gemini-flash-latest",
    instruction=(
        "You are an experienced writing teacher. The user message contains a"
        " filename label followed by a single image. The image shows, top to"
        " bottom, the printed essay prompt and the student's handwritten"
        " response.\n\n"
        "Read both. Return a structured grade.\n\n"
        "Score each dimension 0-5:\n"
        "  content     - relevance to the prompt\n"
        "  structure   - coherence and organization\n"
        "  language    - accuracy and fluency\n"
        "  handwriting - legibility and presentation\n\n"
        "overall_score is 0-20, weighted 8/5/5/2 across content / structure"
        " / language / handwriting, rounded to the nearest integer.\n\n"
        "prompt_summary: one sentence describing what the essay was meant to"
        " address.\n"
        "transcription: the student's handwritten response transcribed"
        " verbatim. Preserve their original words, line breaks, spelling, and"
        " grammar — do NOT silently correct mistakes. Use \\n for line"
        " breaks. Do not include the printed prompt at the top of the image.\n"
        "strengths: 1-3 short bullets.\n"
        "improvements: 1-3 actionable bullets.\n"
        "filename: copy the filename label from the user message verbatim."
    ),
    output_schema=EssayGrade,
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
  result = await ctx.run_node(grader, node_input=content, use_sub_branch=True)

  if isinstance(result, EssayGrade):
    grade = result
  elif isinstance(result, dict):
    grade = EssayGrade(**result)
  else:
    grade = EssayGrade(**json.loads(result))
  # Trust the loader for filename; the LLM might paraphrase it.
  grade = grade.model_copy(update={"filename": filename})
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


def write_report(node_input: list[EssayGrade]):
  grades = node_input
  if not grades:
    yield Event(message="Nothing graded; no report written.")
    return

  ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
  REPORTS_DIR.mkdir(parents=True, exist_ok=True)
  report_path = REPORTS_DIR / f"{ts}.md"

  lines: list[str] = [
      f"# Essay Grading Report — {ts}",
      "",
      f"Graded {len(grades)} essays.",
      "",
      "## Summary",
      "| Filename | Score | Content | Structure | Language | Handwriting |",
      "| --- | --- | --- | --- | --- | --- |",
  ]
  for g in grades:
    d = g.dimensions
    lines.append(
        f"| {g.filename} | {g.overall_score} | {d.content} |"
        f" {d.structure} | {d.language} | {d.handwriting} |"
    )
  lines.append("")
  lines.append("## Detailed Feedback")
  for g in grades:
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
  yield Event(message=f"Wrote report -> {report_path}")


root_agent = Workflow(
    name="root_agent",
    edges=[("START", list_essays, orchestrate, write_report)],
)
