# SIGINT

Daily intelligence brief — AI, IAM & cybersecurity, fintech & fraud, SWE, market intel.

A GitHub Actions pipeline that polls RSS feeds, distils them via the Claude API,
and publishes a styled HTML brief plus an RSS feed served from GitHub Pages.

**Subscribe (daily):** `https://zelim92.github.io/sigint/daily/rss.xml`

**Browse:** `https://zelim92.github.io/sigint/daily/YYYY-MM-DD.html`

**Manual trigger:** Actions tab → *SIGINT Daily Brief* → Run workflow

## How it works

- **Schedule:** `0 22 * * *` UTC — daily at 22:00 UTC (06:00 MYT next day).
- **Source:** RSS feeds listed in `sources.yaml`. Use native feeds where
  available; for email-only newsletters, route through
  [Kill the Newsletter!](https://kill-the-newsletter.com).
- **State:** `briefs/.state.json` tracks last-seen guid + pubdate per feed so
  missed runs don't drop content. Newly-added feeds bootstrap from the last 24h.
- **Distillation:** Claude (`claude-sonnet-4-6`) returns structured JSON across
  five sections — AI, IAM/security, fintech & fraud, SWE, market intel — plus
  2–3 deep-dive recommendations.
- **Outputs (committed to repo, served via Pages):**
  - `docs/daily/YYYY-MM-DD.html` — full styled brief
  - `docs/daily/rss.xml` — feed; one `<item>` per day with embedded HTML
  - `briefs/daily/YYYY-MM-DD.json` — raw model output for reproducibility

## Setup

1. **Clone and create a venv.** Uses [`uv`](https://docs.astral.sh/uv/)
   (install with `brew install uv`).

   ```bash
   uv venv
   source .venv/bin/activate
   uv pip install -r requirements.txt
   ```

2. **Add feeds to `sources.yaml`** — one entry per feed:

   ```yaml
   feeds:
     - name: krebs-on-security
       url: https://krebsonsecurity.com/feed/
     - name: tldr
       url: https://kill-the-newsletter.com/feeds/<your-feed-id>.xml
   ```

3. **Add GitHub secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | Your Anthropic API key |

4. **Enable GitHub Pages:** Settings → Pages → Source: *Deploy from a branch*
   → branch `main`, folder `/docs`.

5. **First run:** push to `main`. The workflow runs at the next 22:00 UTC, or
   trigger it manually from the Actions tab.

## Local development

```bash
source .venv/bin/activate          # if not already active
cp .env.example .env               # then fill in ANTHROPIC_API_KEY
python3 scripts/generate_daily.py
```

The script writes the same `docs/daily/`, `docs/daily/rss.xml`, and
`briefs/daily/` paths as in CI. Re-running on the same MYT day is a no-op:
the RSS prepend is guarded by `<guid>`, and the HTML write is skipped if
the dated file already exists.

## Repo layout

```
.
├── .github/workflows/
│   ├── daily.yml          ← cron + manual trigger
│   └── weekly.yml         ← stub, disabled
├── scripts/
│   ├── generate_daily.py
│   ├── ingest_rss.py
│   └── generate_weekly.py ← stub
├── sources.yaml           ← feed list
├── briefs/
│   ├── daily/             ← raw JSON, one per run
│   └── .state.json        ← per-feed last-seen state
├── docs/                  ← GitHub Pages root
│   ├── daily/
│   │   ├── rss.xml
│   │   └── YYYY-MM-DD.html
│   └── weekly/rss.xml     ← skeleton
├── requirements.txt
└── .env.example
```
