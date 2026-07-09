# LearnMate Personalized English

Google ADK workflow for personalized English learning. The maintained agent is
`english_coach`, which produces structured English coaching feedback from
uploaded submission images, writes aggregate reports, and exports personalized
training inputs.

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

PDF export also needs two system command-line tools:

```bash
brew install pandoc
```

Install `wkhtmltopdf` from the macOS package at
<https://wkhtmltopdf.org/downloads.html>, then verify both tools are available:

```bash
make check-tools
```

## Run

Start the combined ADK Web and PDF reports app from the repository root:

```bash
REPORTS_PASSWORD=change-me .venv/bin/uvicorn english_coach.web_app:app --port 8080
```

Open <http://127.0.0.1:8080/dev-ui/> and choose `english_coach` in the ADK web
UI. Attach `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, or `.heif` submission
images to a message and ask the coach to process them. The coach stages uploaded
images under `english_coach/input/uploads/`, renames recognized inputs as
`Student_YYYY-MM-DD.ext`, writes markdown reports under `english_coach/reports/`
plus training JSON under `english_coach/training_inputs/`. PDF copies are
exported automatically under `english_coach/reports/pdf_exports/` when `pandoc`
and `wkhtmltopdf` are available.

Open <http://127.0.0.1:8080/reports> to view, print, or download generated
PDFs. The default username is `teacher`; use `REPORTS_USERNAME` to override it.
`REPORTS_PASSWORD` must be set before reports can be viewed.

## PDF export

Manual export uses the same print stylesheet as the automatic workflow:

```bash
make check-tools
make pdf
make pdf DATE=2026-06-06
python3 -m english_coach.pdf_export english_coach/reports/Cindy_2026-06-05_20-06-53.md
```

## Test

```bash
python -m unittest discover -s tests -v
```

## Notes

Local environment files, ADK session databases, virtual environments, and Python cache files are intentionally ignored by git.
