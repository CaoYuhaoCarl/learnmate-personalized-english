"""Export English coach Markdown reports to PDF."""

from __future__ import annotations

import argparse
import datetime
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

PACKAGE_DIR = Path(__file__).parent
REPORTS_DIR = PACKAGE_DIR / "reports"
DEFAULT_CSS_PATH = PACKAGE_DIR / "report_print.css"
DEFAULT_OUTPUT_DIR = REPORTS_DIR / "pdf_exports"


class PdfExportError(RuntimeError):
    """Raised when a report cannot be exported to PDF."""


def _require_tool(name: str) -> str:
    tool_path = shutil.which(name)
    if not tool_path:
        raise PdfExportError(
            f"Missing required tool: {name}. Install pandoc and wkhtmltopdf "
            "before exporting PDFs."
        )
    return tool_path


def reports_for_date(
    date_text: str,
    reports_dir: Path = REPORTS_DIR,
) -> list[Path]:
    """Return Markdown reports generated on a YYYY-MM-DD date."""
    return sorted(reports_dir.glob(f"*_{date_text}_*.md"))


def export_report_pdf(
    markdown_path: Path | str,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    css_path: Path | str = DEFAULT_CSS_PATH,
) -> Path:
    """Convert one Markdown report to a PDF and return the PDF path."""
    markdown = Path(markdown_path)
    output = Path(output_dir)
    css = Path(css_path)

    if not markdown.is_file():
        raise PdfExportError(f"Markdown report not found: {markdown}")
    if not css.is_file():
        raise PdfExportError(f"PDF stylesheet not found: {css}")

    pandoc = _require_tool("pandoc")
    wkhtmltopdf = _require_tool("wkhtmltopdf")

    output.mkdir(parents=True, exist_ok=True)
    pdf_path = output / f"{markdown.stem}.pdf"
    command = [
        pandoc,
        str(markdown),
        "--from=gfm+yaml_metadata_block",
        "--standalone",
        f"--css={css}",
        "--pdf-engine",
        wkhtmltopdf,
        "--pdf-engine-opt=--enable-local-file-access",
        "--pdf-engine-opt=--page-size",
        "--pdf-engine-opt=A4",
        "--pdf-engine-opt=--margin-top",
        "--pdf-engine-opt=16mm",
        "--pdf-engine-opt=--margin-right",
        "--pdf-engine-opt=14mm",
        "--pdf-engine-opt=--margin-bottom",
        "--pdf-engine-opt=16mm",
        "--pdf-engine-opt=--margin-left",
        "--pdf-engine-opt=14mm",
        "-o",
        str(pdf_path),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"PDF export failed for {markdown}"
        if detail:
            message = f"{message}: {detail}"
        raise PdfExportError(message) from exc

    if not pdf_path.is_file():
        raise PdfExportError(f"PDF export did not create output: {pdf_path}")

    return pdf_path


def _default_date() -> str:
    return datetime.datetime.now().astimezone().date().isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export English coach Markdown reports to PDF."
    )
    parser.add_argument(
        "reports",
        nargs="*",
        type=Path,
        help="Specific Markdown report paths. Defaults to reports for today.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Export reports generated on this YYYY-MM-DD date when no paths are given.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    reports = list(args.reports)
    if not reports:
        date_text = args.date or _default_date()
        reports = reports_for_date(date_text)
        if not reports:
            print(f"No Markdown reports found for {date_text}.", file=sys.stderr)
            return 1

    try:
        for report in reports:
            pdf_path = export_report_pdf(report)
            print(pdf_path)
    except PdfExportError as exc:
        print(exc, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
