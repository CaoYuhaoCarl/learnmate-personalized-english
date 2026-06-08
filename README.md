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

## Run

Start ADK from the repository root:

```bash
adk web
```

Then choose `english_coach` in the ADK web UI. Drop `.jpg`, `.jpeg`, `.png`,
`.webp`, `.heic`, or `.heif` submission images into `english_coach/input/` or
`english_coach/input/tem/`, send any chat message, and the coach renames
recognized inputs as `Student_YYYY-MM-DD.ext`, writes markdown reports under
`english_coach/reports/` plus training JSON under
`english_coach/training_inputs/`. PDF copies are exported automatically under
`english_coach/reports/pdf_exports/` when `pandoc` and `wkhtmltopdf` are
available.

## PDF export

Manual export uses the same print stylesheet as the automatic workflow:

```bash
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
