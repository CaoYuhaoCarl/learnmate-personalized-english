# LearnMate Personalized English

Google ADK agents for personalized English learning workflows. The primary
maintained workflow is `essay_grader`.

## Primary Agent

- `essay_grader`: grades handwritten student essays from uploaded image files
  and writes aggregate reports.

## Example Agents

- `my_agent`: routes a message into bug, customer support, or logistics paths.
- `hitl_agent`: extracts a refund request, analyzes the refund decision, and requests human approval for large approved refunds.

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If you run the example agents, copy their example environment files and add
your local credentials:

```bash
cp my_agent/.env.example my_agent/.env
cp hitl_agent/.env.example hitl_agent/.env
```

## Run

Start ADK from the repository root:

```bash
adk web
```

Then choose `essay_grader` in the ADK web UI. Drop `.jpg`, `.jpeg`, or `.png`
essay images into `essay_grader/essays/`, send any chat message, and the grader
writes a markdown aggregate report under `essay_grader/reports/`.

## Test

```bash
python -m unittest discover -s tests -v
```

## Notes

Local environment files, ADK session databases, virtual environments, and Python cache files are intentionally ignored by git.
