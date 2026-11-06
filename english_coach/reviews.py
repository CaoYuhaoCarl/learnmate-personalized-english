"""Teacher revisions and immutable, independently exportable confirmations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException, Request

from .review_models import items_from_profiles, profiles_from_items, validate_items

MAX_REVIEW_BYTES = 2 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_review(items: list[dict]) -> dict:
    return {"revision": 1, "state": "draft", "items": deepcopy(items),
            "original_items": deepcopy(items), "needs_review": True,
            "legacy": any(item.get("legacy") for item in items),
            "confirmed_revision": None, "confirmed_at": None, "history": [],
            "updated_at": now()}


async def read_review_request(request: Request, *, save: bool = False) -> dict:
    if request.headers.get("x-learnmate-request") != "1":
        raise HTTPException(403, "请从教师校对页面提交修改。")
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(415, "请使用校对页面保存内容。")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REVIEW_BYTES:
            raise HTTPException(413, "校对内容过长，请缩短后保存。")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "校对请求格式无效。") from None
    allowed = {"base_revision", "items"} if save else {"base_revision", "reviewed"}
    if (not isinstance(payload, dict) or set(payload) - allowed
            or type(payload.get("base_revision")) is not int or payload["base_revision"] < 1):
        raise HTTPException(422, "校对版本无效，请重新读取这次作业。")
    if save and (not isinstance(payload.get("items"), list) or not 1 <= len(payload["items"]) <= 100):
        raise HTTPException(422, "校对内容无效。")
    return payload


def export_snapshot(directory: Path, snapshot: dict) -> dict:
    """Only deterministic rendering runs here; this never invokes a model."""
    from . import workflow

    manifest = {"reports": [], "pdf_failures": 0}
    label = f'教师确认版本 v{snapshot["revision"]} · {snapshot["confirmed_at"]}'
    if snapshot.get("failed_files"):
        label += f' · {len(snapshot["failed_files"])} 张图片未完成处理，本报告仅包含已校对的成功结果。'
    for event in workflow.write_report(
        snapshot["profiles"], reports_dir=directory / "reports",
        training_inputs_dir=directory / "training_inputs",
        pdf_exports_dir=directory / "reports" / "pdf_exports", open_in_wps=False,
        review_label=label,
    ):
        if isinstance(event.output, dict):
            manifest = event.output
    return manifest


class ReviewService:
    """Mutations run on the app event loop; blocking exports receive snapshots."""

    def __init__(self, store):
        self.store = store
        self.tasks: dict[tuple[str, int], asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(1)

    def recover(self, record: dict) -> bool:
        changed = False
        for version in record.get("review", {}).get("history", []):
            if version["pdf_status"] == "pending":
                version["pdf_status"] = "failed"
                changed = True
        return changed

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def review(self, record: dict) -> dict:
        if record.get("status") in {"queued", "running"}:
            raise HTTPException(409, "作业仍在处理，请完成后再校对。")
        if "review" in record:
            # A disk failure on the final export update must not leave the UI
            # waiting forever. A missing worker can be retried without the LLM.
            for version in record["review"]["history"]:
                task = self.tasks.get((record["id"], version["revision"]))
                if version["pdf_status"] == "pending" and (task is None or task.done()):
                    version["pdf_status"] = "partial" if version.get("reports") else "failed"
            return record["review"]
        items = items_from_profiles(record.get("profiles", []))
        if not items:
            raise HTTPException(404, "本次提交没有可校对的结果。")
        return initial_review(items)

    def _image_url(self, record: dict, item: dict) -> str | None:
        if any(source["input_id"] == item["input_id"] for source in record.get("inputs", [])):
            return f'/api/submissions/{record["id"]}/images/{quote(item["input_id"], safe="")}'
        return None

    def report_links(self, identifier: str, version: dict) -> list[dict]:
        base = f'/api/submissions/{identifier}/revisions/{version["revision"]}/reports/'
        return [{"filename": report["filename"],
                 "title": f'{report["title"]} · 定稿 v{version["revision"]}',
                 "url": base + quote(report["filename"], safe=""),
                 "download_url": base + quote(report["filename"], safe="") + "?download=1"}
                for report in version.get("reports", [])]

    def public(self, record: dict) -> dict:
        review = deepcopy(self.review(record))
        review["history"] = [
            {"revision": version["revision"], "confirmed_at": version["confirmed_at"],
             "pdf_status": version["pdf_status"],
             "reports": self.report_links(record["id"], version),
             "snapshot_url": f'/api/submissions/{record["id"]}/revisions/{version["revision"]}'}
            for version in review["history"]
        ]
        current = next((version for version in review["history"]
                        if version["revision"] == review["revision"]), None)
        review["pdf_status"] = current["pdf_status"] if current else "not_available"
        review["reports"] = current["reports"] if current else []
        for key in ("items", "original_items"):
            for item in review[key]:
                item["image_url"] = self._image_url(record, item)
        review["failed_files"] = [item for item in record.get("files", [])
                                   if item.get("status") != "succeeded"]
        return review

    def get(self, identifier: str) -> dict:
        return self.public(self.store.get(identifier))

    @staticmethod
    def check_revision(review: dict, revision: int):
        if review["revision"] != revision:
            raise HTTPException(409, "这份作业已在其他页面更新。当前编辑仍保留，请核对最新版本后再保存。")

    def save(self, identifier: str, payload: dict) -> dict:
        record = self.store.get(identifier)
        review = self.review(record)
        self.check_revision(review, payload["base_revision"])
        try:
            items = validate_items(payload["items"], review["items"])
            profiles = [profile.model_dump(mode="json") for profile in profiles_from_items(items)]
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "校对内容无效，请检查必填内容、日期和分值（每项 0 至 5 分）。") from None
        if items != review["items"]:
            review.update(items=items, revision=review["revision"] + 1,
                          state="draft", needs_review=True, updated_at=now())
        record.update(review=review, profiles=profiles)
        self.store._write(record)
        return self.public(record)

    def _job_dir(self, identifier: str) -> Path:
        # get() has already validated the UUID and the status manifest.
        root = self.store.root.resolve()
        directory = (root / identifier).resolve()
        if directory.parent != root:
            raise HTTPException(404, "未找到这次提交。")
        return directory

    def _inside(self, identifier: str, relative: str) -> Path:
        directory = self._job_dir(identifier)
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory):
            raise HTTPException(404, "未找到文件。")
        return path

    def confirm(self, identifier: str, payload: dict) -> dict:
        record = self.store.get(identifier)
        review = self.review(record)
        self.check_revision(review, payload["base_revision"])
        if payload.get("reviewed") is not True:
            raise HTTPException(422, "请先确认已核对所有作业内容和未完成的图片。")
        if review["state"] == "confirmed":
            return self.public(record)
        if len(self.tasks) >= 4:
            raise HTTPException(429, "报告正在导出，请稍后确认。草稿已保留。")
        confirmed_at = now()
        profiles = [profile.model_dump(mode="json") for profile in profiles_from_items(review["items"])]
        snapshot = {"revision": review["revision"], "confirmed_at": confirmed_at,
                    "items": deepcopy(review["items"]), "profiles": profiles,
                    "failed_files": [item for item in record.get("files", [])
                                     if item.get("status") != "succeeded"]}
        relative = f'confirmed/r{review["revision"]}/snapshot.json'
        path = self._inside(identifier, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A previous failed manifest write may leave an orphan snapshot. It is
        # immutable; do not replace it or make its version refer to other data.
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing["items"] != snapshot["items"]:
                raise HTTPException(409, "该版本已有确认内容，请保存为新版本后再确认。")
            snapshot = existing
            confirmed_at = snapshot["confirmed_at"]
        else:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        version = {"revision": review["revision"], "confirmed_at": confirmed_at,
                   "snapshot": relative, "pdf_status": "pending", "reports": []}
        review["history"].append(version)
        review.update(state="confirmed", needs_review=False,
                      confirmed_revision=review["revision"], confirmed_at=confirmed_at)
        record["review"] = review
        self.store._write(record)
        self._launch(identifier, version["revision"])
        return self.public(record)

    def retry(self, identifier: str, payload: dict) -> dict:
        record = self.store.get(identifier)
        review = self.review(record)
        self.check_revision(review, payload["base_revision"])
        if review["state"] != "confirmed":
            raise HTTPException(409, "请先确认当前草稿，再生成这一版本的 PDF。")
        version = next(item for item in review["history"] if item["revision"] == review["revision"])
        if version["pdf_status"] in {"pending", "ready"}:
            return self.public(record)
        if len(self.tasks) >= 4:
            raise HTTPException(429, "报告正在导出，请稍后重试。")
        version["pdf_status"] = "pending"
        self.store._write(record)
        self._launch(identifier, version["revision"])
        return self.public(record)

    def _launch(self, identifier: str, revision: int):
        key = (identifier, revision)
        task = asyncio.create_task(self._export(identifier, revision))
        self.tasks[key] = task
        def completed(finished):
            if self.tasks.get(key) is finished:
                self.tasks.pop(key, None)
            if not finished.cancelled():
                # Failures are surfaced through the retryable export state,
                # never by logging document text from exception messages.
                finished.exception()
        task.add_done_callback(completed)

    async def _export(self, identifier: str, revision: int):
        pdf_status, reports = "failed", []
        relative = f'confirmed/r{revision}/exports/{uuid4().hex}'
        try:
            async with self.semaphore:
                snapshot = self.snapshot(identifier, revision)
                directory = self._inside(identifier, relative)
                directory.mkdir(parents=True, exist_ok=False)
                async with asyncio.timeout(300):
                    manifest = await asyncio.to_thread(export_snapshot, directory, snapshot)
                for report in manifest.get("reports", []):
                    filename = report["filename"]
                    if (Path(filename).name != filename or not filename.endswith(".pdf")
                            or not (directory / "reports" / "pdf_exports" / filename).is_file()):
                        raise ValueError("Invalid export manifest")
                    reports.append({**report, "directory": relative + "/reports/pdf_exports"})
                pdf_status = ("partial" if reports else "failed") if manifest.get("pdf_failures") else ("ready" if reports else "failed")
        except asyncio.CancelledError:
            pdf_status = "failed"
            raise
        except Exception:
            # Export diagnostics may contain document text or local paths.
            pdf_status = "failed"
        finally:
            # Re-read after awaiting export: an editor may already have saved a
            # newer draft or confirmed another version while this one rendered.
            record = self.store.get(identifier)
            version = next(item for item in record["review"]["history"] if item["revision"] == revision)
            previous = version.get("reports", [])
            if reports:
                archived = {item["filename"]: item for item in version.get("archived_reports", [])}
                archived.update({item["filename"]: item for item in previous})
                version["archived_reports"] = list(archived.values())
                combined = {item.get("report_key", item["title"]): item for item in previous}
                combined.update({item.get("report_key", item["title"]): item for item in reports})
                version["reports"] = list(combined.values())
                expected = len(self.snapshot(identifier, revision)["profiles"]) + 1
                pdf_status = "ready" if len(combined) >= expected else "partial"
            elif previous:
                pdf_status = "partial"
            version["pdf_status"] = pdf_status
            try:
                self.store._write(record)
            except OSError:
                # Disk still contains the confirmed snapshot; public() exposes
                # a retryable failure once this task has finished.
                pass

    def snapshot(self, identifier: str, revision: int) -> dict:
        record = self.store.get(identifier)
        version = next((item for item in record.get("review", {}).get("history", [])
                        if item["revision"] == revision), None)
        if version is None:
            raise HTTPException(404, "未找到这个定稿版本。")
        return json.loads(self._inside(identifier, version["snapshot"]).read_text(encoding="utf-8"))

    def report_path(self, identifier: str, revision: int, filename: str) -> Path:
        record = self.store.get(identifier)
        version = next((item for item in record.get("review", {}).get("history", [])
                        if item["revision"] == revision), None)
        report = next((item for item in ((version or {}).get("reports", []) + (version or {}).get("archived_reports", []))
                       if item["filename"] == filename), None)
        if report is None or Path(filename).name != filename or not filename.endswith(".pdf"):
            raise HTTPException(404, "未找到这个版本的 PDF。")
        directory = self._inside(identifier, report["directory"])
        path = (directory / filename).resolve()
        if path.parent != directory or not path.is_file():
            raise HTTPException(404, "未找到这个版本的 PDF。")
        return path

    def image(self, identifier: str, input_id: str) -> tuple[Path, str]:
        record = self.store.get(identifier)
        source = next((item for item in record.get("inputs", []) if item["input_id"] == input_id), None)
        if source is None:
            raise HTTPException(404, "这份历史结果没有可用的原图关联。")
        path = self._inside(identifier, source["path"])
        if not path.is_file() or path.parent != self._inside(identifier, "inputs"):
            raise HTTPException(404, "未找到原图。")
        return path, source["mime"]
