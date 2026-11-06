"""Run an explicit Web submission without scanning shared input directories."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from google.adk import Runner, Workflow
from google.adk.sessions import InMemorySessionService
from google.genai import types

from . import workflow
from .review_models import items_from_results, profiles_from_items
from .schemas import InputProcessingResult


def _file_outcome(result: InputProcessingResult, original_name: str) -> dict[str, str]:
    success = result.feedback is not None or result.grammar_training is not None
    if success:
        message = "已生成反馈。"
    elif (result.skipped_reason or "").startswith("Processing failed"):
        message = "图片处理失败，请检查图片清晰度或稍后重新提交。"
    else:
        message = "未识别为作文或语法作业，请检查图片内容后重新提交。"
    return {
        "filename": original_name,
        "status": "succeeded" if success else "failed",
        "message": message,
    }


async def process_submission(
    job_dir: Path,
    inputs: list[dict[str, str]],
    on_update: Callable[[dict], None],
) -> dict:
    """Process only the supplied files, keeping all artifacts inside ``job_dir``.

    The API owns upload validation, admission control and persistence.
    A fresh in-memory ADK session is used only for this invocation; public status
    is reported separately, without exposing internal prompts or error text.
    """
    if not inputs:
        raise ValueError("至少选择一张作业图片。")
    job_dir = Path(job_dir).resolve()
    explicit_inputs = []
    for index, item in enumerate(inputs, start=1):
        path = Path(item["path"]).resolve()
        if not path.is_relative_to(job_dir) or not path.is_file():
            raise ValueError("上传文件不在本次任务目录中。")
        explicit_inputs.append({**item, "path": str(path), "input_id": item.get("input_id", f"input-{index}")})

    total = len(explicit_inputs)
    completed_paths: set[str] = set()
    outcome: dict = {}

    def uploaded_inputs(node_input: object) -> list[dict[str, str]]:
        return explicit_inputs

    async def prepare_draft(node_input: list[InputProcessingResult]):
        results = [InputProcessingResult.model_validate(item) for item in node_input]
        files = [
            _file_outcome(result, item["filename"])
            for result, item in zip(results, explicit_inputs, strict=True)
        ]
        # Failed extraction can contain provider diagnostics in skipped_reason.
        # Keep those diagnostics out of the teacher-facing result and reports.
        successful = [
            result for result in results
            if result.feedback is not None or result.grammar_training is not None
        ]
        items = items_from_results(results, [item["input_id"] for item in explicit_inputs])
        profiles = profiles_from_items(items)
        on_update({"stage": "reviewing", "completed": total, "total": total})
        original_results = []
        for result, source in zip(results, explicit_inputs, strict=True):
            if result not in successful:
                continue
            raw = result.model_dump(mode="json")
            raw["input_id"] = source["input_id"]
            raw["source_path"] = str(Path(source["path"]).relative_to(job_dir))
            original_results.append(raw)

        success_count = len(successful)
        status = "succeeded" if success_count == total else "partial" if success_count else "failed"
        message = {
            "succeeded": "作业草稿已生成，请校对并确认后导出正式报告。",
            "partial": "部分图片未能处理；已保留成功图片的草稿，请核对逐项结果。",
            "failed": "本次图片均未生成反馈，请检查图片后重新提交。",
        }[status]
        outcome.update({
            "status": status,
            "completed": total,
            "total": total,
            "profiles": [profile.model_dump(mode="json") for profile in profiles],
            "review_items": items,
            "original_results": original_results,
            "files": files,
            "reports": [],
            "pdf_status": "not_available",
            "message": message,
        })
        return {"completed": total}

    submission = Workflow(
        name="submission",
        edges=[("START", uploaded_inputs, workflow.orchestrate, prepare_draft)],
    )
    session_service = InMemorySessionService()
    session_id = uuid4().hex
    await session_service.create_session(
        app_name="learnmate_portal", user_id="teacher", session_id=session_id,
    )
    runner = Runner(
        node=submission, app_name="learnmate_portal", session_service=session_service,
    )
    on_update({"stage": "processing", "completed": 0, "total": total})
    try:
        async for event in runner.run_async(
            user_id="teacher",
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text="Process this submission.")]),
        ):
            if event.node_info.name == "process_one_input" and event.output is not None:
                result = InputProcessingResult.model_validate(event.output)
                completed_paths.add(result.source_path)
                on_update({
                    "stage": "processing", "completed": len(completed_paths), "total": total,
                })
    finally:
        await runner.close()
    if not outcome:
        raise RuntimeError("本次处理没有返回结果，请重新提交。")
    return outcome
