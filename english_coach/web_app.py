"""FastAPI entrypoint that adds teacher-friendly PDF report routes to ADK Web."""

from __future__ import annotations

import html
import os
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic
from fastapi.security import HTTPBasicCredentials
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.agent_loader import AgentLoader

from . import history_store
from . import report_layout

PACKAGE_DIR = Path(__file__).parent
PROJECT_DIR = PACKAGE_DIR.parent
REPORTS_DIR = history_store.resolve_reports_dir()
LEARNMATE_LOGO_PATH = PACKAGE_DIR / "learnmate_logo.svg"
PRISM_THEME_PATHS = {
    "/dev-ui/prism-dark.css": PACKAGE_DIR / "prism-dark.css",
    "/dev-ui/prism-light.css": PACKAGE_DIR / "prism-light.css",
}
REPORTS_USERNAME_ENV = "REPORTS_USERNAME"
REPORTS_PASSWORD_ENV = "REPORTS_PASSWORD"
ADK_APP_NAME = "english_coach"

security = HTTPBasic(auto_error=False)


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

    valid_username = secrets.compare_digest(credentials.username, username)
    valid_password = secrets.compare_digest(credentials.password, password)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Basic", **_no_store_headers()},
        )


def _pdf_reports(reports_dir: Path) -> list[Path]:
    reports_by_name: dict[str, Path] = {}
    for pdf_dir in report_layout.readable_pdf_dirs(reports_dir):
        if not pdf_dir.is_dir():
            continue
        for path in pdf_dir.iterdir():
            if path.is_file() and path.suffix == ".pdf":
                reports_by_name.setdefault(path.name, path)
    return sorted(
        reports_by_name.values(),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _resolve_pdf(reports_dir: Path, filename: str) -> Path:
    if Path(filename).name != filename or not filename.endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    for directory in report_layout.readable_pdf_dirs(reports_dir):
        pdf_dir = directory.resolve()
        pdf_path = (pdf_dir / filename).resolve()
        if pdf_path.parent == pdf_dir and pdf_path.is_file():
            return pdf_path
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _page_shell(title: str, body: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:960px;margin:32px auto;padding:0 20px;color:#1f2937}"
        "h1{font-size:28px;line-height:1.2;margin:0 0 20px}"
        "h2{font-size:18px;margin:28px 0 12px}"
        "table{width:100%;border-collapse:collapse;margin-bottom:8px}"
        "th,td{text-align:left;border-bottom:1px solid #e5e7eb;padding:12px 8px}"
        "th{font-size:13px;color:#4b5563}"
        "a{color:#0969da;text-decoration:none}"
        "a:hover{text-decoration:underline}"
        "p{color:#4b5563}"
        ".muted{color:#6b7280;font-size:13px}"
        ".sandbox{background:#fef3c7;border:1px solid #f59e0b;border-radius:6px;"
        "padding:10px 14px;color:#92400e;font-size:14px}"
        "</style>"
        "</head>"
        "<body>"
        f"{body}"
        "</body>"
        "</html>"
    )


def _sandbox_banner() -> str:
    root = history_store.data_root()
    if root is None:
        return ""
    return (
        '<p class="sandbox">Sandbox mode: reading and writing data under '
        f"{html.escape(str(root))} ({history_store.DATA_ROOT_ENV}). "
        "The real student records are untouched.</p>"
    )


def _reports_html(reports_dir: Path) -> str:
    reports = _pdf_reports(reports_dir)
    if not reports:
        body = "<p>No PDF reports yet.</p>"
    else:
        rows = []
        for report in reports:
            filename = html.escape(report.name)
            rows.append(
                "<tr>"
                f"<td>{filename}</td>"
                f'<td><a href="/reports/{filename}">View / Print</a></td>'
                f'<td><a href="/reports/{filename}?download=1">Download</a></td>'
                "</tr>"
            )
        body = (
            "<table>"
            "<thead><tr><th>Report</th><th>Open</th><th>Save</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )

    return _page_shell("PDF Reports", f"<h1>PDF Reports</h1>{_sandbox_banner()}{body}")


def _students_html() -> str:
    students = history_store.list_students()
    if not students:
        body = "<p>No student history recorded yet.</p>"
    else:
        rows = []
        for student in students:
            name = html.escape(student["name"])
            href = quote(student["name"])
            avg_score = student["avg_overall_score"]
            avg_text = f"{avg_score:.1f}/20" if avg_score is not None else "-"
            external_id = student["external_id"]
            id_text = str(external_id) if external_id is not None else "-"
            rows.append(
                "<tr>"
                f'<td><a href="/students/{href}">{name}</a></td>'
                f"<td>{id_text}</td>"
                f"<td>{student['submission_count']}</td>"
                f"<td>{avg_text}</td>"
                f"<td>{html.escape(student['last_seen_at'] or '')}</td>"
                "</tr>"
            )
        body = (
            "<table>"
            "<thead><tr><th>Student</th><th>ID</th><th>Submissions</th>"
            "<th>Avg Score</th><th>Last Seen</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )

    return _page_shell("Students", f"<h1>Students</h1>{_sandbox_banner()}{body}")


def _student_detail_html(name: str) -> str | None:
    detail = history_store.get_student_history(name)
    if detail is None:
        return None

    display_name = html.escape(detail["name"])
    submission_rows = []
    for submission in detail["submissions"]:
        score = submission["overall_score"]
        score_text = f"{score:.1f}/20" if score is not None else "-"
        date_text = submission["submission_date"] or submission["recorded_at"]
        submission_rows.append(
            "<tr>"
            f"<td>{html.escape(date_text or '')}</td>"
            f"<td>{html.escape(submission['category'])}</td>"
            f"<td>{html.escape(submission['filename'])}</td>"
            f"<td>{score_text}</td>"
            "</tr>"
        )
    submissions_body = (
        "<table>"
        "<thead><tr><th>Date</th><th>Type</th><th>File</th><th>Score</th></tr></thead>"
        f"<tbody>{''.join(submission_rows)}</tbody>"
        "</table>"
        if submission_rows
        else "<p>No submissions recorded yet.</p>"
    )

    mistake_sections = []
    for bucket in detail["mistakes_by_skill"]:
        examples = "".join(
            "<li>"
            f"<strong>{html.escape(example['evidence'] or '')}</strong> "
            f"&rarr; {html.escape(example['suggested_fix'] or '')}"
            f"<div class=\"muted\">{html.escape(example['explanation'] or '')}</div>"
            "</li>"
            for example in bucket["examples"]
        )
        mistake_sections.append(
            f"<h2>{html.escape(bucket['skill_tag'])} ({bucket['count']})</h2>"
            f"<ul>{examples}</ul>"
        )
    mistakes_body = (
        "".join(mistake_sections)
        if mistake_sections
        else "<p>No recurring mistakes recorded yet.</p>"
    )

    external_id = detail["external_id"]
    roster_line = (
        f'<p class="muted">Roster id: {external_id}'
        f' (class {html.escape(detail["external_class"] or "-")})</p>'
        if external_id is not None
        else ""
    )

    body = (
        f"<h1>{display_name}</h1>"
        f"{_sandbox_banner()}"
        f"{roster_line}"
        f'<p class="muted">First seen {html.escape(detail["first_seen_at"])} '
        f'&middot; Last seen {html.escape(detail["last_seen_at"])}</p>'
        "<h2>Score History</h2>"
        f"{submissions_body}"
        "<h2>Recurring Mistakes</h2>"
        f"{mistakes_body}"
    )
    return _page_shell(f"{detail['name']} - History", body)


def attach_report_routes(app: FastAPI, *, reports_dir: Path = REPORTS_DIR) -> FastAPI:
    @app.get("/reports", response_class=HTMLResponse)
    def list_reports(_auth: None = Depends(_require_reports_auth)) -> HTMLResponse:
        return HTMLResponse(_reports_html(reports_dir), headers=_no_store_headers())

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


def attach_history_routes(app: FastAPI) -> FastAPI:
    @app.get("/students", response_class=HTMLResponse)
    def list_students(_auth: None = Depends(_require_reports_auth)) -> HTMLResponse:
        return HTMLResponse(_students_html(), headers=_no_store_headers())

    @app.get("/students/{name}", response_class=HTMLResponse)
    def student_detail(
        name: str, _auth: None = Depends(_require_reports_auth)
    ) -> HTMLResponse:
        page = _student_detail_html(name)
        if page is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return HTMLResponse(page, headers=_no_store_headers())

    return app


def attach_brand_routes(app: FastAPI) -> FastAPI:
    @app.middleware("http")
    async def serve_prism_theme(request: Request, call_next):
        theme_path = PRISM_THEME_PATHS.get(request.url.path)
        if theme_path is not None:
            return FileResponse(
                theme_path,
                media_type="text/css",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        return await call_next(request)

    @app.get("/learnmate-logo.svg", include_in_schema=False)
    def learnmate_logo() -> FileResponse:
        return FileResponse(
            LEARNMATE_LOGO_PATH,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return app


def create_app() -> FastAPI:
    app = get_fast_api_app(
        agents_dir=str(PROJECT_DIR),
        agent_loader=EnglishCoachAgentLoader(str(PROJECT_DIR)),
        session_service_uri="memory://",
        artifact_service_uri="memory://",
        web=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload_agents=False,
        logo_text="LearnMate",
        logo_image_url="/learnmate-logo.svg",
    )
    app = attach_brand_routes(app)
    app = attach_report_routes(app)
    return attach_history_routes(app)


app = create_app()
