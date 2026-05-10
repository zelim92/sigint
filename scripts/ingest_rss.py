"""RSS ingest for SIGINT.

Polls feeds listed in `sources.yaml` and returns items in the same shape the
Gmail path produced: `{"subject": ..., "sender": ..., "body": ...}`.

State (last-seen guid + pubdate per feed) is persisted to `briefs/.state.json`
so missed runs don't drop content and so newly-added feeds are bootstrapped
with only the last 24h instead of the entire feed history.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import html2text
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources.yaml"
STATE_PATH = REPO_ROOT / "briefs" / ".state.json"

BOOTSTRAP_HOURS = 24
MIN_BODY_CHARS = 100

# TODO: dedupe with generate_daily.py — FOOTNOTE_*_RE, inline_footnote_urls,
# and smart_truncate are copy-pasted from there. Pull into scripts/_text.py
# (or import from generate_daily) once the RSS path is exercised in prod.


# ────────────────────────────────────────────────────────────────
# Footnote inlining (mirrors the Gmail path so KtN-forwarded
# plain-text newsletters keep their `[N] URL` mappings after
# truncation).
# ────────────────────────────────────────────────────────────────

FOOTNOTE_LINE_RE = re.compile(r"^\s*\[(\d+)\]\s+(https?://\S+)\s*$", re.MULTILINE)
FOOTNOTE_REF_RE = re.compile(r"\[(\d+)\]")


def inline_footnote_urls(body: str) -> str:
    footnotes = {n: u for n, u in FOOTNOTE_LINE_RE.findall(body)}
    if not footnotes:
        return body
    body_no_list = FOOTNOTE_LINE_RE.sub("", body)

    def sub(m: re.Match) -> str:
        n = m.group(1)
        return f"[{n}: {footnotes[n]}]" if n in footnotes else m.group(0)

    return FOOTNOTE_REF_RE.sub(sub, body_no_list)


def smart_truncate(body: str, head: int = 10000, tail: int = 2000) -> str:
    if len(body) <= head + tail:
        return body
    return body[:head] + "\n\n[…]\n\n" + body[-tail:]


# ────────────────────────────────────────────────────────────────
# Sources + state
# ────────────────────────────────────────────────────────────────

def load_sources() -> list[dict]:
    if not SOURCES_PATH.exists():
        return []
    data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    return data.get("feeds") or []


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )


# ────────────────────────────────────────────────────────────────
# Feed parsing
# ────────────────────────────────────────────────────────────────

def _entry_pubdate(entry) -> datetime | None:
    parsed = (
        getattr(entry, "published_parsed", None)
        or getattr(entry, "updated_parsed", None)
    )
    if not parsed:
        return None
    # feedparser's *_parsed fields are UTC struct_time. Use timegm (UTC→epoch)
    # not mktime (local→epoch), otherwise pubdates skew by the local TZ offset.
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def _entry_body(entry) -> str:
    """Pick the most-complete body field, then strip HTML to markdown-ish text."""
    raw = ""
    if getattr(entry, "content", None):
        raw = (entry.content[0].get("value") or "") if entry.content else ""
    if not raw:
        raw = getattr(entry, "summary", "") or ""

    converter = html2text.HTML2Text()
    converter.body_width = 0  # don't wrap — Claude doesn't care
    converter.ignore_images = True
    text = converter.handle(raw).strip()
    return inline_footnote_urls(text)


def _sender_label(parsed_feed, fallback: str) -> str:
    feed = getattr(parsed_feed, "feed", None)
    title = getattr(feed, "title", "") if feed else ""
    if title:
        return title
    link = getattr(feed, "link", "") if feed else ""
    return urlparse(link).hostname or fallback


def fetch_feed(
    feed_cfg: dict,
    feed_state: dict,
    bootstrap_cutoff: datetime,
) -> tuple[list[dict], dict]:
    """Fetch one feed; return (new_items, updated_state_for_this_feed)."""
    name = feed_cfg["name"]
    url = feed_cfg["url"]
    parsed = feedparser.parse(url)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"feed parse failed: {parsed.bozo_exception}")

    last_seen_guid = feed_state.get("last_seen_guid")
    last_seen_pubdate_str = feed_state.get("last_seen_pubdate")
    last_seen_pubdate = (
        datetime.fromisoformat(last_seen_pubdate_str)
        if last_seen_pubdate_str
        else None
    )

    sender = _sender_label(parsed, name)
    items: list[dict] = []
    newest_guid = last_seen_guid
    newest_pubdate = last_seen_pubdate

    for entry in parsed.entries:
        guid = getattr(entry, "id", "") or getattr(entry, "link", "")
        pubdate = _entry_pubdate(entry)

        if guid and guid == last_seen_guid:
            continue
        if last_seen_pubdate and pubdate and pubdate <= last_seen_pubdate:
            continue
        # First-time feed: only pull the last BOOTSTRAP_HOURS of history.
        if last_seen_pubdate is None and pubdate and pubdate < bootstrap_cutoff:
            continue

        body = _entry_body(entry)
        if len(body.strip()) < MIN_BODY_CHARS:
            continue

        items.append({
            "subject": getattr(entry, "title", "") or "(untitled)",
            "sender": sender,
            "body": smart_truncate(body),
        })

        if pubdate and (newest_pubdate is None or pubdate > newest_pubdate):
            newest_pubdate = pubdate
            newest_guid = guid

    new_state = dict(feed_state)
    if newest_guid:
        new_state["last_seen_guid"] = newest_guid
    if newest_pubdate:
        new_state["last_seen_pubdate"] = newest_pubdate.isoformat()

    return items, new_state


def fetch_all() -> tuple[list[dict], dict, list[dict]]:
    """Returns (threads, new_state, failures).

    A dead feed never fails the whole run — its error is appended to
    `failures` and the rest continue.
    """
    sources = load_sources()
    state = load_state()
    bootstrap_cutoff = datetime.now(timezone.utc) - timedelta(hours=BOOTSTRAP_HOURS)

    threads: list[dict] = []
    new_state: dict = dict(state)
    failures: list[dict] = []

    for feed_cfg in sources:
        name = feed_cfg.get("name") or feed_cfg.get("url", "?")
        url = feed_cfg.get("url")
        if not url:
            failures.append({"name": name, "error": "missing url"})
            continue
        try:
            items, feed_state = fetch_feed(
                feed_cfg, state.get(url, {}), bootstrap_cutoff
            )
            threads.extend(items)
            new_state[url] = feed_state
        except Exception as e:
            failures.append({"name": name, "error": str(e)})

    return threads, new_state, failures
