#!/usr/bin/env python3
"""
odyssey-watch
=============
Alerts when Harkins Arizona Mills w/ IMAX posts showtimes for a target film
beyond a watermark date.

Runs entirely on GitHub's servers. Three modes, chosen from a dropdown in the
GitHub Actions tab:

  check             normal run -- probe for new dates, alert if found
  test-notification send a test push, prove the alert path works
  diagnose          save the raw page text so the reader can be corrected

Outcomes of a normal run:
  EXTENDED   -> target film found on a date after the watermark. Loud alert.
  NO_CHANGE  -> found on the watermark date, nothing after. Silent.
  ANOMALY    -> not found on the watermark date either. Warning after 2 in a row.
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

# Substring match, case-insensitive. Deliberately loose -- Harkins may list
# this as "The Odyssey", "The Odyssey: The IMAX Experience", etc.
TITLE_PATTERN = re.compile(os.environ.get("TITLE_PATTERN", r"odyssey"), re.I)

# Empty = alert on ANY format. Set to "IMAX" to require IMAX. Leave empty
# at first: you want to know the moment anything opens past the watermark.
_fmt = os.environ.get("FORMAT_PATTERN", "").strip()
FORMAT_PATTERN = re.compile(_fmt, re.I) if _fmt else None

# The last date currently showing showtimes. Set once; the script raises it
# automatically after each successful alert.
DEFAULT_WATERMARK = os.environ.get("WATERMARK", "2026-09-16")

# Days past the watermark to probe. 7 covers a full theatrical week, so a
# Thursday gap can't hide a Friday extension.
PROBE_DAYS = int(os.environ.get("PROBE_DAYS", "7"))

# Once an extension is found, how far forward to walk to find the new end.
HORIZON_SCAN_DAYS = int(os.environ.get("HORIZON_SCAN_DAYS", "45"))

STATE_PATH = Path(os.environ.get("STATE_PATH", "state.json"))
DIAGNOSTIC_PATH = Path("diagnostic.txt")

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "odyssey-watch/1.0 (personal showtime alert)",
)
PAGE_DELAY_SEC = float(os.environ.get("PAGE_DELAY_SEC", "1.5"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "25000"))

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", re.I)
FORMAT_LABEL_RE = re.compile(r"\b(IMAX(?:\s+\w+)?|Dolby Cinema|CINEXL)\b", re.I)


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


def probe_day(page, day: date, keep_full_text: bool = False) -> DayResult:
    """Load one date's theatre page and look for the target film."""
    page.goto(theatre_url(day), wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)

    # Schedule is rendered client-side. networkidle is usually enough; give
    # slow XHRs one more beat before reading.
    try:
        page.wait_for_selector(
            "text=/showtime|no showtimes|:\\d{2}\\s*(am|pm)/i", timeout=8000
        )
    except Exception:
        pass

    text = page.inner_text("body")
    full = text if keep_full_text else ""

    # Harkins bounces unknown/far-future dates back to today. If we didn't
    # land on the date we asked for, treat it as "not published yet" --
    # otherwise today's schedule would false-positive.
    if day.isoformat() not in page.url:
        return DayResult(day=day, found=False, full_text=full,
                         note=f"redirected to {page.url}")

    match = TITLE_PATTERN.search(text)
    if not match:
        return DayResult(day=day, found=False, full_text=full,
                         note="title pattern not present in page text")

    # Window of text around the title, to pull times and format labels.
    start = max(0, match.start() - 200)
    window = text[start: match.end() + 900]

    if FORMAT_PATTERN and not FORMAT_PATTERN.search(window):
        return DayResult(day=day, found=False, excerpt=window[:400],
                         full_text=full, note="title found but format filtered out")

    return DayResult(
        day=day,
        found=True,
        times=TIME_RE.findall(window),
        formats=sorted(set(FORMAT_LABEL_RE.findall(window))),
        excerpt=window[:600],
        full_text=full,
    )


def scan(days: list[date], keep_full_text: bool = False) -> list[DayResult]:
    results: list[DayResult] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            user_agent=USER_AGENT, viewport={"width": 1280, "height": 2400}
        )
        page = ctx.new_page()
        for d in days:
            try:
                results.append(probe_day(page, d, keep_full_text))
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

    r = httpx.post(
        "https://api.pushover.net/1/messages.json", data=payload, timeout=15
    )
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
        print("   Check Settings > Secrets and variables > Actions.")
        return 1
    notify(
        "Odyssey watch is wired up",
        "If you're reading this on your phone, the alerting half works. "
        "Nothing has been detected yet -- this is only a test.",
        priority=0,
    )
    print("Done. Check your phone.")
    return 0


def mode_diagnose(watermark: date) -> int:
    """Capture raw page text so the reader logic can be corrected."""
    probe = watermark + timedelta(days=2)
    print(f"Diagnosing: {watermark} (should HAVE showtimes) "
          f"and {probe} (should NOT, yet)")

    results = scan([watermark, probe], keep_full_text=True)

    chunks = ["ODYSSEY-WATCH DIAGNOSTIC", f"generated: {datetime.now()}", ""]
    for r in results:
        chunks += [
            "=" * 70,
            f"DATE:      {r.day}",
            f"URL:       {theatre_url(r.day)}",
            f"FOUND:     {r.found}",
            f"NOTE:      {r.note or '-'}",
            f"TIMES:     {', '.join(r.times) or '-'}",
            f"FORMATS:   {', '.join(r.formats) or '-'}",
            "",
            "--- matched window ---",
            r.excerpt or "(nothing matched)",
            "",
            "--- full page text (first 15000 chars) ---",
            (r.full_text or "(empty)")[:15000],
            "",
        ]
        print(f"  {r.day}: found={r.found} times={len(r.times)} {r.note}")

    DIAGNOSTIC_PATH.write_text("\n".join(chunks))
    print(f"\nWrote {DIAGNOSTIC_PATH} "
          f"({DIAGNOSTIC_PATH.stat().st_size} bytes). "
          "Download it from the Artifacts section of this run.")
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

    if after:
        last_hit = max(r.day for r in after)
        # Walk forward a week at a time to find the true new horizon.
        while (last_hit - watermark).days <= HORIZON_SCAN_DAYS:
            chunk = [last_hit + timedelta(days=i) for i in range(1, 8)]
            hits = [r for r in scan(chunk) if r.found]
            if not hits:
                break
            after.extend(hits)
            last_hit = max(r.day for r in hits)

        lines = []
        for r in sorted(after, key=lambda x: x.day):
            times = ", ".join(r.times[:12]) or "(times not parsed)"
            fmts = "/".join(r.formats)
            lines.append(f"<b>{r.day:%a %b %d}</b> {fmts}\n{times}")

        body = (
            f"New dates are live past {watermark:%b %d}.\n\n"
            + "\n\n".join(lines)
            + f"\n\nNew last date: {last_hit:%b %d}"
        )
        print(f"EXTENDED -> new horizon {last_hit}")
        if notify_enabled:
            notify(
                "Odyssey extended - AZ Mills IMAX",
                body,
                priority=1,
                url=theatre_url(min(r.day for r in after)),
            )

        state.update(
            watermark=last_hit.isoformat(),
            last_alert=now,
            last_ok_run=now,
            consecutive_anomalies=0,
        )

    elif canary.found:
        print(f"NO_CHANGE -> still ends {watermark}. Staying quiet.")
        state.update(last_ok_run=now, consecutive_anomalies=0)

    else:
        n = state.get("consecutive_anomalies", 0) + 1
        state["consecutive_anomalies"] = n
        print(f"ANOMALY -> not found on {watermark} ({n} in a row). "
              f"{canary.note}")
        if n == 2 and notify_enabled:
            notify(
                "Odyssey watch needs attention",
                f"The film wasn't found on {watermark:%b %d}, a date it "
                "previously played. Either the Harkins page changed or the "
                "run was shortened. Worth checking manually.",
                priority=0,
                url=theatre_url(watermark),
            )

    return state


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="no notifications, no state write")
    ap.add_argument("--silent", action="store_true",
                    help="write state, send nothing")
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
