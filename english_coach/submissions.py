"""Small, disk-backed submission runner for the single-teacher web app."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile

from .config import MIME_BY_SUFFIX
from .reviews import ReviewService, initial_review

MAX_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_TOTAL_BYTES + 1024 * 1024
ACTIVE_STATUSES = {"queued", "running"}
Processor = Callable[[Path, list[dict], Callable[[dict], None]], Awaitable[dict]]


class UploadLimitMiddleware:
    """Bound multipart bodies, including chunked uploads, before parsing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/api/submissions":
            return await self.app(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "一次上传的图片总大小不能超过 30 MB。")
            return message

        await self.app(scope, limited_receive, send)


def canonical_id(value: str) -> str:
    try:
        identifier = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(422, "提交编号无效，请重新选择作业。") from None
    if identifier != value:
        raise HTTPException(422, "提交编号无效，请重新选择作业。")
    return identifier


def _image_matches(data: bytes, mime: str) -> bool:
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if mime in {"image/heic", "image/heif"}:
        return data[4:8] == b"ftyp" and any(
            brand in data[8:64] for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")
        )
    return False


async def read_upload(request: Request) -> tuple[str, str, list[dict]]:
    # The custom header cannot be sent by a cross-origin HTML form.
    if request.headers.get("x-learnmate-request") != "1":
        raise HTTPException(403, "请从作业页面提交图片。")
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "上传请求无效。") from None
    if length < 0 or length > MAX_REQUEST_BYTES:
        raise HTTPException(413, "一次上传的图片总大小不能超过 30 MB。")
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(415, "请选择图片后提交。")

    async with request.form(max_files=MAX_FILES, max_fields=2) as form:
        request_id = canonical_id(form.get("request_id", ""))
        language = form.get("feedback_language", "zh-Hans")
        if not isinstance(language, str) or language not in {"zh-Hans", "en"}:
            raise HTTPException(422, "请选择中文或英文反馈。")
        uploads = form.getlist("files")
        if not uploads or len(uploads) > MAX_FILES:
            raise HTTPException(422, "请上传 1 至 10 张作业图片。")
        files = []
        total = 0
        for upload in uploads:
            if not isinstance(upload, UploadFile):
                raise HTTPException(422, "上传内容必须是图片文件。")
            filename = upload.filename or ""
            if (not filename or len(filename.encode()) > 200 or "/" in filename or "\\" in filename
                    or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
                raise HTTPException(422, "图片文件名无效，请重命名后再上传。")
            mime = MIME_BY_SUFFIX.get(Path(filename).suffix.lower())
            if not mime:
                raise HTTPException(422, "仅支持 JPG、PNG、WEBP、HEIC 和 HEIF 图片。")
            data = await upload.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise HTTPException(413, "每张图片不超过 10 MB，一次上传总计不超过 30 MB。")
            if not _image_matches(data, mime):
                raise HTTPException(422, "图片为空或格式与扩展名不符，请检查文件。")
            files.append({"filename": filename, "mime": mime, "data": data})
    return request_id, language, files


class SubmissionStore:
    """One process, one running batch; completed results survive restarts."""

    def __init__(self, root: Path, processor: Processor, *, timeout: float = 900):
        self.root = root
        self.processor = processor
        self.timeout = timeout
        self.tasks: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(1)
        self._lock_file = None
        self.reviews = ReviewService(self)

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.root / ".worker.lock").open("a")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("The teacher app supports one worker per data directory.") from None
        for path in self.root.glob("*/status.json"):
            record = self._read_path(path)
            changed = bool(record) and self.reviews.recover(record)
            if record and record.get("status") in ACTIVE_STATUSES:
                record.update(status="interrupted", stage="finished",
                              message="服务在处理过程中重启。本次处理已中断，请检查已有结果后重新提交。")
                changed = True
            if changed:
                self._write(record)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.reviews.close()
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    @staticmethod
    def _read_path(path: Path) -> dict | None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("id") == path.parent.name:
                canonical_id(record["id"])
                return record
        except (OSError, ValueError, HTTPException):
            pass
        return None

    def _write(self, record: dict):
        path = self.root / record["id"] / "status.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def get(self, identifier: str) -> dict:
        canonical_id(identifier)
        record = self._read_path(self.root / identifier / "status.json")
        if record is None:
            raise HTTPException(404, "未找到这次提交，请返回页面重新选择作业。")
        return record

    def public(self, record: dict, *, summary: bool = False) -> dict:
        result = {key: value for key, value in record.items()
                  if key not in {"fingerprint", "inputs", "original_results", "review"}}
        result["reports"] = [
            {**report, "url": f'/api/submissions/{record["id"]}/reports/{quote(report["filename"], safe="")}',
             "download_url": f'/api/submissions/{record["id"]}/reports/{quote(report["filename"], safe="")}?download=1'}
            for report in record.get("reports", [])
        ]
        if record.get("review"):
            legacy_reports = [{**report, "title": f'历史结果（未经教师确认）· {report["title"]}'}
                              for report in result["reports"]]
            review = self.reviews.public(record)
            result.update(review_available=True, review_state=review["state"],
                          review_revision=review["revision"], confirmed_revision=review["confirmed_revision"],
                          pdf_status=review["pdf_status"], reports=review["reports"])
            result["historical_reports"] = legacy_reports + [
                {**report, "title": f'旧定稿 · {report["title"]}'}
                for version in review["history"] if version["revision"] != review["revision"]
                for report in version["reports"]
            ]
        else:
            try:
                review = self.reviews.review(record)
            except HTTPException:
                review = None
            result["review_available"] = bool(review)
            if review:
                result.update(review_state="draft", review_revision=review["revision"], confirmed_revision=None)
            # Existing reports remain accessible, explicitly labelled as legacy.
            result["reports"] = [{**report, "title": f'历史结果（未经教师确认）· {report["title"]}'}
                                 for report in result["reports"]]
        if summary:
            result.pop("profiles", None)
        return result

    def recent(self, limit: int | None = 20) -> list[dict]:
        records = (self._read_path(path) for path in self.root.glob("*/status.json"))
        ordered = sorted((record for record in records if record),
                         key=lambda record: record.get("created_at", ""), reverse=True)
        return [self.public(record, summary=True) for record in ordered[:limit]]

    def submit(self, identifier: str, language: str, files: list[dict]) -> tuple[dict, bool]:
        canonical_id(identifier)
        digest = hashlib.sha256(language.encode())
        for item in files:
            for value in (item["filename"].encode(), item["mime"].encode(), item["data"]):
                digest.update(len(value).to_bytes(8, "big"))
                digest.update(value)
        fingerprint = digest.hexdigest()
        job_dir = self.root / identifier
        if job_dir.exists():
            record = self.get(identifier)
            if record.get("fingerprint") != fingerprint:
                raise HTTPException(409, "这个提交编号已经用于另一组作业，请开始新的提交。")
            return self.public(record), False
        if len(self.tasks) >= 4:
            raise HTTPException(429, "目前有多批作业正在等待处理，请稍后再提交。")
        job_dir.mkdir(parents=True)
        upload_dir = job_dir / "inputs"
        upload_dir.mkdir()
        inputs = []
        sources = []
        for index, item in enumerate(files, start=1):
            path = upload_dir / f'{index:02d}_{item["filename"]}'
            path.write_bytes(item["data"])
            input_id = str(uuid4())
            inputs.append({"input_id": input_id, "path": str(path), "filename": item["filename"],
                           "mime": item["mime"], "feedback_language": language})
            sources.append({"input_id": input_id, "path": str(path.relative_to(job_dir)),
                            "filename": item["filename"], "mime": item["mime"]})
        record = {"id": identifier, "fingerprint": fingerprint, "status": "queued", "stage": "queued",
                  "completed": 0, "total": len(files), "feedback_language": language,
                  "message": "作业已接收，等待处理。", "profiles": [], "reports": [], "pdf_status": "not_available",
                  "files": [{"filename": item["filename"], "status": "queued", "message": "等待处理"} for item in files],
                  "inputs": sources,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        self._write(record)
        task = asyncio.create_task(self._run(record, inputs))
        self.tasks[identifier] = task
        task.add_done_callback(lambda finished: self.tasks.pop(identifier, None))
        return self.public(record), True

    async def _run(self, record: dict, inputs: list[dict]):
        def update(progress):
            record.update({key: value for key, value in progress.items() if key in {"stage", "completed"}})
            record["message"] = "图片处理完成，正在整理校对草稿。" if record["stage"] in {"reporting", "reviewing"} else "正在识别和分析作业，请稍候。"
            self._write(record)

        try:
            async with self.semaphore:
                record.update(status="running", stage="processing", message="正在识别和分析作业，请稍候。")
                self._write(record)
                async with asyncio.timeout(self.timeout):
                    outcome = await self.processor(self.root / record["id"], inputs, update)
                record.update({key: value for key, value in outcome.items() if key in {
                    "status", "completed", "profiles", "files", "reports", "pdf_status", "message"
                }})
                if outcome.get("review_items"):
                    record["review"] = initial_review(outcome["review_items"])
                    record["original_results"] = outcome.get("original_results", [])
                    record["reports"] = []
                    record["pdf_status"] = "not_available"
                record["stage"] = "finished"
        except asyncio.CancelledError:
            record.update(status="interrupted", stage="finished", message="服务已停止，本次处理已中断。请检查结果后重新提交。")
        except TimeoutError:
            record.update(status="failed", stage="finished", message="处理时间过长，已停止等待。请减少图片数量后重新提交。")
        except Exception:
            # SDK exceptions can contain image text, credentials, or local paths.
            record.update(status="failed", stage="finished", message="作业处理未完成。请检查模型配置和服务连接后重新提交。")
        finally:
            if record["status"] in {"failed", "interrupted"}:
                for item in record["files"]:
                    if item["status"] in {"queued", "running"}:
                        item.update(status="failed", message="未完成处理")
            self._write(record)

    def report_path(self, identifier: str, filename: str) -> Path:
        record = self.get(identifier)
        allowed = {report["filename"] for report in record.get("reports", [])}
        if filename not in allowed or Path(filename).name != filename or not filename.endswith(".pdf"):
            raise HTTPException(404, "未找到报告。")
        job_dir = (self.root / identifier).resolve()
        directory = (job_dir / "reports" / "pdf_exports").resolve()
        if job_dir.parent != self.root.resolve() or not directory.is_relative_to(job_dir):
            raise HTTPException(404, "未找到报告。")
        path = (directory / filename).resolve()
        if path.parent != directory or not path.is_file():
            raise HTTPException(404, "未找到报告。")
        return path
