#!/usr/bin/env python3
"""
odyssey-watch
=============
Alerts when Harkins Arizona Mills w/ IMAX posts showtimes for a target film
beyond a watermark date.

Modes (dropdown in the GitHub Actions tab):
  check             normal run -- probe for new dates, alert if found
  test-notification send a test push, prove the alert path works
  diagnose          save raw page text + screenshots for troubleshooting
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

THEATRE_SLUG = os.environ.get("THEATRE_SLUG", "arizona-mills-w-imax")
BASE_URL = "https://www.harkins.com/theatres/{slug}/{date}"

TITLE_PATTERN = re.compile(os.environ.get("TITLE_PATTERN", r"odyssey"), re.I)

_fmt = os.environ.get("FORMAT_PATTERN", "").strip()
FORMAT_PATTERN = re.compile(_fmt, re.I) if _fmt else None

DEFAULT_WATERMARK = os.environ.get("WATERMARK", "2026-09-16")
PROBE_DAYS = int(os.environ.get("PROBE_DAYS", "7"))
HORIZON_SCAN_DAYS = int(os.environ.get("HORIZON_SCAN_DAYS", "45"))

STATE_PATH = Path(os.environ.get("STATE_PATH", "state.json"))
DIAGNOSTIC_PATH = Path("diagnostic.txt")

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")

NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "45000"))
CONTENT_TIMEOUT_MS = int(os.environ.get("CONTENT_TIMEOUT_MS", "20000"))
SETTLE_MS = int(os.environ.get("SETTLE_MS", "3000"))
PAGE_DELAY_SEC = float(os.environ.get("PAGE_DELAY_SEC", "1.5"))

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", re.I)
FORMAT_LABEL_RE = re.compile(
    r"\b(IMAX(?:\s*70\s*MM)?|Dolby Cinema|CIN\u00c9XL|CINEXL|Digital)\b", re.I
)

# A film's rating line, e.g. "R  2h 52m  ALD" -- this is what separates one
# film's block from the next, and it's how we stop a film's showtime list
# from bleeding into the next film's times.
RATING_LINE_RE = re.compile(
    r"\b(?:G|PG|PG-?13|R|NC-?17|NR)\b[^\n]{0,20}\d+h\s*\d+m", re.I
)

SHOWTIME_SELECTOR = "text=/\\d{1,2}:\\d{2}\\s*(am|pm)/i"
# Harkins' exact wording when a date has no published schedule, plus the
# usual variants in case they reword it.
EMPTY_SELECTOR = (
    "text=/failed to get schedule|no showtimes|not available|"
    "check back|coming soon|too far in the future/i"
)
EMPTY_TEXT_RE = re.compile(
    r"failed to get schedule|too far in the future|no showtimes", re.I
)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class DayResult:
    day: date
    found: bool
    times: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    excerpt: str = ""
    full_text: str = ""
    note: str = ""
    page_title: str = ""
    html_len: int = 0
    landed_url: str = ""
    rendered: bool = False
    schedule_absent: bool = False   # page loaded and said "no schedule"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            print("warn: state.json unreadable, starting fresh", file=sys.stderr)
    return {
        "watermark": DEFAULT_WATERMARK,
        "last_ok_run": None,
        "last_alert": None,
        "consecutive_anomalies": 0,
    }


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def theatre_url(day: date) -> str:
    return BASE_URL.format(slug=THEATRE_SLUG, date=day.isoformat())


def film_block(text: str, match: re.Match) -> str:
    """
    Return just the target film's block of text.

    Layout is: Title / rating line / genres / [format] / times... / NextTitle /
    next rating line. So we find this film's rating line, then cut at the NEXT
    rating line. That boundary sits after our times and before the next
    film's times.
    """
    own = RATING_LINE_RE.search(text, match.end())
    if not own:
        return text[match.start(): match.end() + 500]
    nxt = RATING_LINE_RE.search(text, own.end())
    end = nxt.start() if nxt else min(len(text), own.end() + 500)
    return text[match.start(): end]


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def probe_day(page, day: date, keep_full_text: bool = False,
              screenshot: bool = False) -> DayResult:
    url = theatre_url(day)

    nav_error = None
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            nav_error = None
            break
        except Exception as exc:  # noqa: BLE001
            nav_error = exc
            if attempt == 1:
                print(f"    {day}: navigation retry after: {exc}")
                time.sleep(3)

    if nav_error is not None:
        return DayResult(day=day, found=False,
                         note=f"navigation failed twice: {nav_error}")

    rendered = False
    for selector, timeout in ((SHOWTIME_SELECTOR, CONTENT_TIMEOUT_MS),
                              (EMPTY_SELECTOR, 6000)):
        try:
            page.wait_for_selector(selector, timeout=timeout)
            rendered = True
            break
        except Exception:
            continue

    page.wait_for_timeout(SETTLE_MS)

    text = page.inner_text("body")
    absent = bool(EMPTY_TEXT_RE.search(text))
    if absent:
        rendered = True   # the page DID load; it just has nothing to show

    if screenshot:
        page.screenshot(path=f"diag-{day.isoformat()}.png", full_page=True)

    base = dict(
        full_text=text if keep_full_text else "",
        page_title=page.title(),
        html_len=len(page.content()),
        landed_url=page.url,
        rendered=rendered,
        schedule_absent=absent,
    )

    if day.isoformat() not in page.url:
        return DayResult(day=day, found=False,
                         note=f"redirected to {page.url}", **base)

    if absent:
        return DayResult(day=day, found=False,
                         note="no schedule published for this date yet", **base)

    match = TITLE_PATTERN.search(text)
    if not match:
        note = "schedule exists but target film is not on it"
        if not rendered:
            note = "content never rendered -- possible load problem"
        return DayResult(day=day, found=False, note=note, **base)

    block = film_block(text, match)

    if FORMAT_PATTERN and not FORMAT_PATTERN.search(block):
        return DayResult(day=day, found=False, excerpt=block[:400],
                         note="title found but format filtered out", **base)

    return DayResult(
        day=day,
        found=True,
        times=TIME_RE.findall(block),
        formats=sorted(set(FORMAT_LABEL_RE.findall(block))),
        excerpt=block[:700],
        **base,
    )


def scan(days: list[date], keep_full_text: bool = False,
         screenshot: bool = False) -> list[DayResult]:
    results: list[DayResult] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 2400},
            locale="en-US",
            timezone_id="America/Phoenix",
        )
        ctx.set_default_timeout(NAV_TIMEOUT_MS)
        page = ctx.new_page()
        for d in days:
            try:
                results.append(probe_day(page, d, keep_full_text, screenshot))
            except Exception as exc:  # noqa: BLE001
                print(f"warn: probe failed for {d}: {exc}", file=sys.stderr)
                results.append(DayResult(day=d, found=False, note=f"error: {exc}"))
            time.sleep(PAGE_DELAY_SEC)
        browser.close()
    return results


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def notify(title: str, message: str, priority: int = 0, url: str = "") -> None:
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        print("!! No Pushover credentials found. Would have sent:")
        print(f"   {title}\n   {message}")
        return

    payload = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message,
        "priority": priority,
        "html": 1,
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "Open Harkins"
    if priority == 1:
        payload["sound"] = "persistent"

    r = httpx.post("https://api.pushover.net/1/messages.json",
                   data=payload, timeout=15)
    if r.status_code != 200:
        print(f"!! Pushover rejected the message: {r.status_code} {r.text}")
    r.raise_for_status()
    print("Pushover accepted the message.")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def mode_test_notify() -> int:
    print("Sending a test notification...")
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        print("!! PUSHOVER_TOKEN and/or PUSHOVER_USER are empty.")
        return 1
    notify("Odyssey watch is wired up",
           "If you're reading this on your phone, the alerting half works.",
           priority=0)
    print("Done. Check your phone.")
    return 0


def mode_diagnose(watermark: date) -> int:
    days = [watermark, watermark + timedelta(days=1),
            watermark + timedelta(days=2)]
    print(f"Diagnosing {days[0]} (should HAVE showtimes) then "
          f"{days[1]} and {days[2]} (should NOT, yet)")

    results = scan(days, keep_full_text=True, screenshot=True)

    chunks = ["ODYSSEY-WATCH DIAGNOSTIC", f"generated: {datetime.now()}", ""]
    for r in results:
        chunks += [
            "=" * 70,
            f"DATE:        {r.day}",
            f"LANDED ON:   {r.landed_url or '-'}",
            f"HTML SIZE:   {r.html_len}",
            f"RENDERED:    {r.rendered}",
            f"NO SCHEDULE: {r.schedule_absent}",
            f"FOUND:       {r.found}",
            f"NOTE:        {r.note or '-'}",
            f"TIMES:       {', '.join(r.times) or '-'}",
            f"FORMATS:     {', '.join(r.formats) or '-'}",
            "",
            "--- film block ---",
            r.excerpt or "(nothing matched)",
            "",
            "--- full page text (first 15000 chars) ---",
            (r.full_text or "(empty)")[:15000],
            "",
        ]
        print(f"  {r.day}: found={r.found} absent={r.schedule_absent} "
              f"times={len(r.times)} {r.note}")

    DIAGNOSTIC_PATH.write_text("\n".join(chunks))
    print(f"\nWrote {DIAGNOSTIC_PATH} plus screenshots. Download from Artifacts.")
    return 0


def mode_check(state: dict, notify_enabled: bool) -> dict:
    watermark = date.fromisoformat(state["watermark"])
    probe_days = [watermark] + [
        watermark + timedelta(days=i) for i in range(1, PROBE_DAYS + 1)
    ]
    print(f"Watermark: {watermark}. Probing {len(probe_days)} dates...")

    results = scan(probe_days)
    canary = results[0]
    after = [r for r in results[1:] if r.found]
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    # Record how far out Harkins publishes ANY schedule. If this number
    # climbs by one every day, they use a rolling booking window.
    published = [r.day for r in results if not r.schedule_absent and r.rendered]
    if published:
        state["furthest_schedule_seen"] = max(published).isoformat()

    if after:
        last_hit = max(r.day for r in after)
        while (last_hit - watermark).days <= HORIZON_SCAN_DAYS:
            chunk = [last_hit + timedelta(days=i) for i in range(1, 8)]
            hits = [r for r in scan(chunk) if r.found]
            if not hits:
                break
            after.extend(hits)
            last_hit = max(r.day for r in hits)

        lines = []
        for r in sorted(after, key=lambda x: x.day):
            times = ", ".join(r.times) or "(times not parsed)"
            lines.append(f"<b>{r.day:%a %b %d}</b>\n{times}")

        body = (f"New dates are live past {watermark:%b %d}.\n\n"
                + "\n\n".join(lines)
                + f"\n\nNew last date: {last_hit:%b %d}")
        print(f"EXTENDED -> new horizon {last_hit}")
        if notify_enabled:
            notify("Odyssey extended - AZ Mills IMAX", body, priority=1,
                   url=theatre_url(min(r.day for r in after)))

        state.update(watermark=last_hit.isoformat(), last_alert=now,
                     last_ok_run=now, consecutive_anomalies=0)

    elif canary.found:
        print(f"NO_CHANGE -> still ends {watermark}. Staying quiet.")
        state.update(last_ok_run=now, consecutive_anomalies=0)

    else:
        n = state.get("consecutive_anomalies", 0) + 1
        state["consecutive_anomalies"] = n
        print(f"ANOMALY -> not found on {watermark} ({n} in a row). {canary.note}")
        if n == 2 and notify_enabled:
            notify("Odyssey watch needs attention",
                   f"The film wasn't found on {watermark:%b %d}, a date it "
                   "previously played. Either the Harkins page changed or the "
                   "run was shortened. Worth checking manually.",
                   priority=0, url=theatre_url(watermark))

    return state


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--silent", action="store_true")
    args = ap.parse_args()

    if args.test_notify:
        return mode_test_notify()

    state = load_state()

    if args.diagnose:
        return mode_diagnose(date.fromisoformat(state["watermark"]))

    state = mode_check(state, notify_enabled=not (args.dry_run or args.silent))

    if not args.dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
