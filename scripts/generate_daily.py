#!/usr/bin/env python3
"""SIGINT — daily intelligence brief generator.

Pulls newsletter threads from Gmail (`category:forums`), distils them via the
Claude API into a structured brief, then writes:

  - briefs/daily/YYYY-MM-DD.json   raw model output
  - docs/daily/YYYY-MM-DD.html     full styled brief
  - docs/daily/rss.xml             prepends one <item> with embedded HTML

Designed to run in GitHub Actions; also runnable locally with a .env file.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DAILY = REPO_ROOT / "docs" / "daily"
BRIEFS_DAILY = REPO_ROOT / "briefs" / "daily"
RSS_PATH = DOCS_DAILY / "rss.xml"

GITHUB_USER = "zelim92"
REPO_NAME = "sigint"
SITE_BASE = f"https://{GITHUB_USER}.github.io/{REPO_NAME}"

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4000
PIPELINE_VERSION = "v0.2"

SECTIONS: list[tuple[str, str]] = [
    ("ai", "Artificial intelligence"),
    ("sec", "IAM & cybersecurity"),
    ("fin", "Fintech & fraud"),
    ("swe", "Distributed systems & SWE"),
    ("mkt", "Market intelligence"),
]
SECTION_TAG_LABEL = {
    "ai": "AI",
    "sec": "SEC",
    "fin": "FIN",
    "swe": "SWE",
    "mkt": "MKT",
}

SYSTEM_PROMPT = """You are an expert intelligence analyst for a lead software engineer and long-term investor with the following profile:

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
signal: "high" = novel, actionable, or architecturally significant. "med" = useful context but not urgent."""


# ────────────────────────────────────────────────────────────────
# Gmail
# ────────────────────────────────────────────────────────────────

def get_gmail_service():
    token_json = json.loads(os.environ["GMAIL_TOKEN"])
    creds = Credentials.from_authorized_user_info(token_json)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Gmail credentials invalid and not refreshable — re-run the local OAuth flow."
            )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_fetch_hours() -> int:
    """72h on Monday (UTC) to seamlessly cover the weekend gap, 24h otherwise."""
    return 72 if datetime.now(timezone.utc).weekday() == 0 else 24


def extract_body(payload: dict) -> str:
    """Recursively pull the first text/plain body from a Gmail payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    for part in payload.get("parts", []) or []:
        body = extract_body(part)
        if body:
            return body
    return ""


def fetch_threads(service) -> list[dict]:
    hours = get_fetch_hours()
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"category:forums after:{since}"

    result = service.users().messages().list(
        userId="me", q=query, maxResults=25
    ).execute()

    threads: list[dict] = []
    for msg_ref in result.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")
        body = extract_body(msg["payload"])

        if not body or len(body.strip()) < 100:
            continue

        threads.append({
            "subject": subject,
            "sender": sender,
            "body": body[:3000],
        })
    return threads


# ────────────────────────────────────────────────────────────────
# Claude distillation
# ────────────────────────────────────────────────────────────────

def distil(threads: list[dict], date_str: str) -> dict:
    corpus = "\n\n".join(
        f"---\nSOURCE: {t['sender']}\nSUBJECT: {t['subject']}\n\n{t['body']}"
        for t in threads
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Today's date is {date_str}. Distil this corpus from the last "
                f"{get_fetch_hours()} hours:\n\n{corpus}"
            ),
        }],
    )

    raw = response.content[0].text
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"No JSON found in Claude response. Raw output:\n{raw[:500]}")
    return json.loads(match.group())


def empty_brief(date_str: str) -> dict:
    return {
        "date": date_str,
        "threadCount": 0,
        "storyCount": 0,
        "sourceCount": 0,
        "sections": {key: [] for key, _ in SECTIONS},
        "deepDives": [],
        "_noContent": True,
    }


# ────────────────────────────────────────────────────────────────
# Rendering helpers
# ────────────────────────────────────────────────────────────────

def esc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def estimate_read_minutes(brief: dict) -> int:
    words = 0
    for items in brief.get("sections", {}).values():
        for s in items:
            words += len((s.get("headline", "") + " " + s.get("body", "")).split())
    for d in brief.get("deepDives", []):
        words += len((d.get("title", "") + " " + d.get("why", "")).split())
    return max(1, ceil(words / 200))


def domain_from_url(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def all_sources(brief: dict) -> list[str]:
    seen: list[str] = []
    for items in brief.get("sections", {}).values():
        for s in items:
            for src in s.get("sources", []) or []:
                if src and src not in seen:
                    seen.append(src)
    return seen


def section_storyhtml(key: str, story: dict) -> str:
    signal = "high" if story.get("signal") == "high" else "med"
    headline_text = esc(story.get("headline", ""))
    url = story.get("url")

    if url:
        headline_html = f'<a href="{esc(url)}" target="_blank">{headline_text}</a>'
    else:
        headline_html = headline_text

    chips = "".join(
        f'<span class="chip">{esc(src)}</span>'
        for src in (story.get("sources") or [])
    )

    ref_links = ""
    if url:
        domain = domain_from_url(url) or url
        ref_links = (
            f'<div class="ref-links">'
            f'<a class="ref-link {key}" href="{esc(url)}" target="_blank">{esc(domain)} ↗</a>'
            f'</div>'
        )

    return (
        f'      <div class="story {signal}">\n'
        f'        <div class="story-headline">{headline_html}</div>\n'
        f'        <div class="story-body">{esc(story.get("body", ""))}</div>\n'
        f'        <div class="story-meta">{chips}{ref_links}</div>\n'
        f'      </div>'
    )


def section_html(key: str, title: str, stories: list[dict]) -> str:
    if not stories:
        return ""
    items_label = "1 item" if len(stories) == 1 else f"{len(stories)} items"
    bodies = "\n\n".join(section_storyhtml(key, s) for s in stories)
    return (
        f'  <div class="section-card">\n'
        f'    <div class="section-header">\n'
        f'      <span class="section-tag tag-{key}">{SECTION_TAG_LABEL[key]}</span>\n'
        f'      <span class="section-title">{esc(title)}</span>\n'
        f'      <span class="item-count">{items_label}</span>\n'
        f'    </div>\n'
        f'    <div class="section-body">\n\n'
        f'{bodies}\n\n'
        f'    </div>\n'
        f'  </div>'
    )


def deep_dives_html(dives: list[dict]) -> str:
    if not dives:
        return ""
    items_label = "1 item" if len(dives) == 1 else f"{len(dives)} items"
    rows = []
    for i, d in enumerate(dives, 1):
        title = esc(d.get("title", ""))
        url = d.get("url")
        title_html = (
            f'<a href="{esc(url)}" target="_blank">{title} ↗</a>' if url else title
        )
        rows.append(
            f'      <div class="dive-item">\n'
            f'        <span class="dive-num">{i:02d}</span>\n'
            f'        <div>\n'
            f'          <div class="dive-title">{title_html}</div>\n'
            f'          <div class="dive-why">{esc(d.get("why", ""))}</div>\n'
            f'        </div>\n'
            f'      </div>'
        )
    return (
        f'  <div class="section-card">\n'
        f'    <div class="section-header">\n'
        f'      <span class="section-tag tag-dive">DEEP DIVES</span>\n'
        f'      <span class="section-title">Recommended deep dives</span>\n'
        f'      <span class="item-count">{items_label}</span>\n'
        f'    </div>\n'
        f'    <div class="section-body">\n\n'
        + "\n\n".join(rows) + "\n\n"
        f'    </div>\n'
        f'  </div>'
    )


# ────────────────────────────────────────────────────────────────
# Full HTML page
# ────────────────────────────────────────────────────────────────

PAGE_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap');
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'IBM Plex Sans', sans-serif; background: #fff; color: #111; padding: 2rem; max-width: 720px; margin: 0 auto; }
  :root { --accent: #1D9E75; --warn: #BA7517; --border: #e5e5e5; --muted: #888; --bg-secondary: #f7f7f5; }
  .header { display: flex; align-items: baseline; justify-content: space-between; border-bottom: 1px solid var(--border); padding-bottom: 0.75rem; margin-bottom: 1.25rem; }
  .logo { font-family: 'IBM Plex Mono', monospace; font-size: 13px; font-weight: 500; letter-spacing: 0.08em; color: var(--accent); }
  .tagline { font-size: 12px; color: var(--muted); font-family: 'IBM Plex Mono', monospace; margin-left: 12px; }
  .date-stamp { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--muted); }
  .meta-row { display: flex; gap: 10px; margin-bottom: 1.25rem; flex-wrap: wrap; }
  .meta-card { background: var(--bg-secondary); border-radius: 8px; padding: 10px 16px; flex: 1; min-width: 110px; }
  .meta-card .val { font-family: 'IBM Plex Mono', monospace; font-size: 20px; font-weight: 500; }
  .meta-card .lbl { font-size: 11px; color: var(--muted); font-family: 'IBM Plex Mono', monospace; margin-top: 2px; }
  .brief-container { display: flex; flex-direction: column; gap: 1rem; }
  .section-card { border: 0.5px solid var(--border); border-radius: 12px; background: #fff; overflow: hidden; }
  .section-header { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-bottom: 0.5px solid var(--border); background: var(--bg-secondary); }
  .section-tag { font-family: 'IBM Plex Mono', monospace; font-size: 10px; font-weight: 500; padding: 3px 8px; border-radius: 3px; letter-spacing: 0.06em; flex-shrink: 0; }
  .tag-ai  { background: #E1F5EE; color: #0F6E56; }
  .tag-sec { background: #FCEBEB; color: #A32D2D; }
  .tag-fin { background: #E6F1FB; color: #185FA5; }
  .tag-swe { background: #EEEDFE; color: #534AB7; }
  .tag-mkt { background: #FAEEDA; color: #854F0B; }
  .tag-dive{ background: #F1EFE8; color: #5F5E5A; }
  .section-title { font-size: 13px; font-weight: 500; flex: 1; }
  .item-count { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--muted); }
  .section-body { padding: 16px; display: flex; flex-direction: column; gap: 16px; }
  .story { border-left: 2px solid var(--border); padding-left: 12px; }
  .story.high { border-left-color: var(--accent); }
  .story.med  { border-left-color: var(--warn); }
  .story-headline { font-size: 13px; font-weight: 500; margin-bottom: 5px; line-height: 1.4; }
  .story-headline a { color: inherit; text-decoration: none; border-bottom: 1px solid #d0d0d0; transition: border-color 0.15s, color 0.15s; }
  .story-headline a:hover { border-bottom-color: var(--accent); color: var(--accent); }
  .story-body { font-size: 13px; line-height: 1.68; color: #333; }
  .story-meta { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; align-items: center; }
  .chip { font-family: 'IBM Plex Mono', monospace; font-size: 10px; color: var(--muted); background: var(--bg-secondary); padding: 2px 7px; border-radius: 3px; border: 0.5px solid var(--border); }
  .ref-links { display: flex; gap: 5px; flex-wrap: wrap; }
  .ref-link { font-family: 'IBM Plex Mono', monospace; font-size: 10px; text-decoration: none; padding: 2px 8px; border-radius: 3px; transition: background 0.15s; }
  .ref-link.ai      { color: #0F6E56; background: #E1F5EE; border: 0.5px solid #9FE1CB; }
  .ref-link.ai:hover{ background: #9FE1CB; }
  .ref-link.sec     { color: #A32D2D; background: #FCEBEB; border: 0.5px solid #F7C1C1; }
  .ref-link.sec:hover{ background: #F7C1C1; }
  .ref-link.fin     { color: #185FA5; background: #E6F1FB; border: 0.5px solid #A8C8F0; }
  .ref-link.fin:hover{ background: #A8C8F0; }
  .ref-link.swe     { color: #534AB7; background: #EEEDFE; border: 0.5px solid #CECBF6; }
  .ref-link.swe:hover{ background: #CECBF6; }
  .ref-link.mkt     { color: #854F0B; background: #FAEEDA; border: 0.5px solid #FAC775; }
  .ref-link.mkt:hover{ background: #FAC775; }
  .ref-link.neutral { color: #5F5E5A; background: #F1EFE8; border: 0.5px solid #D3D1C7; }
  .ref-link.neutral:hover{ background: #D3D1C7; }
  .dive-item { display: flex; gap: 10px; padding-bottom: 14px; border-bottom: 0.5px solid var(--border); }
  .dive-item:last-child { border-bottom: none; padding-bottom: 0; }
  .dive-num { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--accent); padding-top: 2px; flex-shrink: 0; width: 20px; }
  .dive-title { font-size: 13px; font-weight: 500; margin-bottom: 4px; }
  .dive-title a { color: inherit; text-decoration: none; border-bottom: 1px solid #d0d0d0; transition: border-color 0.15s, color 0.15s; }
  .dive-title a:hover { border-bottom-color: var(--accent); color: var(--accent); }
  .dive-why { font-size: 12px; color: var(--muted); line-height: 1.55; }
  .empty-state { padding: 32px; text-align: center; font-size: 13px; color: var(--muted); }
  .footer { margin-top: 1.25rem; padding-top: 0.75rem; border-top: 0.5px solid var(--border); }
  .footer-note { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--muted); }
"""


def render_full_html(brief: dict, run_dt: datetime) -> str:
    date_obj = datetime.strptime(brief["date"], "%Y-%m-%d")
    date_stamp = date_obj.strftime("%a %d %b %Y").upper()
    footer_date = run_dt.strftime("%d %b %Y")

    sources = all_sources(brief)
    sources_label = ", ".join(sources) if sources else "—"

    is_empty = brief.get("_noContent") or brief.get("storyCount", 0) == 0

    if is_empty:
        body_inner = (
            '  <div class="section-card"><div class="empty-state">'
            "no meaningful signal in the inbox window — quiet day."
            "</div></div>"
        )
        threads = brief.get("threadCount", 0)
        stories = 0
        read_min = 1
        source_count = 0
    else:
        section_blocks = [
            section_html(key, title, brief.get("sections", {}).get(key, []))
            for key, title in SECTIONS
        ]
        section_blocks.append(deep_dives_html(brief.get("deepDives", [])))
        body_inner = "\n\n".join(b for b in section_blocks if b)

        threads = brief.get("threadCount", 0)
        stories = brief.get("storyCount", 0)
        read_min = estimate_read_minutes(brief)
        source_count = brief.get("sourceCount", 0)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SIGINT // {date_stamp}</title>
<style>{PAGE_CSS}</style>
</head>
<body>

<div class="header">
  <div style="display:flex;align-items:baseline;">
    <span class="logo">SIGINT</span>
    <span class="tagline">// daily intelligence brief</span>
  </div>
  <span class="date-stamp">{date_stamp}</span>
</div>

<div class="meta-row">
  <div class="meta-card"><div class="val">{threads}</div><div class="lbl">threads fetched</div></div>
  <div class="meta-card"><div class="val">{stories}</div><div class="lbl">stories surfaced</div></div>
  <div class="meta-card"><div class="val">~{read_min} min</div><div class="lbl">est. read time</div></div>
  <div class="meta-card"><div class="val">{source_count}</div><div class="lbl">sources</div></div>
</div>

<div class="brief-container">

{body_inner}

</div>

<div class="footer">
  <span class="footer-note">generated {footer_date} · sources: {esc(sources_label)} · sigint pipeline {PIPELINE_VERSION}</span>
</div>

</body>
</html>
"""


# ────────────────────────────────────────────────────────────────
# RSS embedded HTML (inline-styled, no JS, no CSS variables)
# ────────────────────────────────────────────────────────────────

TAG_COLORS = {
    "ai":   ("#E1F5EE", "#0F6E56", "#9FE1CB"),
    "sec":  ("#FCEBEB", "#A32D2D", "#F7C1C1"),
    "fin":  ("#E6F1FB", "#185FA5", "#A8C8F0"),
    "swe":  ("#EEEDFE", "#534AB7", "#CECBF6"),
    "mkt":  ("#FAEEDA", "#854F0B", "#FAC775"),
    "dive": ("#F1EFE8", "#5F5E5A", "#D3D1C7"),
}


def rss_section_html(key: str, title: str, stories: list[dict]) -> str:
    if not stories:
        return ""
    bg, fg, _ = TAG_COLORS[key]
    items_label = "1 item" if len(stories) == 1 else f"{len(stories)} items"
    parts = [
        f'<div style="border:0.5px solid #e5e5e5;border-radius:12px;background:#fff;margin-bottom:16px;">'
        f'<div style="display:flex;align-items:center;gap:10px;padding:10px 16px;'
        f'border-bottom:0.5px solid #e5e5e5;background:#f7f7f5;">'
        f'<span style="font-family:monospace;font-size:10px;font-weight:500;padding:3px 8px;'
        f'border-radius:3px;letter-spacing:0.06em;background:{bg};color:{fg};">'
        f'{SECTION_TAG_LABEL[key]}</span>'
        f'<span style="font-size:13px;font-weight:500;flex:1;">{esc(title)}</span>'
        f'<span style="font-family:monospace;font-size:11px;color:#888;">{items_label}</span>'
        f'</div>'
        f'<div style="padding:16px;">'
    ]

    for s in stories:
        border = "#1D9E75" if s.get("signal") == "high" else "#BA7517"
        url = s.get("url")
        headline = esc(s.get("headline", ""))
        if url:
            headline = f'<a href="{esc(url)}" style="color:inherit;text-decoration:none;border-bottom:1px solid #d0d0d0;">{headline}</a>'

        chips = "".join(
            f'<span style="font-family:monospace;font-size:10px;color:#888;background:#f7f7f5;'
            f'padding:2px 7px;border-radius:3px;border:0.5px solid #e5e5e5;margin-right:6px;">'
            f'{esc(src)}</span>'
            for src in (s.get("sources") or [])
        )

        ref_link = ""
        if url:
            domain = domain_from_url(url) or url
            lbg, lfg, lbd = TAG_COLORS[key]
            ref_link = (
                f'<a href="{esc(url)}" style="font-family:monospace;font-size:10px;'
                f'text-decoration:none;padding:2px 8px;border-radius:3px;'
                f'color:{lfg};background:{lbg};border:0.5px solid {lbd};">'
                f'{esc(domain)} ↗</a>'
            )

        parts.append(
            f'<div style="border-left:2px solid {border};padding-left:12px;margin-bottom:16px;">'
            f'<div style="font-size:13px;font-weight:500;margin-bottom:5px;line-height:1.4;">{headline}</div>'
            f'<div style="font-size:13px;line-height:1.68;color:#333;">{esc(s.get("body", ""))}</div>'
            f'<div style="margin-top:8px;">{chips}{ref_link}</div>'
            f'</div>'
        )

    parts.append("</div></div>")
    return "".join(parts)


def rss_deep_dives_html(dives: list[dict]) -> str:
    if not dives:
        return ""
    bg, fg, _ = TAG_COLORS["dive"]
    items_label = "1 item" if len(dives) == 1 else f"{len(dives)} items"
    parts = [
        f'<div style="border:0.5px solid #e5e5e5;border-radius:12px;background:#fff;margin-bottom:16px;">'
        f'<div style="display:flex;align-items:center;gap:10px;padding:10px 16px;'
        f'border-bottom:0.5px solid #e5e5e5;background:#f7f7f5;">'
        f'<span style="font-family:monospace;font-size:10px;font-weight:500;padding:3px 8px;'
        f'border-radius:3px;letter-spacing:0.06em;background:{bg};color:{fg};">DEEP DIVES</span>'
        f'<span style="font-size:13px;font-weight:500;flex:1;">Recommended deep dives</span>'
        f'<span style="font-family:monospace;font-size:11px;color:#888;">{items_label}</span>'
        f'</div>'
        f'<div style="padding:16px;">'
    ]
    for i, d in enumerate(dives, 1):
        title = esc(d.get("title", ""))
        url = d.get("url")
        title_html = (
            f'<a href="{esc(url)}" style="color:inherit;text-decoration:none;'
            f'border-bottom:1px solid #d0d0d0;">{title} ↗</a>'
            if url else title
        )
        parts.append(
            f'<div style="display:flex;gap:10px;padding-bottom:14px;'
            f'border-bottom:0.5px solid #e5e5e5;margin-bottom:14px;">'
            f'<span style="font-family:monospace;font-size:11px;color:#1D9E75;'
            f'padding-top:2px;width:20px;flex-shrink:0;">{i:02d}</span>'
            f'<div>'
            f'<div style="font-size:13px;font-weight:500;margin-bottom:4px;">{title_html}</div>'
            f'<div style="font-size:12px;color:#888;line-height:1.55;">{esc(d.get("why", ""))}</div>'
            f'</div></div>'
        )
    parts.append("</div></div>")
    return "".join(parts)


def render_rss_html(brief: dict, run_dt: datetime) -> str:
    date_str = brief["date"]
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    date_stamp = date_obj.strftime("%a %d %b %Y").upper()
    sources = all_sources(brief)
    sources_label = ", ".join(sources) if sources else "—"
    page_url = f"{SITE_BASE}/daily/{date_str}.html"

    is_empty = brief.get("_noContent") or brief.get("storyCount", 0) == 0

    header = (
        f'<p style="font-family:monospace;font-size:12px;color:#888;">'
        f'<a href="{esc(page_url)}">Open full interactive brief ↗</a>'
        f'</p>'
        f'<div style="font-family:sans-serif;color:#111;max-width:720px;">'
        f'<div style="border-bottom:1px solid #e5e5e5;padding-bottom:0.75rem;margin-bottom:1.25rem;'
        f'display:flex;align-items:baseline;justify-content:space-between;">'
        f'<div><span style="font-family:monospace;font-size:13px;font-weight:500;'
        f'letter-spacing:0.08em;color:#1D9E75;">SIGINT</span>'
        f'<span style="font-family:monospace;font-size:12px;color:#888;margin-left:12px;">'
        f'// daily intelligence brief</span></div>'
        f'<span style="font-family:monospace;font-size:11px;color:#888;">{date_stamp}</span>'
        f'</div>'
    )

    if is_empty:
        body = (
            '<div style="padding:32px;text-align:center;font-size:13px;color:#888;'
            'border:0.5px solid #e5e5e5;border-radius:12px;">'
            'no meaningful signal in the inbox window — quiet day.'
            '</div>'
        )
        meta = ""
    else:
        threads = brief.get("threadCount", 0)
        stories = brief.get("storyCount", 0)
        read_min = estimate_read_minutes(brief)
        source_count = brief.get("sourceCount", 0)
        meta = (
            f'<div style="display:flex;gap:10px;margin-bottom:1.25rem;flex-wrap:wrap;">'
            + "".join(
                f'<div style="background:#f7f7f5;border-radius:8px;padding:10px 16px;'
                f'flex:1;min-width:110px;">'
                f'<div style="font-family:monospace;font-size:20px;font-weight:500;">{val}</div>'
                f'<div style="font-size:11px;color:#888;font-family:monospace;margin-top:2px;">{lbl}</div>'
                f'</div>'
                for val, lbl in [
                    (threads, "threads fetched"),
                    (stories, "stories surfaced"),
                    (f"~{read_min} min", "est. read time"),
                    (source_count, "sources"),
                ]
            )
            + '</div>'
        )
        section_blocks = [
            rss_section_html(key, title, brief.get("sections", {}).get(key, []))
            for key, title in SECTIONS
        ]
        section_blocks.append(rss_deep_dives_html(brief.get("deepDives", [])))
        body = "".join(b for b in section_blocks if b)

    footer_date = run_dt.strftime("%d %b %Y")
    footer = (
        f'<div style="margin-top:1.25rem;padding-top:0.75rem;border-top:0.5px solid #e5e5e5;">'
        f'<span style="font-family:monospace;font-size:11px;color:#888;">'
        f'generated {footer_date} · sources: {esc(sources_label)} · sigint pipeline {PIPELINE_VERSION}'
        f'</span></div></div>'
    )

    return header + meta + body + footer


# ────────────────────────────────────────────────────────────────
# RSS feed read / prepend
# ────────────────────────────────────────────────────────────────

RSS_SKELETON = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>SIGINT Daily</title>
    <link>{SITE_BASE}/daily/</link>
    <description>Daily intelligence brief — AI, IAM &amp; cybersecurity, fintech &amp; fraud, SWE, market intel</description>
    <language>en</language>
    <ttl>60</ttl>
  </channel>
</rss>
"""


def ensure_rss_skeleton() -> str:
    if not RSS_PATH.exists():
        RSS_PATH.write_text(RSS_SKELETON, encoding="utf-8")
    return RSS_PATH.read_text(encoding="utf-8")


def prepend_rss_item(brief: dict, rss_item_html: str, run_dt: datetime) -> bool:
    """Prepend an RSS <item> for this brief. Idempotent: skips if guid exists."""
    feed = ensure_rss_skeleton()
    date_str = brief["date"]
    guid = f"sigint-daily-{date_str}"

    if f"<guid isPermaLink=\"false\">{guid}</guid>" in feed:
        return False

    date_obj = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, tzinfo=timezone(timedelta(hours=8))  # MYT
    )
    pub_date = format_datetime(date_obj)
    title_date = date_obj.strftime("%a %d %b %Y")
    page_url = f"{SITE_BASE}/daily/{date_str}.html"

    item = (
        f"    <item>\n"
        f"      <title>SIGINT // {esc(title_date)}</title>\n"
        f"      <link>{esc(page_url)}</link>\n"
        f"      <pubDate>{esc(pub_date)}</pubDate>\n"
        f"      <guid isPermaLink=\"false\">{guid}</guid>\n"
        f"      <description><![CDATA[{rss_item_html}]]></description>\n"
        f"    </item>\n"
    )

    # Insert after the closing tag of the last <channel>-level metadata element
    # (use </ttl> as the anchor — present in our skeleton).
    anchor = "</ttl>"
    idx = feed.find(anchor)
    if idx == -1:
        # Fallback: insert just before </channel>
        anchor = "</channel>"
        idx = feed.find(anchor)
        insert_at = idx
    else:
        insert_at = idx + len(anchor)

    new_feed = feed[:insert_at] + "\n" + item + feed[insert_at:]
    RSS_PATH.write_text(new_feed, encoding="utf-8")
    return True


# ────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────

def main() -> int:
    run_dt = datetime.now(timezone.utc)
    date_str = run_dt.strftime("%Y-%m-%d")

    DOCS_DAILY.mkdir(parents=True, exist_ok=True)
    BRIEFS_DAILY.mkdir(parents=True, exist_ok=True)

    html_path = DOCS_DAILY / f"{date_str}.html"
    json_path = BRIEFS_DAILY / f"{date_str}.json"

    if html_path.exists():
        print(f"[skip] {html_path.name} already exists — nothing to do.")
        return 0

    print(f"[gmail] fetching last {get_fetch_hours()}h of category:forums…")
    service = get_gmail_service()
    threads = fetch_threads(service)
    print(f"[gmail] {len(threads)} thread(s) after filtering")

    if not threads:
        print("[claude] skipped — no threads. Writing 'no content' brief.")
        brief = empty_brief(date_str)
    else:
        print(f"[claude] distilling via {MODEL}…")
        brief = distil(threads, date_str)
        brief.setdefault("date", date_str)
        brief.setdefault("threadCount", len(threads))

    json_path.write_text(json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[write] {json_path.relative_to(REPO_ROOT)}")

    html_path.write_text(render_full_html(brief, run_dt), encoding="utf-8")
    print(f"[write] {html_path.relative_to(REPO_ROOT)}")

    rss_item_html = render_rss_html(brief, run_dt)
    if prepend_rss_item(brief, rss_item_html, run_dt):
        print(f"[write] {RSS_PATH.relative_to(REPO_ROOT)} (prepended)")
    else:
        print(f"[skip] {RSS_PATH.relative_to(REPO_ROOT)} already has guid for {date_str}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        raise
