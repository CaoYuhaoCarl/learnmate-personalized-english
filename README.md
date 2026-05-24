# Storyflow ADK Agents

Small Google ADK examples for workflow-style agents.

## Agents

- `my_agent`: routes a message into bug, customer support, or logistics paths.
- `hitl_agent`: extracts a refund request, analyzes the refund decision, and requests human approval for large approved refunds.
- `composition_correction_agent`: backend-only multi-agent workflow that accepts a handwriting image path, extracts OCR text, pauses for human OCR review, corrects the composition, scores it, and persists a structured correction record.

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copy the example environment file into each agent directory and add your local credentials:

```bash
cp .env.example my_agent/.env
cp .env.example hitl_agent/.env
cp .env.example composition_correction_agent/.env
```

## Run

Start ADK from the repository root:

```bash
adk web
```

Then choose `my_agent`, `hitl_agent`, or `composition_correction_agent` in the ADK web UI.

Run the backend-only composition correction sample without OCR or model credentials:

```bash
python examples/run_handwriting_correction.py
```

The sample reads `samples/handwritten_english_composition.svg` and its `.txt`
sidecar OCR stub, prints each agent step, prints the final correction report,
and writes a local JSON correction record under `tmp/composition_correction_records/`.

See `docs/composition_correction_backend.md` for the OCR/model agent integration points.

## Test

```bash
python -m unittest discover -s tests -v
```

## Composition correction agent notes

The local sample intentionally avoids external OCR and model calls. It uses `LocalSidecarOcrAgent` as an offline OCR agent stub, while `ocr_extraction_agent` is the ADK agent responsible for OCR in the real workflow. Run `composition_correction_agent.root_agent` through ADK with model credentials and an uploaded image payload for production agent outputs.

## Notes

Local environment files, ADK session databases, virtual environments, and Python cache files are intentionally ignored by git.
