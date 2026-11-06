"""Authenticated teacher workspace, submission API, and existing ADK Web."""

from __future__ import annotations

import html
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic
from fastapi.security import HTTPBasicCredentials
from starlette.requests import HTTPConnection
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.agent_loader import AgentLoader
from google.adk.cli.utils import envs

from .constants import PDF_EXPORTS_DIRNAME
from .constants import PROJECT_DIR
from .constants import REPORTS_DIR
from .submissions import Processor, SubmissionStore, UploadLimitMiddleware, read_upload
from .reviews import read_review_request

REPORTS_USERNAME_ENV = "REPORTS_USERNAME"
REPORTS_PASSWORD_ENV = "REPORTS_PASSWORD"
ADK_APP_NAME = "english_coach"

security = HTTPBasic(auto_error=False)


class WebSocketAuthMiddleware:
    """ADK live sockets must share the HTTP app's authentication boundary."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            connection = HTTPConnection(scope)
            try:
                _require_reports_auth(await security(connection))
                _check_origin(connection)
            except HTTPException:
                await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


def _check_origin(connection: HTTPConnection):
    origin = connection.headers.get("origin")
    scheme = {"ws": "http", "wss": "https"}.get(connection.url.scheme, connection.url.scheme)
    try:
        cross_origin = origin and (urlsplit(origin).scheme, urlsplit(origin).netloc) != (scheme, connection.url.netloc)
    except ValueError:
        cross_origin = True
    if connection.headers.get("sec-fetch-site") == "cross-site" or cross_origin:
        raise HTTPException(403, "请从本服务的页面提交请求。")


class EnglishCoachAgentLoader(AgentLoader):
    def list_agents(self) -> list[str]:
        return [ADK_APP_NAME]

    def load_agent(self, agent_name: str):
        if agent_name != ADK_APP_NAME:
            raise ValueError(f"Unknown ADK app: {agent_name}")
        return super().load_agent(agent_name)


def _no_store_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store"}


def _configured_credentials() -> tuple[str, str]:
    username = os.environ.get(REPORTS_USERNAME_ENV, "teacher")
    password = os.environ.get(REPORTS_PASSWORD_ENV, "")
    if not password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{REPORTS_PASSWORD_ENV} is not configured.",
            headers=_no_store_headers(),
        )
    return username, password


def _require_reports_auth(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    username, password = _configured_credentials()
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Basic", **_no_store_headers()},
        )

    valid_username = secrets.compare_digest(credentials.username.encode(), username.encode())
    valid_password = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Basic", **_no_store_headers()},
        )


def _pdf_exports_dir(reports_dir: Path) -> Path:
    return reports_dir / PDF_EXPORTS_DIRNAME


def _pdf_reports(reports_dir: Path) -> list[Path]:
    pdf_dir = _pdf_exports_dir(reports_dir)
    if not pdf_dir.is_dir():
        return []
    return sorted(
        (path for path in pdf_dir.iterdir() if path.is_file() and path.suffix == ".pdf"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _resolve_pdf(reports_dir: Path, filename: str) -> Path:
    if Path(filename).name != filename or not filename.endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    pdf_dir = _pdf_exports_dir(reports_dir).resolve()
    pdf_path = (pdf_dir / filename).resolve()
    if pdf_path.parent != pdf_dir or not pdf_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return pdf_path


def _reports_html(reports_dir: Path, store: SubmissionStore | None = None) -> str:
    reports = [
        (report.stat().st_mtime, f"历史结果（未经教师确认）· {report.name}", f"/reports/{quote(report.name, safe='')}")
        for report in _pdf_reports(reports_dir)
    ]
    if store is not None:
        for submission in store.recent(limit=None):
            for report in submission["reports"] + submission.get("historical_reports", []):
                reports.append((datetime.fromisoformat(submission["created_at"]).timestamp(),
                                f'{report["title"]} · {report["filename"]}', report["url"]))
    reports.sort(key=lambda report: report[0], reverse=True)
    if not reports:
        body = "<p>No PDF reports yet.</p>"
    else:
        rows = []
        for _, title, url in reports:
            filename = html.escape(title)
            url = html.escape(url, quote=True)
            rows.append(
                "<tr>"
                f"<td>{filename}</td>"
                f'<td><a href="{url}">View / Print</a></td>'
                f'<td><a href="{url}?download=1">Download</a></td>'
                "</tr>"
            )
        body = (
            "<table>"
            "<thead><tr><th>Report</th><th>Open</th><th>Save</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )

    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>PDF Reports</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:960px;margin:32px auto;padding:0 20px;color:#1f2937}"
        "h1{font-size:28px;line-height:1.2;margin:0 0 20px}"
        "table{width:100%;border-collapse:collapse}"
        "th,td{text-align:left;border-bottom:1px solid #e5e7eb;padding:12px 8px}"
        "th{font-size:13px;color:#4b5563}"
        "a{color:#0969da;text-decoration:none}"
        "a:hover{text-decoration:underline}"
        "p{color:#4b5563}"
        "</style>"
        "</head>"
        "<body>"
        '<p><a href="/">← 返回教师工作台</a></p>'
        "<h1>PDF Reports</h1>"
        f"{body}"
        "</body>"
        "</html>"
    )


def attach_report_routes(app: FastAPI, *, reports_dir: Path = REPORTS_DIR,
                         store: SubmissionStore | None = None) -> FastAPI:
    @app.get("/reports", response_class=HTMLResponse)
    def list_reports(_auth: None = Depends(_require_reports_auth)) -> HTMLResponse:
        return HTMLResponse(_reports_html(reports_dir, store), headers=_no_store_headers())

    @app.get("/reports/{filename}")
    def open_report(
        filename: str,
        download: bool = Query(False),
        _auth: None = Depends(_require_reports_auth),
    ) -> FileResponse:
        pdf_path = _resolve_pdf(reports_dir, filename)
        disposition = "attachment" if download else "inline"
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=pdf_path.name,
            headers=_no_store_headers(),
            content_disposition_type=disposition,
        )

    return app


def attach_teacher_routes(app: FastAPI, store: SubmissionStore) -> None:
    static_dir = Path(__file__).parent / "static"
    app.mount("/teacher-assets", StaticFiles(directory=static_dir), name="teacher-assets")

    @app.get("/", response_class=HTMLResponse)
    def teacher_home():
        return FileResponse(static_dir / "teacher.html", media_type="text/html", headers=_no_store_headers())

    @app.post("/api/submissions")
    async def submit(request: Request):
        identifier, language, files = await read_upload(request)
        record, created = store.submit(identifier, language, files)
        return JSONResponse(record, status_code=202 if created else 200, headers=_no_store_headers())

    @app.get("/api/submissions")
    def recent_submissions():
        return JSONResponse({"submissions": store.recent()}, headers=_no_store_headers())

    @app.get("/api/submissions/{identifier}")
    def submission(identifier: str):
        return JSONResponse(store.public(store.get(identifier)), headers=_no_store_headers())

    @app.get("/api/submissions/{identifier}/review")
    async def get_review(identifier: str):
        return JSONResponse(store.reviews.get(identifier), headers=_no_store_headers())

    @app.put("/api/submissions/{identifier}/review")
    async def save_review(identifier: str, request: Request):
        payload = await read_review_request(request, save=True)
        return JSONResponse(store.reviews.save(identifier, payload), headers=_no_store_headers())

    @app.post("/api/submissions/{identifier}/review/confirm")
    async def confirm_review(identifier: str, request: Request):
        payload = await read_review_request(request)
        return JSONResponse(store.reviews.confirm(identifier, payload), status_code=202, headers=_no_store_headers())

    @app.post("/api/submissions/{identifier}/review/export")
    async def retry_review_export(identifier: str, request: Request):
        payload = await read_review_request(request)
        return JSONResponse(store.reviews.retry(identifier, payload), status_code=202, headers=_no_store_headers())

    @app.get("/api/submissions/{identifier}/images/{input_id}")
    async def review_image(identifier: str, input_id: str):
        path, mime = store.reviews.image(identifier, input_id)
        return FileResponse(path, media_type=mime, headers=_no_store_headers())

    @app.get("/api/submissions/{identifier}/revisions/{revision}")
    async def review_snapshot(identifier: str, revision: int):
        return JSONResponse(store.reviews.snapshot(identifier, revision), headers=_no_store_headers())

    @app.get("/api/submissions/{identifier}/revisions/{revision}/reports/{filename}")
    async def review_report(identifier: str, revision: int, filename: str, download: bool = Query(False)):
        path = store.reviews.report_path(identifier, revision, filename)
        return FileResponse(path, media_type="application/pdf", filename=path.name,
                            content_disposition_type="attachment" if download else "inline",
                            headers=_no_store_headers())

    @app.get("/api/submissions/{identifier}/reports/{filename}")
    def submission_report(identifier: str, filename: str, download: bool = Query(False)):
        path = store.report_path(identifier, filename)
        return FileResponse(path, media_type="application/pdf", filename=path.name,
                            content_disposition_type="attachment" if download else "inline",
                            headers=_no_store_headers())


def create_app(*, data_dir: Path | None = None, processor: Processor | None = None,
               include_adk: bool = True) -> FastAPI:
    from .submission_runner import process_submission

    envs.load_dotenv_for_agent(ADK_APP_NAME, str(PROJECT_DIR))
    store = SubmissionStore(
        data_dir if data_dir is not None else Path(os.environ.get("LEARNMATE_WEB_DATA_DIR", str(REPORTS_DIR / "web"))),
        processor or process_submission,
    )
    adk_app = get_fast_api_app(
        agents_dir=str(PROJECT_DIR),
        agent_loader=EnglishCoachAgentLoader(str(PROJECT_DIR)),
        session_service_uri="memory://",
        artifact_service_uri="memory://",
        web=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload_agents=False,
    ) if include_adk else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.start()
        try:
            if adk_app is not None:
                async with adk_app.router.lifespan_context(adk_app):
                    yield
            else:
                yield
        finally:
            await store.close()

    app = FastAPI(title="LearnMate", lifespan=lifespan)
    app.state.submissions = store
    app.add_middleware(UploadLimitMiddleware)
    app.add_middleware(WebSocketAuthMiddleware)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        try:
            _require_reports_auth(await security(request))
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                _check_origin(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers={**_no_store_headers(), **(exc.headers or {})})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
                "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    attach_teacher_routes(app, store)
    attach_report_routes(app, store=store)
    if adk_app is not None:
        app.mount("/", adk_app)
    return app


app = create_app()
