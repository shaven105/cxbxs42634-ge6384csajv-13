#!/usr/bin/env python3
"""Daily StarLux (JX) Taipei(TPE)<->Bangkok(BKK) fare tracker.

Pulls the server-rendered fare page StarLux publishes at
flights-from-taipei-to-bangkok. That page embeds two things we can read
with a plain HTTP request (no headless browser / login needed):

1. A rolling ~6-month "monthly fare card" carousel, each card showing the
   cheapest round-trip Economy fare StarLux has recently cached for that
   month (e.g. "December 2026, From TWD9,622, Seen: 1 day ago"). This is
   the metric we track daily as our TARGET_MONTH price signal.
2. A small "fares" sample array of a handful of recently-cached searches
   (any travel class, any date pair). We opportunistically check it for a
   fare that actually matches our target departure/return dates.

Neither StarLux's own booking engine nor Google Flights can be queried for
an exact date-pair total fare without a JS-executing browser session and
those sites actively resist automation, so this script does NOT attempt
that. It tracks the best available *public, unauthenticated* signal for
the route instead, and calls that out honestly in every report.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

FARE_PAGE_URL = "https://www.starlux-airlines.com/flights/en/flights-from-taipei-to-bangkok"
ORIGIN, DEST = "TPE", "BKK"

# The trip we actually care about.
TARGET_DEPARTURE = "2026-12-04"
TARGET_RETURN = "2026-12-08"
TARGET_MONTH_LABEL = "December 2026"

DATA_DIR = Path(__file__).parent / "data"
CSV_PATH = DATA_DIR / "price_history.csv"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MONTHLY_CARD_RE = re.compile(
    r"slide \d+ of \d+, (?P<month>[A-Za-z]+ \d{4}), From (?P<currency>[A-Z]{3})"
    r"(?P<price>[\d,]+).*?Seen: (?P<seen_value>\d+) (?P<seen_unit>\w+?)s?"
    r"(?:,|\s+ago)"
)


@dataclass
class FareReading:
    checked_at_utc: str
    target_month_label: str
    target_month_price_twd: float | None
    target_month_seen: str | None
    exact_match_price_twd: float | None
    exact_match_note: str | None
    nearby_price_twd: float | None
    nearby_departure: str | None
    nearby_return: str | None


def fetch_page_html() -> str:
    req = Request(FARE_PAGE_URL, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def parse_monthly_cards(html: str) -> dict[str, dict]:
    cards: dict[str, dict] = {}
    for m in MONTHLY_CARD_RE.finditer(html):
        month = m.group("month")
        cards[month] = {
            "price": float(m.group("price").replace(",", "")),
            "currency": m.group("currency"),
            "seen": f"{m.group('seen_value')} {m.group('seen_unit')}(s) ago",
        }
    return cards


def parse_fares_sample(html: str) -> list[dict]:
    idx = html.find('"fares":[')
    if idx == -1:
        return []
    start = html.find("[", idx)
    depth = 0
    end = None
    for j in range(start, len(html)):
        if html[j] == "[":
            depth += 1
        elif html[j] == "]":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end is None:
        return []
    try:
        return json.loads(html[start : end + 1])
    except json.JSONDecodeError:
        return []


def find_exact_or_nearby(fares: list[dict]) -> tuple[dict | None, dict | None]:
    exact = None
    nearby = None
    for fare in fares:
        if fare.get("originAirportCode") != ORIGIN or fare.get("destinationAirportCode") != DEST:
            continue
        dep, ret = fare.get("departureDate"), fare.get("returnDate")
        if dep == TARGET_DEPARTURE and ret == TARGET_RETURN:
            exact = fare
        elif dep and ret and abs(_days_between(dep, TARGET_DEPARTURE)) <= 4:
            nearby = fare
    return exact, nearby


def _days_between(a: str, b: str) -> int:
    fmt = "%Y-%m-%d"
    return (datetime.strptime(a, fmt) - datetime.strptime(b, fmt)).days


def build_reading() -> FareReading:
    html = fetch_page_html()
    monthly = parse_monthly_cards(html)
    target = monthly.get(TARGET_MONTH_LABEL)

    fares = parse_fares_sample(html)
    exact, nearby = find_exact_or_nearby(fares)

    return FareReading(
        checked_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        target_month_label=TARGET_MONTH_LABEL,
        target_month_price_twd=target["price"] if target else None,
        target_month_seen=target["seen"] if target else None,
        exact_match_price_twd=exact.get("totalPrice") if exact else None,
        exact_match_note="economy" if exact and exact.get("travelClass") == "ECONOMY" else (
            exact.get("travelClass") if exact else None
        ),
        nearby_price_twd=nearby.get("totalPrice") if nearby else None,
        nearby_departure=nearby.get("departureDate") if nearby else None,
        nearby_return=nearby.get("returnDate") if nearby else None,
    )


def append_to_csv(reading: FareReading) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(reading).keys()))
        if is_new:
            writer.writeheader()
        writer.writerow(asdict(reading))


def previous_reading() -> dict | None:
    if not CSV_PATH.exists():
        return None
    with CSV_PATH.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-2] if len(rows) >= 2 else None


if __name__ == "__main__":
    reading = build_reading()
    append_to_csv(reading)
    print(json.dumps(asdict(reading), indent=2, ensure_ascii=False))
    if reading.target_month_price_twd is None:
        # Non-fatal: page structure may have changed. Surface it loudly in CI logs.
        print("WARNING: could not find target month fare card — page markup may have changed", file=sys.stderr)
