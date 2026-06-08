# english-coach

ADK v2 workflow for English coaching feedback. Drop `.jpg`, `.jpeg`, `.png`,
`.webp`, `.heic`, or `.heif` files into `./input/` or `./input/tem/`. The
workflow classifies each screenshot as writing feedback, grammar training, or
unsupported, then routes it to the matching structured extractor. After
extraction, recognized input images are automatically renamed in their original
folder as `Student_YYYY-MM-DD.ext` using the visible student name and image date.
It writes per-student Markdown reports to `./reports/` and machine-readable
personalized training inputs to `./training_inputs/`. When `pandoc` and
`wkhtmltopdf` are available, each Markdown report is also exported to
`./reports/pdf_exports/`.

Usage: from the *parent* directory of this folder, run `adk web` and pick
`english_coach` in the app dropdown. (`adk web` discovers apps as subdirectories
of the cwd, and the directory name must be a valid Python identifier — that's
why it's `english_coach`, not `english-coach`.)

PDF export requires `pandoc` and `wkhtmltopdf` to be installed on the computer.
On macOS, install `pandoc` with `brew install pandoc`, then install
`wkhtmltopdf` from <https://wkhtmltopdf.org/downloads.html>. From the repository
root, run `make check-tools` to confirm both commands are available.

Manual PDF export from the repository root:

```bash
make check-tools
make pdf
make pdf DATE=2026-06-06
python3 -m english_coach.pdf_export english_coach/reports/Cindy_2026-06-05_20-06-53.md
```
