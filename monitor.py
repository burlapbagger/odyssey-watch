#!/usr/bin/env python3
"""
odyssey-watch
=============
Alerts when Harkins Arizona Mills w/ IMAX posts NEW showtimes for The Odyssey
in IMAX 70mm beyond a watermark date.

Detection is structural. Harkins renders each film as a row containing one
"movie-showtime-category" block per format, and the format itself is a logo
image identified by alt="IMAX 70mm", class "imaxSeventymm", and a src ending
in IMAX70MM. We read that, so a digital-only listing will not trigger an alert.

If the structural parse fails but the title appears in the page text, we still
alert -- flagged as unconfirmed format. Missing the real thing is worse than
an occasional imprecise alert.
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

# The premium format we care about. Matched against the logo image's alt
# text, class name, and src -- any one hit counts.
PREMIUM_PATTERN = re.compile(
    os.environ.get("PREMIUM_PATTERN", r"imax\s*70\s*mm|imaxseventymm|IMAX70MM"),
    re.I,
)

# Set REQUIRE_PREMIUM=false to alert on ANY format (useful if the 70mm run
# ends and you'd take a digital showing instead).
REQUIRE_PREMIUM = os.environ.get("REQUIRE_PREMIUM", "true").lower() != "false"

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

SHOWTIME_SELECTOR = "text=/\\d{1,2}:\\d{2}\\s*(am|pm)/i"
EMPTY_SELECTOR = (
    "text=/failed to get schedule|no showtimes|not available|"
    "check back|coming soon|too far in the future/i"
)
EMPTY_TEXT_RE = re.compile(
    r"failed to get schedule|too far in the future|no showtimes", re.I
)

# Runs inside the page. Finds the film row, then walks its format categories.
EXTRACT_JS = """
(needle) => {
  const rx = new RegExp(needle, 'i');
  const out = { rowFound: false, categories: [], titleInText: false };

  out.titleInText = rx.test(document.body.innerText || '');

  // Film titles render as an h4 with role="link".
  let heading = Array.from(document.querySelectorAll('h4'))
    .find(h => rx.test(h.textContent || ''));
  if (!heading) return out;

  // Walk up until we find an ancestor holding the showtime categories.
  let row = heading;
  for (let i = 0; i < 8 && row.parentElement; i++) {
    row = row.parentElement;
    if (row.querySelector('[data-testid="movie-showtime-category"]')) break;
  }
  const cats = row.querySelectorAll('[data-testid="movie-showtime-category"]');
  if (!cats.length) return out;
  out.rowFound = true;

  for (const cat of cats) {
    const img = cat.querySelector('img');
    const label =
      (img ? [img.alt || '', img.className || '', img.src || ''].join(' ') : '')
      || (cat.innerText || '').split('\\n')[0];
    const times = Array.from(cat.querySelectorAll('a'))
      .filter(a => /\\d{1,2}:\\d{2}\\s*(am|pm)/i.test(a.textContent || ''))
      .map(a => ({ time: a.textContent.trim(), href: a.href }));
    if (times.length) out.categories.push({ label: label.trim(), times });
  }
  return out;
}
"""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class DayResult:
    day: date
    found: bool
    times: list[str] = field(default_factory=list)
    format_label: str = ""
    booking_url: str = ""
    premium_confirmed: bool = False
    note: str = ""
    landed_url: str = ""
    rendered: bool = False
    schedule_absent: bool = False
    raw: dict = field(default_factory=dict)


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


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def probe_day(page, day: date, screenshot: bool = False) -> DayResult:
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
        rendered = True

    if screenshot:
        page.screenshot(path=f"diag-{day.isoformat()}.png", full_page=True)

    try:
        data = page.evaluate(EXTRACT_JS, TITLE_PATTERN.pattern)
    except Exception as exc:  # noqa: BLE001
        data = {"error": str(exc), "categories": [], "titleInText": False}

    base = dict(landed_url=page.url, rendered=rendered,
                schedule_absent=absent, raw=data)

    if day.isoformat() not in page.url:
        return DayResult(day=day, found=False,
                         note=f"redirected to {page.url}", **base)

    if absent:
        return DayResult(day=day, found=False,
                         note="no schedule published for this date yet", **base)

    cats = data.get("categories", [])

    # Prefer a category whose format logo says IMAX 70mm.
    premium = [c for c in cats if PREMIUM_PATTERN.search(c.get("label", ""))]
    chosen = premium[0] if premium else (cats[0] if cats and not REQUIRE_PREMIUM
                                         else None)

    if chosen:
        times = [t["time"] for t in chosen["times"]]
        return DayResult(
            day=day, found=True, times=times,
            format_label="IMAX 70mm" if premium else chosen.get("label", "")[:40],
            booking_url=chosen["times"][0]["href"] if chosen["times"] else "",
            premium_confirmed=bool(premium),
            note="", **base,
        )

    if cats:
        labels = ", ".join(c.get("label", "")[:30] for c in cats)
        return DayResult(day=day, found=False,
                         note=f"film present but not in 70mm (formats: {labels})",
                         **base)

    # Structural parse found nothing. Fall back to text so a layout change
    # can't silence us completely.
    if data.get("titleInText"):
        return DayResult(day=day, found=True, times=[],
                         format_label="unconfirmed",
                         booking_url=theatre_url(day),
                         premium_confirmed=False,
                         note="FALLBACK: title in page text but structure "
                              "not parsed -- verify manually", **base)

    note = "schedule exists but target film is not on it"
    if not rendered:
        note = "content never rendered -- possible load problem"
    return DayResult(day=day, found=False, note=note, **base)


def scan(days: list[date], screenshot: bool = False) -> list[DayResult]:
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
                results.append(probe_day(page, d, screenshot))
            except Exception as exc:  # noqa: BLE001
                print(f"warn: probe failed for {d}: {exc}", file=sys.stderr)
                results.append(DayResult(day=d, found=False, note=f"error: {exc}"))
            time.sleep(PAGE_DELAY_SEC)
        browser.close()
    return results


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def notify(title: str, message: str, priority: int = 0, url: str = "",
           url_title: str = "Open Harkins") -> None:
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
        payload["url_title"] = url_title
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
    days = [watermark, watermark + timedelta(days=1)]
    print(f"Diagnosing {days[0]} and {days[1]}")
    results = scan(days, screenshot=True)

    chunks = ["ODYSSEY-WATCH DIAGNOSTIC", f"generated: {datetime.now()}", ""]
    for r in results:
        chunks += [
            "=" * 70,
            f"DATE:        {r.day}",
            f"FOUND:       {r.found}",
            f"70MM:        {r.premium_confirmed}",
            f"FORMAT:      {r.format_label or '-'}",
            f"TIMES:       {', '.join(r.times) or '-'}",
            f"BOOKING URL: {r.booking_url or '-'}",
            f"NOTE:        {r.note or '-'}",
            "",
            "--- extracted structure ---",
            json.dumps(r.raw, indent=2)[:6000],
            "",
        ]
        print(f"  {r.day}: found={r.found} 70mm={r.premium_confirmed} "
              f"times={r.times} {r.note}")

    DIAGNOSTIC_PATH.write_text("\n".join(chunks))
    print(f"\nWrote {DIAGNOSTIC_PATH}. Download from Artifacts.")
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

        confirmed = all(r.premium_confirmed for r in after)
        lines = []
        for r in sorted(after, key=lambda x: x.day):
            times = ", ".join(r.times) or "(see page)"
            lines.append(f"<b>{r.day:%a %b %d}</b> — {r.format_label}\n{times}")

        body = ("\n\n".join(lines) + f"\n\nRuns through {last_hit:%b %d}")
        if not confirmed:
            body += "\n\n(Format unconfirmed — verify it's the IMAX auditorium.)"

        title = ("Odyssey 70mm extended - AZ Mills" if confirmed
                 else "Odyssey dates added - AZ Mills")
        first = min(after, key=lambda x: x.day)

        print(f"EXTENDED -> new horizon {last_hit}, 70mm={confirmed}")
        if notify_enabled:
            notify(title, body, priority=1,
                   url=first.booking_url or theatre_url(first.day),
                   url_title="Buy tickets")

        state.update(watermark=last_hit.isoformat(), last_alert=now,
                     last_ok_run=now, consecutive_anomalies=0)

    elif canary.found:
        print(f"NO_CHANGE -> still ends {watermark} "
              f"({len(canary.times)} times, 70mm={canary.premium_confirmed}). "
              "Staying quiet.")
        state.update(last_ok_run=now, consecutive_anomalies=0)

    else:
        n = state.get("consecutive_anomalies", 0) + 1
        state["consecutive_anomalies"] = n
        print(f"ANOMALY -> not found on {watermark} ({n} in a row). {canary.note}")
        if n == 2 and notify_enabled:
            notify("Odyssey watch needs attention",
                   f"The film wasn't found in 70mm on {watermark:%b %d}, a date "
                   "it previously played. Either the Harkins page changed or "
                   "the run was shortened. Worth checking manually.",
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
