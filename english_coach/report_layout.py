"""Directory layout for generated reports."""

from pathlib import Path

STUDENTS_DIRNAME = "students"
SESSIONS_DIRNAME = "sessions"
PDF_DIRNAME = "pdf"
LEGACY_PDF_DIRNAME = "pdf_exports"


def student_markdown_dir(reports_dir: Path) -> Path:
    return reports_dir / STUDENTS_DIRNAME


def session_markdown_dir(reports_dir: Path) -> Path:
    return reports_dir / SESSIONS_DIRNAME


def pdf_dir(reports_dir: Path) -> Path:
    return reports_dir / PDF_DIRNAME


def markdown_dirs(reports_dir: Path) -> tuple[Path, ...]:
    """Return current Markdown directories plus the legacy reports root."""
    return (
        student_markdown_dir(reports_dir),
        session_markdown_dir(reports_dir),
        reports_dir,
    )


def readable_pdf_dirs(reports_dir: Path) -> tuple[Path, ...]:
    """Return the current PDF directory followed by the legacy directory."""
    return (pdf_dir(reports_dir), reports_dir / LEGACY_PDF_DIRNAME)
