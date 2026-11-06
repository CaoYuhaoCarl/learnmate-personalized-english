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

PDF export also needs the `pandoc` and `weasyprint` system command-line tools.
On macOS, install them with Homebrew:

```bash
brew install pandoc weasyprint
```

These tools are installed separately from the Python requirements. Verify that
both commands can run:

```bash
make check-tools
```

## Run

Configure `GOOGLE_API_KEY` and `REPORTS_PASSWORD` in the repository's `.env` or `english_coach/.env`.
The default HTTP Basic username is `teacher`; set `REPORTS_USERNAME` to override it.
Start the app from the repository root with one worker:

```bash
.venv/bin/uvicorn english_coach.web_app:app --host 127.0.0.1 --port 8080 --workers 1
```

Open <http://127.0.0.1:8080/> and sign in to use the teacher workspace.
Choose up to 10 `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, or `.heif` images, choose Chinese or English feedback, and submit.
Each image may be up to 10 MiB, with a total of 30 MiB per submission.
The server checks the extension and image signature; unreadable image contents are reported during processing.
Processing creates a teacher draft; it does not generate a formal PDF yet.
Compare the original image with the editable transcription, remove mistaken deductions or add missed errors, and adjust feedback and practice priorities.
Writing scores show the system suggestion and optional teacher scores (0–5 per dimension, in half-point steps); the total is their sum out of 20.
Save the draft, check the updated suggestions, then acknowledge the review and choose “确认并生成 PDF”.
Deleting an error removes its derived practice suggestion; correcting transcription marks dependent judgments for teacher review rather than silently calling the model again.
The original extraction remains available for comparison, and teacher scores are retained when system suggestions change.
Use “重试 PDF 导出” after an export failure; it reuses the confirmed version without analyzing the images again.

The workspace processes only the files explicitly submitted through it.
Each batch has its own directory, so concurrent submissions and same-named uploads cannot mix outputs.
Unknown student names remain unassigned; this interface does not provide persistent student identities or distinguish two students with the same name.
For ambiguous attribution, submit each student's work separately and verify the result.
All routes, including the legacy ADK API and live WebSocket, require the configured credentials.
The default loopback binding is intended for local use; remote access needs HTTPS and an appropriate access boundary.

Submission state, immutable uploaded images, original evidence and teacher drafts are stored under `english_coach/reports/web/<submission-id>/`.
Confirmed snapshots live in `confirmed/r<revision>/snapshot.json`; each export attempt has a separate directory beneath that version.
PDFs include the confirmed version and time, and all artifacts derive from that snapshot.
Set `LEARNMATE_WEB_DATA_DIR` to an absolute directory to use another local data location.
These files contain student information and are excluded from Git by the default reports directory rule.
The app requires macOS or Linux and one worker per data directory; a file lock rejects a second worker.
One batch runs at a time, with up to three additional batches waiting.
Processing has a 15-minute waiting limit.

Refreshing the page restores the saved submission without sending it to the model again.
If an upload response is lost, the page checks the same submission ID and offers a manual retry; resending that ID and the same files returns the existing task.
Completed results survive a restart.
An interrupted task is marked interrupted after restart and is never retried automatically; inspect the result before starting a new submission.
PDF subprocesses already started may finish during shutdown or after the five-minute export waiting limit, but uncompleted exports are marked retryable without losing the confirmed content.
Two open editor tabs use version checks to prevent an older save from overwriting a newer change.
Editing confirmed content creates a new draft; old PDFs remain labelled by their confirmed version and cannot stand in for the new draft.
Older saved results can be edited, but missing original images or scoring evidence are explicitly identified and cannot be reconstructed from their old score.

The recent submissions panel opens saved feedback.
<http://127.0.0.1:8080/reports> lists all available PDFs from the new workspace and the original report directory, including submissions older than the recent panel.

The original ADK interface remains at <http://127.0.0.1:8080/dev-ui/> for development.
It retains the existing directory-scanning workflow and can process local inputs when there are no valid image attachments.
Its outputs remain under `english_coach/reports/` and `english_coach/training_inputs/`, and its existing macOS WPS auto-open behavior is unchanged.
The teacher workspace opens PDFs only when the user selects a report link.

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
node --test tests/test_teacher_ui.js tests/test_review_ui.js
```

The JavaScript checks use Node's built-in test runner and require no npm packages.
See [teacher acceptance steps and verification boundaries](docs/web-acceptance.md) for the synthetic browser test.
The [teacher review design and acceptance record](docs/teacher-review/decision.md) describes revision semantics and validation.

## Notes

Local environment files, ADK session databases, virtual environments, and Python cache files are intentionally ignored by git.
