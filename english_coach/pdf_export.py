"""Export English coach Markdown reports to PDF."""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from .constants import PDF_EXPORTS_DIR
from .constants import PACKAGE_DIR
from .constants import REPORTS_DIR

DEFAULT_CSS_PATH = PACKAGE_DIR / "report_print.css"
DEFAULT_OUTPUT_DIR = PDF_EXPORTS_DIR
WPS_BUNDLE_ID = "com.kingsoft.wpsoffice.mac"


class PdfExportError(RuntimeError):
    """Raised when a report cannot be exported to PDF."""


class PdfOpenError(RuntimeError):
    """Raised when a generated PDF cannot be opened in WPS."""


def _require_tool(name: str) -> str:
    tool_path = shutil.which(name)
    if not tool_path:
        raise PdfExportError(
            f"Missing required tool: {name}. On macOS, run: "
            "brew install pandoc weasyprint, then run make check-tools."
        )
    return tool_path


def _absolute_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return Path.cwd() / path


def open_pdf_in_wps(pdf_path: Path | str) -> None:
    """Open a PDF in the local macOS WPS app when available."""
    if sys.platform != "darwin":
        return

    pdf = _absolute_path(Path(pdf_path))
    command = ["open", "-b", WPS_BUNDLE_ID, str(pdf)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Could not open PDF in WPS: {pdf}"
        if detail:
            message = f"{message}: {detail}"
        raise PdfOpenError(message) from exc
    except OSError as exc:
        raise PdfOpenError(f"Could not open PDF in WPS: {pdf}: {exc}") from exc


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
    weasyprint = _require_tool("weasyprint")

    output.mkdir(parents=True, exist_ok=True)
    pdf_path = output / f"{markdown.stem}.pdf"
    markdown_for_command = _absolute_path(markdown)
    css_for_command = _absolute_path(css)
    pdf_for_command = _absolute_path(pdf_path)
    command = [
        pandoc,
        str(markdown_for_command),
        "--from=gfm+yaml_metadata_block",
        "--standalone",
        "--to=html5",
        f"--css={css_for_command}",
        "--pdf-engine",
        weasyprint,
        "-o",
        str(pdf_for_command),
    ]

    env = os.environ.copy()
    with tempfile.TemporaryDirectory(
        prefix=f"{markdown.stem}-",
        dir=_absolute_path(output),
    ) as temp_dir:
        env["TMPDIR"] = temp_dir
        env["TEMP"] = temp_dir
        env["TMP"] = temp_dir
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=temp_dir,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            message = f"PDF export failed for {markdown}"
            if detail:
                message = f"{message}: {detail}"
            raise PdfExportError(message) from exc
        except OSError as exc:
            raise PdfExportError(f"PDF export failed for {markdown}: {exc}") from exc

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
