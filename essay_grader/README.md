# essay-grader

ADK v2 workflow that grades handwritten student essays. Drop `.jpg`, `.jpeg`,
or `.png` files into `./essays/` (each image shows the printed prompt above the
student's handwritten response), then start the workflow and send any chat
message — the grader fans out one Gemini call per image in parallel, returns
structured `EssayGrade` results, and writes a markdown aggregate to
`./reports/<UTC-timestamp>.md`.

Usage: from the *parent* directory of this folder, run `adk web` and pick
`essay_grader` in the app dropdown. (`adk web` discovers apps as subdirectories
of the cwd, and the directory name must be a valid Python identifier — that's
why it's `essay_grader`, not `essay-grader`.)
