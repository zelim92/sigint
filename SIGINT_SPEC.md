# SIGINT — Claude Code Build Spec

## What you're building

A GitHub Actions pipeline that runs Mon–Fri at 8am MYT (00:00 UTC), fetches newsletter emails from Gmail, distils them into a high-signal intelligence brief using the Claude API, and publishes two outputs:

1. **`docs/daily/YYYY-MM-DD.html`** — The brief as a full interactive HTML page (served via GitHub Pages)
2. **`docs/daily/rss.xml`** — RSS feed, one `<item>` per day, full styled HTML in `<description>`

Both outputs are committed to the repo and served via GitHub Pages. The daily RSS feed URL (`/daily/rss.xml`) is the stable subscription endpoint.

An additional `workflow_dispatch` trigger allows manual on-demand generation from the GitHub UI or CLI.

**Fetch window:** 24 hours on Tue–Fri. 72 hours on Monday to seamlessly cover the weekend gap — Friday's brief ends at 00:00 UTC Friday, Monday's window starts at 00:00 UTC Friday, no overlap, no gap.

---

## Repo structure

```
sigint/
├── .github/
│   └── workflows/
│       ├── daily.yml            ← Mon–Fri cron + workflow_dispatch
│       └── weekly.yml           ← stub, disabled (future use)
├── scripts/
│   ├── generate_daily.py
│   └── generate_weekly.py       ← stub, not implemented yet
├── briefs/                      ← raw JSON output from Claude API
│   ├── daily/
│   │   └── YYYY-MM-DD.json
│   └── weekly/                  ← empty, ready for future use
├── docs/                        ← GitHub Pages root
│   ├── daily/
│   │   ├── rss.xml              ← https://YOU.github.io/sigint/daily/rss.xml
│   │   └── YYYY-MM-DD.html
│   └── weekly/                  ← empty, ready for future use
│       └── rss.xml              ← https://YOU.github.io/sigint/weekly/rss.xml
├── .env.example
└── README.md
```

---

## GitHub Actions workflow — `.github/workflows/daily.yml`

```yaml
name: SIGINT Daily Brief

on:
  schedule:
    - cron: '0 0 * * 1-5'   # 00:00 UTC = 08:00 MYT, Mon–Fri only
  workflow_dispatch:          # manual trigger from GitHub UI or CLI

jobs:
  generate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install anthropic google-auth google-auth-oauthlib google-api-python-client

      - name: Generate brief
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GMAIL_CREDENTIALS: ${{ secrets.GMAIL_CREDENTIALS }}
          GMAIL_TOKEN: ${{ secrets.GMAIL_TOKEN }}
        run: python scripts/generate_daily.py

      - name: Commit and push outputs
        run: |
          git config user.name "sigint-bot"
          git config user.email "sigint@users.noreply.github.com"
          git add docs/daily/ briefs/daily/
          git diff --staged --quiet || git commit -m "daily: $(date +'%Y-%m-%d')"
          git push
```

---

## Python script — `scripts/generate_daily.py`

### Overview

The script does four things in sequence:

1. **Authenticate** with Gmail via OAuth using credentials stored as GitHub secrets
2. **Fetch** threads from `category:forums` — 24h window Tue–Fri, 72h window on Monday
3. **Distil** the cleaned corpus via Claude API into structured JSON
4. **Render** two output files: `docs/daily/YYYY-MM-DD.html` and prepend to `docs/daily/rss.xml`. Also persist raw JSON to `briefs/daily/YYYY-MM-DD.json`.

### Gmail authentication

Credentials are stored as GitHub secrets (`GMAIL_CREDENTIALS` and `GMAIL_TOKEN`) as JSON strings. The script writes them to temp files for the Google auth library.

```python
import os, json, tempfile
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def get_gmail_service():
    creds_json = json.loads(os.environ['GMAIL_CREDENTIALS'])
    token_json = json.loads(os.environ['GMAIL_TOKEN'])

    # Write to temp files (google-auth requires file paths)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(token_json, f)
        token_path = f.name

    creds = Credentials.from_authorized_user_file(token_path)
    return build('gmail', 'v1', credentials=creds)
```

### Fetching threads

```python
def get_fetch_hours():
    """72h on Monday to cover the weekend gap seamlessly, 24h otherwise."""
    from datetime import datetime
    return 72 if datetime.utcnow().weekday() == 0 else 24


def fetch_threads(service):
    import base64, email as emaillib
    from datetime import datetime, timedelta

    hours = get_fetch_hours()
    since = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())
    query = f'category:forums after:{since}'

    result = service.users().messages().list(
        userId='me', q=query, maxResults=25
    ).execute()

    threads = []
    for msg_ref in result.get('messages', []):
        msg = service.users().messages().get(
            userId='me', id=msg_ref['id'], format='full'
        ).execute()

        headers = {h['name']: h['value'] for h in msg['payload']['headers']}
        subject = headers.get('Subject', '')
        sender = headers.get('From', '')

        # Extract plain text body
        body = extract_body(msg['payload'])

        # Skip if body is empty or clearly non-editorial
        if not body or len(body.strip()) < 100:
            continue

        threads.append({
            'subject': subject,
            'sender': sender,
            'body': body[:3000]  # cap per thread
        })

    return threads


def extract_body(payload):
    """Recursively extract plain text from Gmail message payload."""
    import base64
    if payload.get('mimeType') == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')

    for part in payload.get('parts', []):
        result = extract_body(part)
        if result:
            return result
    return ''
```

### Claude API distillation

Model: `claude-sonnet-4-20250514`
Max tokens: `4000`

**System prompt** (embed verbatim in script):

```
You are an expert intelligence analyst for a lead software engineer and long-term investor with the following profile:

READER PROFILE:
- Lead software engineer specialising in IAM, cybersecurity, and risk & fraud systems at a fintech company
- Long-term investor holding MSFT, GOOGL, AMZN, NVDA, TSMC, VOO, and VEA

Your job: extract ONLY high-signal information from the provided newsletter/forum email corpus.

REMOVE: sponsor content, product promotions, opinion rehashes, tutorial roundups, beginner content, job postings, referral links, repetitive takes on the same story.

MERGE: overlapping stories about the same event into a single item.

PRIORITISE (in order):
- AI: novel research, model releases, agentic architectures, AI applied to security or fintech
- IAM & cybersecurity: identity standards, OAuth/OIDC, zero trust, privileged access, non-human identity threats, active exploits, zero-days, CVEs, supply chain attacks, social engineering at scale
- Fintech & fraud: fraud detection, AML/KYC, payment fraud patterns, real-time payments, open banking, fintech infrastructure, regulatory changes
- Distributed systems & SWE: production case studies, resilience patterns, high-availability systems, senior/staff-level engineering craft, system design shifts
- Market intelligence: earnings signals, strategic moves, M&A, macro tech trends — portfolio-relevant and broader tech landscape

Each story body: 2-3 crisp sentences. No filler. No "in conclusion". Lead with the signal.

Output ONLY valid JSON. No markdown fences. No preamble. Schema:

{
  "date": "YYYY-MM-DD",
  "threadCount": <number>,
  "storyCount": <number>,
  "sourceCount": <number of unique sender domains>,
  "sections": {
    "ai":      [{"headline":"...","body":"...","signal":"high|med","sources":["domain.com"],"url":"https://... or null"}],
    "sec":     [...],
    "fin":     [...],
    "swe":     [...],
    "mkt":     [...]
  },
  "deepDives": [
    {"title":"...","why":"one sentence rationale","url":"https://... or null"}
  ]
}

If a section has nothing worth surfacing, use [].
deepDives: 2-3 items max. Only recommend if there is a genuine URL to read.
signal: "high" = novel, actionable, or architecturally significant. "med" = useful context but not urgent.
```

```python
import anthropic

def distil(threads):
    corpus = '\n\n'.join([
        f"---\nSOURCE: {t['sender']}\nSUBJECT: {t['subject']}\n\n{t['body']}"
        for t in threads
    ])

    client = anthropic.Anthropic()
    response = client.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=4000,
        system=SYSTEM_PROMPT,  # embed the prompt above as a constant
        messages=[{'role': 'user', 'content': f'Distil this corpus:\n\n{corpus}'}]
    )

    import json, re
    raw = response.content[0].text
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError('No JSON found in Claude response')
    return json.loads(match.group())
```

---

## HTML rendering

### `docs/daily/YYYY-MM-DD.html` — full interactive brief

Creates a new dated file each run. Use the design system below.

**Design system** (match exactly — this is the established SIGINT UI):

```css
/* Fonts */
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap');

/* Colours */
--accent:    #1D9E75;   /* green — primary, high signal border */
--warn:      #BA7517;   /* amber — med signal border */
--border:    #e5e5e5;
--muted:     #888;
--bg-secondary: #f7f7f5;

/* Section tag colours */
.tag-ai  { background: #E1F5EE; color: #0F6E56; }
.tag-sec     { background: #FCEBEB; color: #A32D2D; }
.tag-fin { background: #E6F1FB; color: #185FA5; }
.tag-swe     { background: #EEEDFE; color: #534AB7; }
.tag-mkt { background: #FAEEDA; color: #854F0B; }
.tag-dive { background: #F1EFE8; color: #5F5E5A; }

/* Reference link pill colours (match section) */
.ref-link.ai      { color: #0F6E56; background: #E1F5EE; border: 0.5px solid #9FE1CB; }
.ref-link.sec     { color: #A32D2D; background: #FCEBEB; border: 0.5px solid #F7C1C1; }
.ref-link.swe     { color: #534AB7; background: #EEEDFE; border: 0.5px solid #CECBF6; }
.ref-link.mkt     { color: #854F0B; background: #FAEEDA; border: 0.5px solid #FAC775; }
.ref-link.neutral { color: #5F5E5A; background: #F1EFE8; border: 0.5px solid #D3D1C7; }
```

**Layout structure:**

```
SIGINT // daily intelligence brief          [date]
─────────────────────────────────────────────────
[threads] [stories] [~X min] [sources]   ← meta cards

[AI section card]
  ├── section header: tag + title + item count
  └── section body: stories
        ├── story (high/med signal border)
        │     headline (linked if url exists)
        │     body text
        │     source chip + ref link pills
        └── ...

[SEC] [FIN] [SWE] [MKT] — same structure

[DEEP DIVES section card]
  └── numbered list: title (linked) + why rationale

─────────────────────────────────────────────────
generated {date} · sources: {list} · sigint pipeline v0.2
```

**Section order:** ai → sec → fin → swe → mkt → deepDives

**Story rules:**
- Headline is an `<a>` tag if `url` is not null, plain text if null
- Signal `high` → left border `#1D9E75`
- Signal `med` → left border `#BA7517`
- Source chip: small monospace pill showing sender domain
- Ref link pill: coloured to match section, opens in new tab

**Sections with zero stories are omitted entirely.**

---

### RSS item HTML — embedded in `<description>`

The RSS `<description>` field contains a simplified HTML version of the brief — same content, no JavaScript, no CSS variables (use inline styles), degrades gracefully across readers.

At the top of each RSS item, include:

```html
<p style="font-family:monospace;font-size:12px;color:#888;">
  <a href="https://YOUR_GITHUB_USERNAME.github.io/sigint/daily/{YYYY-MM-DD}.html">Open full interactive brief ↗</a>
</p>
```

Replace `YOUR_GITHUB_USERNAME` with the actual GitHub username.

**RSS item structure:**

```xml
<item>
  <title>SIGINT // {DAY DD MON YYYY}</title>
  <link>https://YOUR_GITHUB_USERNAME.github.io/sigint/daily/{YYYY-MM-DD}.html</link>
  <pubDate>{RFC 2822 formatted date, e.g. Thu, 23 Apr 2026 00:00:00 +0800}</pubDate>
  <guid isPermaLink="false">sigint-daily-{YYYY-MM-DD}</guid>
  <description><![CDATA[ ...styled HTML brief... ]]></description>
</item>
```

**Prepend** the new item after `<channel>` metadata, before existing items. Never replace — accumulate. Keep all historical items.

---

## RSS feed skeleton — `docs/daily/rss.xml`

Initialise this file if it doesn't exist:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>SIGINT Daily</title>
    <link>https://YOUR_GITHUB_USERNAME.github.io/sigint/daily/</link>
    <description>Daily intelligence brief — AI, IAM &amp; cybersecurity, fintech &amp; fraud, SWE, market intel</description>
    <language>en</language>
    <ttl>60</ttl>
  </channel>
</rss>
```

---

## Secrets setup (one-time, done outside GitHub Actions)

### Required GitHub secrets

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_CREDENTIALS` | Contents of `credentials.json` from Google Cloud (as a single-line JSON string) |
| `GMAIL_TOKEN` | Contents of `token.json` generated by OAuth flow (as a single-line JSON string) |

### Generating Gmail OAuth token (one-time local setup)

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → Enable Gmail API
3. Create OAuth 2.0 credentials → Desktop app → Download `credentials.json`
4. Run this locally to generate `token.json`:

```python
from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(port=0)

with open('token.json', 'w') as f:
    f.write(creds.to_json())

print("token.json generated. Add contents as GMAIL_TOKEN secret.")
```

5. Add both file contents as GitHub secrets (Settings → Secrets → Actions)

---

## `.env.example`

```
ANTHROPIC_API_KEY=your_anthropic_api_key_here
GMAIL_CREDENTIALS={"installed":{"client_id":"...","client_secret":"...",...}}
GMAIL_TOKEN={"token":"...","refresh_token":"...","token_uri":"...",...}
```

---

## `README.md` (brief)

```markdown
# SIGINT

Daily intelligence brief — AI, IAM & cybersecurity, fintech & fraud, SWE, market intel.

**Subscribe (daily):** `https://YOUR_GITHUB_USERNAME.github.io/sigint/daily/rss.xml`

**Manual trigger:** Actions tab → SIGINT Daily Brief → Run workflow

## Setup

1. Clone repo
2. Follow Gmail OAuth setup in SPEC.md to generate credentials
3. Add `ANTHROPIC_API_KEY`, `GMAIL_CREDENTIALS`, `GMAIL_TOKEN` as GitHub secrets
4. Enable GitHub Pages: Settings → Pages → Source: `docs/` folder
5. Push — first run executes at next 00:00 UTC or trigger manually
```

---

## Implementation notes for Claude Code

- **Do not hallucinate URLs** — only include `url` in stories and deep dives if the URL appears explicitly in the email body. Set to `null` otherwise.
- **Test locally first** — the script should run locally with a `.env` file before wiring into GHA. Add `python-dotenv` as a dev dependency and load `.env` if not in CI.
- **Error handling** — if Gmail returns 0 threads, write a "no content" brief rather than failing. If Claude API fails, exit with non-zero code so GHA marks the run failed.
- **Idempotency** — if the workflow runs twice in a day (e.g. manual + scheduled), check for an existing `<guid>` matching `sigint-daily-{YYYY-MM-DD}` before prepending to RSS. Skip if already exists. Also skip if `docs/daily/YYYY-MM-DD.html` already exists on disk.
- **GitHub Pages** — enable in repo Settings → Pages → Source: `docs/` folder. Briefs served at `https://YOUR_GITHUB_USERNAME.github.io/sigint/daily/YYYY-MM-DD.html`, feed at `/daily/rss.xml`.
- **Token refresh** — Gmail OAuth tokens expire. The script should handle token refresh automatically using `google.auth.transport.requests.Request()` and write the refreshed token back. Since GHA can't persist files between runs, write the refreshed token to a GitHub secret via the GitHub API, or use a service account instead of OAuth for a more robust setup.

### Token refresh handling (important)

OAuth tokens expire and need refreshing. Two options:

**Option A — Refresh and update secret via GitHub API (recommended):**
After refreshing the token, use the GitHub API to update the `GMAIL_TOKEN` secret automatically. Requires adding `GITHUB_TOKEN` with write permissions to secrets.

**Option B — Use a Google Service Account:**
Create a service account in Google Cloud, grant it domain-wide delegation to access the Gmail account, and use service account credentials instead of OAuth. No token expiry issues. More complex initial setup.

For a personal single-user setup, Option A is pragmatic. Start with Option A and move to Option B if token management becomes painful.

---

## Reference: established brief HTML output

The HTML design is already established and validated. A working reference file (`sigint_apr26.html`) should be in the repo root or `docs/` as the visual target. The Python script should generate HTML that matches this file's structure and styling exactly.

Key elements to preserve:
- IBM Plex Mono for all labels, chips, metadata
- IBM Plex Sans for body text
- Section cards with rounded corners and subtle borders
- Colour-coded left border on stories (green = high, amber = med)
- Colour-coded ref link pills matching section colour
- Meta row (4 cards: threads, stories, read time, sources)
- Footer with generation timestamp and source list
```
