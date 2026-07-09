# Deploy to Fly.io

This deployment serves the ADK Web UI plus a small password-protected PDF
reports page. Teachers use ADK Web to upload screenshots and generate reports,
then open the reports page to view, print, or download PDFs.

## Install and authenticate

```bash
brew install flyctl
fly auth login
```

## Create the Fly app

New Fly accounts may require billing before an app can be created. If Fly prints
`We need your payment information to continue`, add a credit card or buy credit
in the Fly dashboard first.

Option A: keep the starter `fly.toml` app name and create that app:

```bash
fly apps create learnmate-english-coach-smoke --org personal
```

If the name is already taken, edit `app = "learnmate-english-coach-smoke"` in
`fly.toml` to another lowercase, hyphenated app name and run `fly apps create`
with the same name.

Option B: let Fly generate a unique app name:

```bash
fly launch --generate-name --region sin --internal-port 8080 --dockerfile Dockerfile --no-deploy
```

This updates `fly.toml` with a globally unique app name.

## Configure secrets

Set the same Gemini/Google API key that you use locally:

```bash
fly secrets set GOOGLE_API_KEY=your_key_here
```

If your local `.env` also uses `GOOGLE_GENAI_USE_VERTEXAI`, set the same mode on
Fly:

```bash
fly secrets set GOOGLE_GENAI_USE_VERTEXAI=false
```

Set a password for the PDF reports page:

```bash
fly secrets set REPORTS_USERNAME=teacher REPORTS_PASSWORD=choose_a_real_password
```

Do not commit API keys or `.env` files.

## Deploy

```bash
fly deploy --remote-only --buildkit --depot=false --ha=false --primary-region sin --wg=false
fly open
```

`--remote-only` lets Fly build the Docker image in Fly's remote builder, so a
local Docker installation is not required. `--ha=false` keeps this smoke test to
one Machine. If the remote builder hangs, retry once with `--recreate-builder`.

## Verify

```bash
fly status --app learnmate-english-coach-smoke
fly secrets list --app learnmate-english-coach-smoke
curl --http1.1 https://learnmate-english-coach-smoke.fly.dev/list-apps
```

Expected:

```text
["english_coach"]
```

The ADK Web UI should be available at:

```text
https://learnmate-english-coach-smoke.fly.dev/dev-ui/
```

The PDF reports page should be available at:

```text
https://learnmate-english-coach-smoke.fly.dev/reports
```

Sign in with `REPORTS_USERNAME` and `REPORTS_PASSWORD`. Click a report name to
open it in the browser for printing, or click the download link to save the PDF.

For a browser smoke test, open the UI, select `english_coach`, attach a supported
image to the message, and ask the agent to process it. Uploaded images are staged
inside `english_coach/input/uploads/` before the existing workflow runs.

## Current limitations

- Uploaded inputs and generated reports live inside the running Machine. Use a
  Fly Volume mounted at `/app/english_coach/reports` or external storage before
  sharing this with real teachers for ongoing use.
- PDF export tools (`pandoc` and `wkhtmltopdf`) are installed in the image, with
  CJK fonts available for Chinese text. Markdown and JSON reports are still
  produced even if PDF export fails later.
- The ADK Web UI is still a developer UI. The PDF report page has simple shared
  password protection, not individual teacher accounts.
