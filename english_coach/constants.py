"""Shared constants for the english_coach package."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
PROJECT_DIR = PACKAGE_DIR.parent
REPORTS_DIR = PACKAGE_DIR / "reports"
PDF_EXPORTS_DIRNAME = "pdf_exports"
PDF_EXPORTS_DIR = REPORTS_DIR / PDF_EXPORTS_DIRNAME
TRAINING_INPUTS_DIR = PACKAGE_DIR / "training_inputs"
WRITING_INPUTS_DIR = PACKAGE_DIR / "input"
