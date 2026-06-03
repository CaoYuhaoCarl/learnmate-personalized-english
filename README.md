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
`.webp`, `.heic`, or `.heif` submission images into `english_coach/input/`,
send any chat message, and the coach writes markdown reports under
`english_coach/reports/` plus training JSON under
`english_coach/training_inputs/`.

## Test

```bash
python -m unittest discover -s tests -v
```

## Notes

Local environment files, ADK session databases, virtual environments, and Python cache files are intentionally ignored by git.
