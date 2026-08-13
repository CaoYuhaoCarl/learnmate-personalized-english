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

Open <http://127.0.0.1:8080/dev-ui/> and choose `english_coach` in the ADK web UI.
Attach `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, or `.heif` submission images to a message and ask the coach to process them.
The coach stages uploaded images under `english_coach/input/uploads/`, renames recognized inputs as `Student_YYYY-MM-DD.ext`, writes student Markdown reports under `english_coach/reports/students/`, session Markdown reports under `english_coach/reports/sessions/`, plus training JSON under `english_coach/training_inputs/`.
PDF copies are exported automatically under `english_coach/reports/pdf/` when `pandoc` and `wkhtmltopdf` are available.

Open <http://127.0.0.1:8080/reports> to view, print, or download generated
PDFs. The default username is `teacher`; use `REPORTS_USERNAME` to override it.
`REPORTS_PASSWORD` must be set before reports can be viewed.

## Sandbox mode for testing

Set `LEARNMATE_DATA_ROOT` before starting the server to run the whole app against a disposable workspace, so test runs never touch the real student records:

```bash
LEARNMATE_DATA_ROOT="$PWD/sandbox" REPORTS_PASSWORD=change-me .venv/bin/uvicorn english_coach.web_app:app --port 8080
```

The history database (`data/learnmate.db`), `reports/`, `training_inputs/`, and `input/` are then all created under that directory.
The `/reports` and `/students` pages show a yellow banner while sandbox mode is active.

To test against realistic data, seed the sandbox from the real database with the WAL-safe backup command (do not plain-copy the file):

```bash
mkdir -p sandbox/data
sqlite3 english_coach/data/learnmate.db ".backup 'sandbox/data/learnmate.db'"
```

## PDF export

Manual export uses the same print stylesheet as the automatic workflow:

```bash
make check-tools
make pdf
make pdf DATE=2026-06-06
python3 -m english_coach.pdf_export english_coach/reports/students/Cindy_2026-06-05_20-06-53.md
```

## Test

```bash
python -m unittest discover -s tests -v
```

## History database maintenance

Re-running the same essay (same student, category, and filename) replaces its record instead of appending a duplicate.
The latest run wins; a re-upload that gets renamed with a `_2` suffix is treated as a new essay, since it may genuinely be a second submission from the same day.
Duplicates recorded before this behavior existed can be cleaned up with:

```bash
python -m english_coach.history_store dedupe          # dry run, prints what would be deleted
python -m english_coach.history_store dedupe --apply  # backs up the DB file, then deletes
```

`--apply` keeps the newest record per essay, removes rows written by old unisolated unit tests, and creates the unique index that enforces one record per essay.
A timestamped backup is written next to the database before anything is deleted.

## Notes

Local environment files, ADK session databases, virtual environments, and Python cache files are intentionally ignored by git.
