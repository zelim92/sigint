# SIGINT

Daily intelligence brief — AI, IAM & cybersecurity, fintech & fraud, SWE, market intel.

A GitHub Actions pipeline that runs Mon–Fri at 08:00 MYT, pulls newsletter
threads from Gmail's `category:forums`, distils them via the Claude API, and
publishes a styled HTML brief plus an RSS feed served from GitHub Pages.

**Subscribe (daily):** `https://zelim92.github.io/sigint/daily/rss.xml`

**Browse:** `https://zelim92.github.io/sigint/daily/YYYY-MM-DD.html`

**Manual trigger:** Actions tab → *SIGINT Daily Brief* → Run workflow

## How it works

- **Schedule:** `0 0 * * 1-5` UTC (08:00 MYT, weekdays). Mondays use a 72h
  fetch window so the Friday→Monday weekend is covered with no gap or overlap.
- **Source:** Gmail threads matching `category:forums` from the last 24h
  (or 72h on Monday).
- **Distillation:** Claude (`claude-sonnet-4-6`) returns structured JSON across
  five sections: AI, IAM/security, fintech & fraud, SWE, market intel — plus
  2–3 deep-dive recommendations.
- **Outputs (committed to repo, served via Pages):**
  - `docs/daily/YYYY-MM-DD.html` — full styled brief
  - `docs/daily/rss.xml` — feed; one `<item>` per day with embedded HTML
  - `briefs/daily/YYYY-MM-DD.json` — raw model output for reproducibility

## Setup

1. **Clone the repo and create a venv.** Uses [`uv`](https://docs.astral.sh/uv/)
   — install with `brew install uv` if you don't have it.

   ```bash
   uv venv
   source .venv/bin/activate
   uv pip install -r requirements.txt
   ```

2. **Generate Gmail OAuth credentials** (one-time, local):

   1. [Google Cloud Console](https://console.cloud.google.com) → create
      project → enable the Gmail API.
   2. APIs & Services → Credentials → *Create OAuth client ID* → **Desktop app**
      → download as `credentials.json` and drop it in the repo root.
   3. With the venv active, run the local OAuth flow — a browser will pop
      open, click *Allow*:

      ```bash
      python3 -c "
      from google_auth_oauthlib.flow import InstalledAppFlow
      flow = InstalledAppFlow.from_client_secrets_file(
          'credentials.json',
          ['https://www.googleapis.com/auth/gmail.readonly'])
      creds = flow.run_local_server(port=0)
      open('token.json', 'w').write(creds.to_json())
      "
      ```

      Both `credentials.json` and `token.json` are gitignored — never commit
      them.

3. **Add GitHub secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | Your Anthropic API key |
   | `GMAIL_CREDENTIALS` | Single-line JSON contents of `credentials.json` |
   | `GMAIL_TOKEN` | Single-line JSON contents of `token.json` |

   Tip: paste the JSON as-is — GitHub stores secrets verbatim; the script
   parses them with `json.loads`.

4. **Enable GitHub Pages:** Settings → Pages → Source: *Deploy from a branch*
   → branch `main`, folder `/docs`.

5. **First run:** push to `main`. The workflow runs at the next 00:00 UTC, or
   trigger it manually from the Actions tab.

## Local development

```bash
source .venv/bin/activate          # if not already active
cp .env.example .env               # then fill in your secrets
python3 scripts/generate_daily.py
```

The script writes the same `docs/daily/`, `docs/daily/rss.xml`, and
`briefs/daily/` paths as in CI. Re-running on the same UTC day is a no-op:
the RSS prepend is guarded by `<guid>`, and the HTML write is skipped if
the dated file already exists.

## Token expiry

Gmail OAuth refresh tokens are long-lived but not eternal. The script refreshes
the access token in memory each run, but if the refresh token itself expires
(or is revoked), CI will fail with an auth error — re-run the local OAuth flow
above and update the `GMAIL_TOKEN` secret.

While your Google Cloud OAuth consent screen is in **Testing** mode, refresh
tokens expire after **7 days**. For a personal single-user setup, publish the
consent screen to *In production* (no Google verification is required when
you're the only user) to get long-lived tokens.

## Repo layout

```
.
├── .github/workflows/
│   ├── daily.yml          ← cron + manual trigger
│   └── weekly.yml         ← stub, disabled
├── scripts/
│   ├── generate_daily.py
│   └── generate_weekly.py ← stub
├── briefs/daily/          ← raw JSON, one per run
├── docs/                  ← GitHub Pages root
│   ├── daily/
│   │   ├── rss.xml
│   │   └── YYYY-MM-DD.html
│   └── weekly/rss.xml     ← skeleton
├── requirements.txt
├── .env.example
└── SIGINT_SPEC.md         ← original build spec
```
