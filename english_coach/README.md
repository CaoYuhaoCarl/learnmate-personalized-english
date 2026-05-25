# english-coach

ADK v2 workflow for English coaching feedback. The current workflow grades
handwritten writing submissions. Drop `.jpg`, `.jpeg`, or `.png` files into
`./submissions/` (each image shows the printed prompt above the student's
handwritten response), then start the workflow and send any chat message. The
coach fans out one Gemini call per image in parallel, returns structured
`EnglishCoachFeedback` results, and writes markdown reports to `./reports/`.

Usage: from the *parent* directory of this folder, run `adk web` and pick
`english_coach` in the app dropdown. (`adk web` discovers apps as subdirectories
of the cwd, and the directory name must be a valid Python identifier — that's
why it's `english_coach`, not `english-coach`.)
