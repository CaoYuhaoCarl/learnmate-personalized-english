"""FastAPI entrypoint that adds teacher-friendly PDF report routes to ADK Web."""

from __future__ import annotations

import html
import os
import secrets
from pathlib import Path

from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic
from fastapi.security import HTTPBasicCredentials
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.agent_loader import AgentLoader

PACKAGE_DIR = Path(__file__).parent
PROJECT_DIR = PACKAGE_DIR.parent
REPORTS_DIR = PACKAGE_DIR / "reports"
PDF_EXPORTS_DIRNAME = "pdf_exports"
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
        "<h1>PDF Reports</h1>"
        f"{body}"
        "</body>"
        "</html>"
    )


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
    )
    return attach_report_routes(app)


app = create_app()
