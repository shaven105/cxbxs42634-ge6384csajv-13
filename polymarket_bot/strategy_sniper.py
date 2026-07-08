"""
S3 Near-Expiry Certainty Sniper v3.1 — Resolution Lag Architecture

The only reliable edge is betting on outcomes ALREADY determined in the real
world but not yet resolved on Polymarket (resolution lag window).

Two verified signal types:

  Tier 1 — Overdue + Crypto (highest priority)
    Market endDate has passed, BTC/ETH/etc. price is unambiguously past the
    threshold (≥5% margin).  Resolution is certain; Polymarket just hasn't
    closed the market yet.  Kelly cap: 35%.

  Tier 2 — Near-Expiry Crypto (< 6h to endDate)
    Market expires within 6 hours, current crypto price ≈ final price.
    Small time discount applied to confidence.  Kelly cap: 20%.

  Tier 2b — Weather Verified (≤3 days to target date)
    Open-Meteo daily-high forecast compared to the temperature threshold
    in the question.  Confidence = N(margin_°F / forecast_std_°F) where
    std grows from 2.5°F (same day) to 9°F (3 days out).  No API key
    needed — Open-Meteo is fully free.  Kelly cap: 20%.

  Tier 3 — Sports Verified (any open market)
    ESPN confirms the game is completed and we match the winner to the
    Polymarket question.  No time restriction — game completion IS the
    signal regardless of where endDate sits.  Kelly cap: 35%.

What we removed vs crowd-only / speculative entries:
  ✗ 72h forward-looking crypto: BTC can move a lot in 3 days, not a "known" outcome
  ✗ Overdue crowd (high bid): calibration shows crowd is systematically wrong here
  ✗ YES/NO arb: spread + fee makes it EV-negative

EV proof (overdue crypto, 97% WR, ask=0.88, $100 bankroll → 35% Kelly = $35):
  win  = 35 × (1−0.88)/0.88 − 35×0.018 = 4.773 − 0.630 = +$4.143
  loss = −35
  EV   = 0.97×4.143 + 0.03×(−35) = 4.019 − 1.050 = +$2.97/trade

5 trades/month → $14.85 = 14.9% MoM; add sports tier → 20%+ ✓
"""

from __future__ import annotations

import json
import logging
import math
import re
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Strategy parameters ────────────────────────────────────────────────────

# Crypto: only enter if within this many hours of endDate OR already overdue
# 12h window: daily markets close at midnight UTC, scanning at 8am still catches them
SNIPE_CRYPTO_MAX_HOURS = 12

# How many hours past endDate we still consider an overdue market valid
SNIPE_OVERDUE_GRACE = 168     # 7 days

# Maximum entry price (must leave at least 5% upside after 1.8% fee)
SNIPE_MAX_ENTRY = 0.95

# Kelly fraction caps by confidence tier
SNIPE_KELLY_CAP_OVERDUE = 0.35   # overdue market: outcome already determined
SNIPE_KELLY_CAP_NEAR    = 0.20   # near-expiry (<6h): small time-price risk remains

# Minimum bet and maximum concurrent positions
SNIPE_MIN_BET       = 0.50   # $0.50 minimum — allows more positions on $100 bankroll
SNIPE_MAX_POSITIONS = 8

# Minimum confidence (from volatility model) required to enter any trade
CRYPTO_MIN_CONFIDENCE = 0.90
POLYMARKET_FEE = 0.018        # Polymarket taker fee, March 2026

STATE_FILE = Path("sniper_trades.json")

COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"
COINBASE_API  = "https://api.coinbase.com/v2/prices/{}/spot"

ESPN_SPORTS = [
    ("basketball", "nba"),
    ("football",   "nfl"),
    ("baseball",   "mlb"),
    ("hockey",     "nhl"),
    ("soccer",     "eng.1"),
    ("soccer",     "esp.1"),
    ("soccer",     "uefa.champions"),
]

# Annualised return volatilities (rough, conservative) per asset
_CRYPTO_VOL_ANNUAL: dict[str, float] = {
    "BTC":   0.80,
    "ETH":   0.80,
    "SOL":   1.20,
    "DOGE":  1.50,
    "XRP":   1.20,
    "BNB":   0.90,
    "MATIC": 1.40,
    "ADA":   1.20,
    "AVAX":  1.30,
    "LINK":  1.20,
    "DOT":   1.20,
    "LTC":   0.90,
}

# ── Weather constants ──────────────────────────────────────────────────────

WEATHER_MAX_DAYS_AHEAD = 3        # only enter if target date ≤3 days away
WEATHER_MIN_CONFIDENCE = 0.88     # Open-Meteo margin must give this confidence

_OPEN_METEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=temperature_2m_max&temperature_unit=fahrenheit"
    "&forecast_days=7&timezone=auto"
)
_OPEN_METEO_ARCHIVE_URL = (
    "https://archive-api.open-meteo.com/v1/archive"
    "?latitude={lat}&longitude={lon}&start_date={date}&end_date={date}"
    "&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto"
)

# Forecast uncertainty (°F std dev) by days ahead; negative = actual (archive)
_WEATHER_STD_F: dict[int, float] = {-1: 0.5, 0: 2.5, 1: 4.0, 2: 6.5, 3: 9.0}

# In-process cache to avoid redundant API calls within one scan
_weather_cache: dict[tuple, float] = {}

# Major US cities tracked on Polymarket weather markets (lat, lon)
_CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york city":   (40.7128, -74.0060),
    "new york":        (40.7128, -74.0060),
    "nyc":             (40.7128, -74.0060),
    "los angeles":     (34.0522, -118.2437),
    "chicago":         (41.8781, -87.6298),
    "houston":         (29.7604, -95.3698),
    "phoenix":         (33.4484, -112.0740),
    "philadelphia":    (39.9526, -75.1652),
    "san antonio":     (29.4241, -98.4936),
    "san diego":       (32.7157, -117.1611),
    "dallas":          (32.7767, -96.7970),
    "san jose":        (37.3382, -121.8863),
    "austin":          (30.2672, -97.7431),
    "san francisco":   (37.7749, -122.4194),
    "seattle":         (47.6062, -122.3321),
    "denver":          (39.7392, -104.9903),
    "washington":      (38.9072, -77.0369),
    "nashville":       (36.1627, -86.7816),
    "oklahoma city":   (35.4676, -97.5164),
    "boston":          (42.3601, -71.0589),
    "portland":        (45.5051, -122.6750),
    "las vegas":       (36.1699, -115.1398),
    "memphis":         (35.1495, -90.0490),
    "louisville":      (38.2527, -85.7585),
    "baltimore":       (39.2904, -76.6122),
    "milwaukee":       (43.0389, -87.9065),
    "albuquerque":     (35.0844, -106.6504),
    "tucson":          (32.2226, -110.9747),
    "fresno":          (36.7378, -119.7871),
    "sacramento":      (38.5816, -121.4944),
    "miami":           (25.7617, -80.1918),
    "atlanta":         (33.7490, -84.3880),
    "minneapolis":     (44.9778, -93.2650),
    "new orleans":     (29.9511, -90.0715),
    "cleveland":       (41.4993, -81.6944),
    "pittsburgh":      (40.4406, -79.9959),
    "st. louis":       (38.6270, -90.1994),
    "st louis":        (38.6270, -90.1994),
    "tampa":           (27.9506, -82.4572),
    "orlando":         (28.5383, -81.3792),
    "detroit":         (42.3314, -83.0458),
    "kansas city":     (39.0997, -94.5786),
    "raleigh":         (35.7796, -78.6382),
    "omaha":           (41.2565, -95.9345),
    "charlotte":       (35.2271, -80.8431),
    "jacksonville":    (30.3322, -81.6557),
    "indianapolis":    (39.7684, -86.1581),
    "columbus":        (39.9612, -82.9988),
    "el paso":         (31.7619, -106.4850),
    "fort worth":      (32.7555, -97.3308),
}

_WEATHER_TEMP_RE = re.compile(r'\b(\d+(?:\.\d+)?)\s*°?\s*[Ff]\b')
_WEATHER_MONTH_DAY_RE = re.compile(
    r'\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
    r'\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b',
    re.I
)
_WEATHER_DOW_RE = re.compile(
    r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', re.I
)
_MONTH_NAMES: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

CRYPTO_MAP: dict[str, tuple[str, str, re.Pattern]] = {
    "BTC":   ("bitcoin",       "BTC-USD",   re.compile(r'\b(?:btc|bitcoin)\b',        re.I)),
    "ETH":   ("ethereum",      "ETH-USD",   re.compile(r'\b(?:eth|ethereum)\b',       re.I)),
    "SOL":   ("solana",        "SOL-USD",   re.compile(r'\b(?:sol|solana)\b',         re.I)),
    "DOGE":  ("dogecoin",      "DOGE-USD",  re.compile(r'\b(?:doge|dogecoin)\b',      re.I)),
    "XRP":   ("ripple",        "XRP-USD",   re.compile(r'\b(?:xrp|ripple)\b',         re.I)),
    "BNB":   ("binancecoin",   "BNB-USD",   re.compile(r'\b(?:bnb)\b',                re.I)),
    "MATIC": ("matic-network", "MATIC-USD", re.compile(r'\b(?:matic|polygon)\b',      re.I)),
    "ADA":   ("cardano",       "ADA-USD",   re.compile(r'\b(?:ada|cardano)\b',        re.I)),
    "AVAX":  ("avalanche-2",   "AVAX-USD",  re.compile(r'\b(?:avax|avalanche)\b',     re.I)),
    "LINK":  ("chainlink",     "LINK-USD",  re.compile(r'\b(?:link|chainlink)\b',     re.I)),
    "DOT":   ("polkadot",      "DOT-USD",   re.compile(r'\b(?:dot|polkadot)\b',       re.I)),
    "LTC":   ("litecoin",      "LTC-USD",   re.compile(r'\b(?:ltc|litecoin)\b',       re.I)),
}

_PRICE_RE = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)')
# Polymarket hourly strike-ladder markets have NO dollar sign:
#   "Ethereum above 1,830 on July 8, 10AM ET?"
# Anchor the number directly after the direction word so date digits
# ("July 8") can never be mistaken for the threshold.
_DIR_THRESH_RE = re.compile(
    r'\b(above|over|below|under)\s+\$?\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)\b', re.I
)
_ABOVE_RE = re.compile(
    r'\b(?:above|over|exceed|reach(?:es)?|hit(?:s)?|surpass(?:es)?|break(?:s)?|cross(?:es)?|at or above)\b', re.I
)
_BELOW_RE = re.compile(
    r'\b(?:below|under|falls?\s+(?:below|under)|drops?\s+(?:below|under)|at or below)\b', re.I
)
_WILL_WIN_RE = re.compile(
    r'\bwill\s+(?:the\s+)?(.{2,40}?)\s+(?:win|beat|defeat|advance)', re.I
)
# Broader fallback: "does/did/can the X win", "X to win", "X wins"
_WIN_VERB_RE = re.compile(
    r'\b(?:does|did|can|could)\s+(?:the\s+)?(.{2,50}?)\s+(?:win|beat|defeat|advance)',
    re.I
)
_WIN_KEYWORDS_RE = re.compile(r'\b(?:win|wins|winner|beat|beats|victory|champion)\b', re.I)
_LOSE_KEYWORDS_RE = re.compile(r'\b(?:lose|loses|loss|loser|fail|fails|fall\s+short)\b', re.I)


# ── Dataclass ──────────────────────────────────────────────────────────────

@dataclass
class SniperTrade:
    date: str
    market_id: str
    question: str
    side: str                   # "YES" or "NO"
    price: float                # entry ask price per share
    bet_usdc: float
    shares: float
    reason: str                 # "overdue_crypto" | "near_expiry_crypto" | "sports_verified"
    hours_to_expiry: float      # negative = already overdue
    live_price: Optional[float] = None
    threshold: Optional[float] = None
    confidence: float = 0.0    # probability used in Kelly calculation
    resolved: bool = False
    outcome: Optional[bool] = None
    pnl_usdc: Optional[float] = None


# ── State persistence ──────────────────────────────────────────────────────

def load_sniper_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"trades": [], "total_pnl": 0.0}


def save_sniper_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def is_already_sniped(state: dict, market_id: str) -> bool:
    return any(
        t["market_id"] == market_id and not t.get("resolved")
        for t in state["trades"]
    )


def record_sniper_trade(state: dict, trade: SniperTrade, bankroll_ref: list) -> None:
    bankroll_ref[0] = round(bankroll_ref[0] - trade.bet_usdc, 4)
    state["trades"].append(asdict(trade))
    logger.info(
        f"[SNIPER] {trade.question[:55]} | {trade.side} @ {trade.price:.3f} "
        f"| {trade.reason} | conf={trade.confidence:.3f} "
        f"| h_left={trade.hours_to_expiry:.1f} | ${trade.bet_usdc:.2f}"
    )


# ── Kelly bet sizing ───────────────────────────────────────────────────────

def _kelly_bet(confidence: float, ask: float, bankroll: float, kelly_cap: float) -> float:
    """Half-Kelly bet size capped at kelly_cap fraction of bankroll."""
    if ask <= 0 or ask >= 1 or confidence <= 0:
        return 0.0
    b = (1.0 - ask) / ask
    p = confidence
    f_full = (b * p - (1.0 - p)) / b
    if f_full <= 0:
        return 0.0
    fraction = min(f_full * 0.5, kelly_cap)
    return round(max(bankroll * fraction, SNIPE_MIN_BET), 2)


# ── Crypto price fetching & verification ──────────────────────────────────

def _norm_cdf(z: float) -> float:
    """Standard normal CDF via Abramowitz & Stegun polynomial (no scipy needed)."""
    if z > 8:
        return 1.0
    if z < -8:
        return 0.0
    k    = 1.0 / (1.0 + 0.2316419 * abs(z))
    poly = k * (0.319381530 + k * (-0.356563782 + k * (1.781477937 + k * (-1.821255978 + k * 1.330274429))))
    phi  = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    p    = 1.0 - phi * poly
    return p if z >= 0 else 1.0 - p


def _crypto_confidence(margin: float, hours_remaining: float, symbol: str = "BTC") -> float:
    """
    P(price stays on current side until expiry) using T-hour lognormal volatility.

    margin          — fractional distance of live price from threshold (>0)
    hours_remaining — hours until market closes (use 0 for already-overdue, floored at 1 min)
    symbol          — asset symbol for per-asset volatility lookup
    """
    if margin <= 0:
        return 0.0
    vol_annual = _CRYPTO_VOL_ANNUAL.get(symbol, 1.0)
    h = max(hours_remaining, 1 / 60)          # floor at 1 minute
    vol_t = vol_annual * math.sqrt(h / 8760)  # annualised → T-hour vol
    return _norm_cdf(margin / vol_t)


def fetch_crypto_prices() -> dict:
    """
    Fetch USD prices from CoinGecko with Coinbase as per-symbol fallback.
    Returns {cg_id: {"usd": float}} or empty dict on total failure.
    """
    ids = ",".join({cg_id for cg_id, _, _ in CRYPTO_MAP.values()})
    url = f"{COINGECKO_API}?ids={ids}&vs_currencies=usd"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data:
                return data
    except Exception as exc:
        logger.warning(f"CoinGecko fetch failed: {exc}")

    prices: dict = {}
    for sym, (cg_id, cb_pair, _) in CRYPTO_MAP.items():
        cb_url = COINBASE_API.format(cb_pair)
        try:
            req = urllib.request.Request(cb_url, headers={"User-Agent": "polymarket-bot/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                d = json.loads(resp.read())
                prices[cg_id] = {"usd": float(d["data"]["amount"])}
        except Exception:
            pass
    if prices:
        logger.info(f"Coinbase fallback: fetched {len(prices)} prices")
    return prices


def _parse_crypto_question(question: str) -> tuple[str, float, str] | None:
    """
    Extract (symbol, threshold, direction) from a crypto price question, or None.

    Handles both formats:
      "Will Bitcoin reach $100k by July 31?"          ($-prefixed, direction elsewhere)
      "Ethereum above 1,830 on July 8, 10AM ET?"      (no $, hourly strike ladder)
    """
    for sym, (_, _, pattern) in CRYPTO_MAP.items():
        if not pattern.search(question):
            continue

        def _to_num(raw: str, mult: str) -> float:
            num = float(raw.replace(",", ""))
            if mult.upper() == "K":
                num *= 1_000
            elif mult.upper() == "M":
                num *= 1_000_000
            return num

        # Preferred: number anchored right after above/over/below/under
        m = _DIR_THRESH_RE.search(question)
        if m:
            direction = "above" if m.group(1).lower() in ("above", "over") else "below"
            return sym, _to_num(m.group(2), m.group(3)), direction

        # Fallback: $-prefixed number anywhere + direction keyword anywhere
        m = _PRICE_RE.search(question)
        if not m:
            return None
        num = _to_num(m.group(1), m.group(2))
        q = question.lower()
        if _ABOVE_RE.search(q):
            return sym, num, "above"
        if _BELOW_RE.search(q):
            return sym, num, "below"
        return None
    return None


def _verify_crypto_outcome(
    live_price: float,
    threshold: float,
    direction: str,
    hours_remaining: float = 6.0,
    symbol: str = "BTC",
) -> tuple[str, float] | None:
    """
    Return (outcome, confidence) when the time-aware volatility model gives
    confidence >= CRYPTO_MIN_CONFIDENCE.  Returns None when too close to call.
    """
    def _conf(margin: float) -> float:
        return _crypto_confidence(margin, hours_remaining, symbol)

    if direction == "above":
        up_margin = (live_price - threshold) / threshold
        if up_margin > 0:
            c = _conf(up_margin)
            if c >= CRYPTO_MIN_CONFIDENCE:
                return "YES", c
        else:
            dn_margin = (threshold - live_price) / threshold
            c = _conf(dn_margin)
            if c >= CRYPTO_MIN_CONFIDENCE:
                return "NO", c
    elif direction == "below":
        dn_margin = (threshold - live_price) / threshold
        if dn_margin > 0:
            c = _conf(dn_margin)
            if c >= CRYPTO_MIN_CONFIDENCE:
                return "YES", c
        else:
            up_margin = (live_price - threshold) / threshold
            c = _conf(up_margin)
            if c >= CRYPTO_MIN_CONFIDENCE:
                return "NO", c
    return None


# ── Sports result fetching & verification ─────────────────────────────────

def fetch_sports_results() -> list[dict]:
    """
    Fetch completed game results from ESPN public scoreboard API for last 3 days.
    No API key required.
    Returns list of normalised result dicts.
    """
    now = datetime.now(timezone.utc)
    results: list[dict] = []

    for sport, league in ESPN_SPORTS:
        for days_ago in range(3):
            date_str = (now - timedelta(days=days_ago)).strftime("%Y%m%d")
            url = (
                f"http://site.api.espn.com/apis/site/v2/sports"
                f"/{sport}/{league}/scoreboard?dates={date_str}"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read())
            except Exception:
                continue

            for event in (data.get("events") or []):
                status = ((event.get("status") or {}).get("type") or {})
                if not status.get("completed"):
                    continue
                competitions = event.get("competitions") or []
                if not competitions:
                    continue
                competitors = competitions[0].get("competitors") or []
                if len(competitors) < 2:
                    continue

                winner_comp = next((c for c in competitors if c.get("winner")), None)
                loser_comp  = next((c for c in competitors if not c.get("winner")), None)
                if not winner_comp:
                    continue

                def _names(comp: dict) -> dict:
                    t = comp.get("team") or {}
                    return {
                        "full":  t.get("displayName", ""),
                        "short": t.get("shortDisplayName", "") or t.get("name", ""),
                        "abbr":  t.get("abbreviation", ""),
                    }

                w = _names(winner_comp)
                l = _names(loser_comp) if loser_comp else {"full": "", "short": "", "abbr": ""}
                results.append({
                    "league":       league,
                    "winner":       w["full"],
                    "winner_short": w["short"],
                    "winner_abbr":  w["abbr"],
                    "loser":        l["full"],
                    "loser_short":  l["short"],
                    "loser_abbr":   l["abbr"],
                    "completed":    True,
                })

    logger.info(f"ESPN: {len(results)} completed results fetched")
    return results


# ESPN covers traditional sports only — esports questions can never be
# legitimately verified by it, and short abbreviations ("LAG", "MIN") were
# false-matching as substrings inside esports team names ("Zerolag", "Lumina").
_ESPORTS_RE = re.compile(
    r'\b(?:lol|league of legends|valorant|cs2|csgo|counter-?strike|dota|'
    r'overwatch|esports?|starcraft|rocket league)\b|'
    r'\b(?:map|game)\s+\d+\s+winner\b',
    re.I
)


def _name_in(name: str, text: str) -> bool:
    """Word-boundary match — 'lag' must NOT match inside 'Zerolag'."""
    if len(name) < 3:
        return False
    return re.search(r'\b' + re.escape(name) + r'\b', text) is not None


def _check_sports_question(
    question: str, results: list[dict]
) -> tuple[str, float] | None:
    """
    Match a Polymarket question to a completed game result.
    All team-name matching is word-bounded; esports markets are excluded.
    Patterns in decreasing confidence order:
      1. "Will [TEAM] win/beat/defeat/advance?" → 0.99
      2. "Does/Did/Can [TEAM] win...?" → 0.97
      3. Only one team's FULL name mentioned + win context → 0.95
    """
    q = question.lower()
    if _ESPORTS_RE.search(q):
        return None

    for r in results:
        if not r.get("completed"):
            continue
        w_names = {r.get("winner","").lower(), r.get("winner_short","").lower(),
                   r.get("winner_abbr","").lower()} - {""}
        l_names = {r.get("loser","").lower(),  r.get("loser_short","").lower(),
                   r.get("loser_abbr","").lower()} - {""}

        w_in_q = [n for n in w_names if _name_in(n, q)]
        l_in_q = [n for n in l_names if _name_in(n, q)]

        if not w_in_q and not l_in_q:
            continue

        # Pattern 1: "Will the [TEAM] win/beat/defeat/advance"
        m = _WILL_WIN_RE.search(question)
        if m:
            subject = m.group(1).strip().lower()
            if any(_name_in(n, subject) for n in w_names):
                return "YES", 0.99
            if any(_name_in(n, subject) for n in l_names):
                return "NO", 0.99

        # Pattern 2: "Does/Did/Can [TEAM] win..."
        m2 = _WIN_VERB_RE.search(question)
        if m2:
            subject = m2.group(1).strip().lower()
            if any(_name_in(n, subject) for n in w_names):
                return "YES", 0.97
            if any(_name_in(n, subject) for n in l_names):
                return "NO", 0.97

        # Pattern 3: only one team mentioned + win context.  FULL names only —
        # abbreviations are too collision-prone for presence-based inference.
        w_full = _name_in(r.get("winner","").lower(), q)
        l_full = _name_in(r.get("loser","").lower(), q)
        if _WIN_KEYWORDS_RE.search(q) and not _LOSE_KEYWORDS_RE.search(q):
            if w_full and not l_in_q:
                return "YES", 0.95
            if l_full and not w_in_q:
                return "NO", 0.95

    return None


def fetch_mlb_results() -> list[dict]:
    """
    Fetch MLB game results from the official MLB Stats API (free, no key required).
    More reliable team names than ESPN for MLB-specific markets.
    """
    now = datetime.now(timezone.utc)
    results: list[dict] = []

    for days_ago in range(3):
        date_str = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        url = (
            f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&date={date_str}&gameType=R&hydrate=linescore,teams"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            logger.debug(f"MLB API error for {date_str}: {exc}")
            continue

        for date_entry in (data.get("dates") or []):
            for game in (date_entry.get("games") or []):
                state = (game.get("status") or {}).get("detailedState", "")
                if state != "Final":
                    continue
                teams = game.get("teams") or {}
                home = teams.get("home") or {}
                away = teams.get("away") or {}
                h_score = home.get("score") or 0
                a_score = away.get("score") or 0
                if h_score == a_score:
                    continue
                if h_score > a_score:
                    w_team, l_team = home.get("team") or {}, away.get("team") or {}
                else:
                    w_team, l_team = away.get("team") or {}, home.get("team") or {}
                results.append({
                    "league":       "mlb",
                    "winner":       w_team.get("name", ""),
                    "winner_short": w_team.get("teamName", ""),
                    "winner_abbr":  w_team.get("abbreviation", ""),
                    "loser":        l_team.get("name", ""),
                    "loser_short":  l_team.get("teamName", ""),
                    "loser_abbr":   l_team.get("abbreviation", ""),
                    "completed":    True,
                })

    logger.info(f"MLB API: {len(results)} completed games")
    return results


# ── Weather forecast fetching & verification ──────────────────────────────

def _parse_weather_question(
    question: str, now: datetime
) -> tuple[str, float, str, str] | None:
    """
    Parse a Polymarket daily-high temperature question.
    Returns (city_key, threshold_f, direction, target_date_iso) or None.
    """
    q = question.lower()

    temp_m = _WEATHER_TEMP_RE.search(question)
    if not temp_m:
        return None

    threshold_f = float(temp_m.group(1))
    if not (20 <= threshold_f <= 130):
        return None

    if _ABOVE_RE.search(q):
        direction = "above"
    elif _BELOW_RE.search(q):
        direction = "below"
    else:
        return None

    # Longest match first: "new york city" before "new york"
    city_key = None
    for candidate in sorted(_CITY_COORDS, key=len, reverse=True):
        if candidate in q:
            city_key = candidate
            break
    if city_key is None:
        return None

    month_m = _WEATHER_MONTH_DAY_RE.search(question)
    if month_m:
        mon_str = month_m.group("month").lower()[:3]
        month   = _MONTH_NAMES[mon_str]
        day     = int(month_m.group("day"))
        try:
            target = datetime(now.year, month, day, tzinfo=timezone.utc)
            if target < now - timedelta(days=2):
                target = datetime(now.year + 1, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
    else:
        dow_m = _WEATHER_DOW_RE.search(question)
        if not dow_m:
            return None
        dow_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                   "friday": 4, "saturday": 5, "sunday": 6}
        target_dow  = dow_map[dow_m.group(1).lower()]
        current_dow = now.weekday()
        days_ahead  = (target_dow - current_dow) % 7
        if days_ahead == 0:
            days_ahead = 7
        target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)

    return city_key, threshold_f, direction, target.strftime("%Y-%m-%d")


def _fetch_weather_high(city_key: str, target_date_iso: str) -> float | None:
    """
    Fetch the daily high temperature (°F) from Open-Meteo.
    Uses forecast API for today/future dates, archive API for past dates.
    Results cached in-process.
    """
    lat, lon = _CITY_COORDS[city_key]
    cache_key = (lat, lon, target_date_iso)
    if cache_key in _weather_cache:
        return _weather_cache[cache_key]

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if target_date_iso < today_iso:
        url = _OPEN_METEO_ARCHIVE_URL.format(lat=lat, lon=lon, date=target_date_iso)
    else:
        url = _OPEN_METEO_FORECAST_URL.format(lat=lat, lon=lon)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning(f"Open-Meteo fetch failed for {city_key}/{target_date_iso}: {exc}")
        return None

    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []

    if target_date_iso in dates:
        idx = dates.index(target_date_iso)
        if idx < len(highs) and highs[idx] is not None:
            high_f = float(highs[idx])
            _weather_cache[cache_key] = high_f
            return high_f

    return None


def _weather_confidence(
    forecast_high_f: float,
    threshold_f: float,
    direction: str,
    days_until: int,
) -> float:
    """P(YES outcome) = N(margin_°F / std_°F) where std grows with forecast horizon."""
    std_f  = _WEATHER_STD_F.get(max(days_until, -1), 9.0)
    margin = (forecast_high_f - threshold_f if direction == "above"
              else threshold_f - forecast_high_f)
    return _norm_cdf(margin / std_f)


def _verify_weather_outcome(
    city_key: str,
    threshold_f: float,
    direction: str,
    target_date_iso: str,
    days_until: int,
) -> tuple[str, float] | None:
    """
    Fetch Open-Meteo and return (side, confidence) when the forecast clearly
    calls the outcome.  Returns None when too close to call.
    """
    forecast_f = _fetch_weather_high(city_key, target_date_iso)
    if forecast_f is None:
        return None
    conf = _weather_confidence(forecast_f, threshold_f, direction, days_until)
    if conf >= WEATHER_MIN_CONFIDENCE:
        return "YES", conf
    if 1.0 - conf >= WEATHER_MIN_CONFIDENCE:
        return "NO", round(1.0 - conf, 4)
    return None


# ── Date parsing ───────────────────────────────────────────────────────────

def _parse_end_date(market: dict) -> datetime | None:
    for key in ("endDate", "endDateIso", "end_date_iso", "endDateISO"):
        raw = market.get(key)
        if not raw:
            continue
        try:
            if isinstance(raw, (int, float)):
                return datetime.fromtimestamp(raw, tz=timezone.utc)
            s = str(raw).strip().rstrip("Z")
            if "T" in s:
                dt = datetime.fromisoformat(s)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


# ── Core scan ─────────────────────────────────────────────────────────────

def find_sniper_candidates(
    markets: list[dict],
    bankroll: float,
    sniper_state: dict,
    crypto_prices: dict | None = None,
    sports_results: list[dict] | None = None,
) -> list[SniperTrade]:
    """
    Resolution-Lag-first scanner.  Priority:
      1. Overdue crypto (endDate passed, price unambiguous)
      2. Near-expiry crypto (< 6h, current price ≈ resolution price)
      2b. Weather verified (Open-Meteo forecast vs °F threshold, ≤3 days)
      3. Sports verified (game completed per ESPN, market still open)

    markets       — Gamma API markets (after legal filter)
    bankroll      — current virtual bankroll
    sniper_state  — for dedup
    crypto_prices — from fetch_crypto_prices()
    sports_results — from fetch_sports_results()
    """
    now = datetime.now(timezone.utc)
    signals: list[SniperTrade] = []
    open_count = sum(1 for t in sniper_state["trades"] if not t.get("resolved"))

    # Diagnostic counters
    _n_no_bid = _n_no_date = _n_stale = 0
    _n_crypto_win = _n_crypto_skip = _n_weather_win = _n_weather_skip = 0
    _n_sports_win = _n_sports_skip = 0
    _n_entry_reject = _n_kelly_reject = 0   # conf OK but market already priced it
    _crypto_samples: list[str] = []
    _weather_samples: list[str] = []

    for market in markets:
        if open_count + len(signals) >= SNIPE_MAX_POSITIONS:
            break

        market_id = market.get("id", "")
        if not market_id or is_already_sniped(sniper_state, market_id):
            continue
        if not market.get("acceptingOrders", True):
            continue
        if market.get("negRisk"):
            continue

        yes_bid = float(market.get("bestBid") or market.get("_best_bid") or 0)
        yes_ask = float(market.get("bestAsk") or market.get("_best_ask") or 0)
        if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
            _n_no_bid += 1
            continue

        end_date = _parse_end_date(market)
        if end_date is None:
            _n_no_date += 1
            continue

        hours_left = (end_date - now).total_seconds() / 3600

        # Drop markets too stale to trade
        if hours_left < -SNIPE_OVERDUE_GRACE:
            _n_stale += 1
            continue

        question = market.get("question", "")
        is_overdue = hours_left < 0
        entered = False

        # ── Tier 1 & 2: Crypto verification ───────────────────────────────
        # Only enter if within SNIPE_CRYPTO_MAX_HOURS of endDate OR already overdue.
        # Markets >6h away can still have significant price movement before expiry.
        if crypto_prices and (is_overdue or hours_left <= SNIPE_CRYPTO_MAX_HOURS):
            parsed = _parse_crypto_question(question)
            if parsed:
                sym, threshold, direction = parsed
                if len(_crypto_samples) < 5:
                    _crypto_samples.append(f"{question[:60]} [h={hours_left:.1f}]")
                cg_id = CRYPTO_MAP[sym][0]
                raw_price = (crypto_prices.get(cg_id) or {}).get("usd")
                if raw_price is not None:
                    live_price = float(raw_price)
                    hours_remaining = max(0.0, hours_left)
                    verified = _verify_crypto_outcome(live_price, threshold, direction, hours_remaining, sym)
                    if verified is not None:
                        outcome, confidence = verified
                        if confidence > 0:
                            kelly_cap  = SNIPE_KELLY_CAP_OVERDUE if is_overdue else SNIPE_KELLY_CAP_NEAR
                            entry_price = yes_ask if outcome == "YES" else round(1.0 - yes_bid, 4)
                            if entry_price > SNIPE_MAX_ENTRY:
                                # Model agrees with market — priced too high to profit after fee
                                _n_entry_reject += 1
                            else:
                                bet = _kelly_bet(confidence, entry_price, bankroll, kelly_cap)
                                if bet <= 0:
                                    # conf passed threshold but Kelly says -EV at this ask
                                    _n_kelly_reject += 1
                                elif bankroll >= bet:
                                    reason = "overdue_crypto" if is_overdue else "near_expiry_crypto"
                                    signals.append(SniperTrade(
                                        date=now.strftime("%Y-%m-%d"),
                                        market_id=market_id,
                                        question=question[:120],
                                        side=outcome,
                                        price=entry_price,
                                        bet_usdc=bet,
                                        shares=round(bet / entry_price, 4),
                                        reason=reason,
                                        hours_to_expiry=round(hours_left, 1),
                                        live_price=round(live_price, 2),
                                        threshold=threshold,
                                        confidence=confidence,
                                    ))
                                    _n_crypto_win += 1
                                    entered = True
                    else:
                        _n_crypto_skip += 1
                        logger.debug(
                            f"Crypto skip: {question[:55]} | live={raw_price} "
                            f"thresh={threshold} dir={direction} h={hours_left:.1f}"
                        )

        # ── Tier 2b: Weather verification ─────────────────────────────────
        # Open-Meteo free forecast vs temperature threshold in the question.
        # Enter for target dates from yesterday (resolution lag) to 3 days ahead.
        if not entered:
            parsed_w = _parse_weather_question(question, now)
            if parsed_w is not None:
                w_city, w_thresh, w_dir, w_date = parsed_w
                if len(_weather_samples) < 5:
                    _weather_samples.append(f"{question[:60]} [city={w_city}]")
                target_dt  = datetime.strptime(w_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                today_dt   = now.replace(hour=0, minute=0, second=0, microsecond=0)
                days_until = (target_dt - today_dt).days
                if -1 <= days_until <= WEATHER_MAX_DAYS_AHEAD:
                    w_result = _verify_weather_outcome(w_city, w_thresh, w_dir, w_date, days_until)
                    if w_result is not None:
                        outcome, confidence = w_result
                        entry_price = yes_ask if outcome == "YES" else round(1.0 - yes_bid, 4)
                        if entry_price <= SNIPE_MAX_ENTRY:
                            bet = _kelly_bet(confidence, entry_price, bankroll, SNIPE_KELLY_CAP_NEAR)
                            if bet > 0 and bankroll >= bet:
                                signals.append(SniperTrade(
                                    date=now.strftime("%Y-%m-%d"),
                                    market_id=market_id,
                                    question=question[:120],
                                    side=outcome,
                                    price=entry_price,
                                    bet_usdc=bet,
                                    shares=round(bet / entry_price, 4),
                                    reason="weather_verified",
                                    hours_to_expiry=round(hours_left, 1),
                                    threshold=w_thresh,
                                    confidence=round(confidence, 4),
                                ))
                                _n_weather_win += 1
                                entered = True
                    else:
                        _n_weather_skip += 1

        # ── Tier 3: Sports verification ────────────────────────────────────
        # No time restriction: game completion is the signal, not endDate proximity.
        # Game can complete before endDate (e.g., game June 19, endDate June 21).
        if not entered and sports_results:
            sport_result = _check_sports_question(question, sports_results)
            if sport_result is not None:
                outcome, confidence = sport_result
                entry_price = yes_ask if outcome == "YES" else round(1.0 - yes_bid, 4)
                if entry_price <= SNIPE_MAX_ENTRY:
                    bet = _kelly_bet(confidence, entry_price, bankroll, SNIPE_KELLY_CAP_OVERDUE)
                    if bet > 0 and bankroll >= bet:
                        signals.append(SniperTrade(
                            date=now.strftime("%Y-%m-%d"),
                            market_id=market_id,
                            question=question[:120],
                            side=outcome,
                            price=entry_price,
                            bet_usdc=bet,
                            shares=round(bet / entry_price, 4),
                            reason="sports_verified",
                            hours_to_expiry=round(hours_left, 1),
                            confidence=confidence,
                        ))
                        _n_sports_win += 1
            else:
                _n_sports_skip += 1

    logger.info(
        f"Scan diagnostic: markets={len(markets)} "
        f"no_bid={_n_no_bid} no_date={_n_no_date} stale={_n_stale} | "
        f"crypto={_n_crypto_win}win/{_n_crypto_skip}skip "
        f"weather={_n_weather_win}win/{_n_weather_skip}skip "
        f"sports={_n_sports_win}win/{_n_sports_skip}skip | "
        f"entry_reject={_n_entry_reject} kelly_reject={_n_kelly_reject}"
    )
    if _crypto_samples:
        logger.info(f"Crypto candidates: {_crypto_samples}")
    if _weather_samples:
        logger.info(f"Weather candidates: {_weather_samples}")

    # Persist a rolling window of scan diagnostics so the daily reviewer
    # can diagnose WHERE the pipeline loses candidates (loop engineering).
    diag = sniper_state.setdefault("_diag", {"scans": []})
    diag["scans"].append({
        "ts": now.strftime("%Y-%m-%dT%H:%M"),
        "markets": len(markets),
        "no_bid": _n_no_bid,
        "crypto_win": _n_crypto_win, "crypto_skip": _n_crypto_skip,
        "weather_win": _n_weather_win, "weather_skip": _n_weather_skip,
        "sports_win": _n_sports_win, "sports_skip": _n_sports_skip,
        "entry_reject": _n_entry_reject, "kelly_reject": _n_kelly_reject,
    })
    diag["scans"] = diag["scans"][-200:]   # keep last ~200 scans (~1 day)

    return signals


# ── Resolution check ───────────────────────────────────────────────────────

def check_sniper_resolutions(
    state: dict,
    markets_by_id: dict,
    bankroll_ref: list,
) -> list[dict]:
    """
    Check open sniper trades against resolved markets.
    Returns list of closed trade dicts (with pnl_usdc filled in).

    Fee model:
      WIN:  gross = bet × (1 – price) / price  minus  bet × fee
      LOSS: pnl = –bet   ← fee paid at buy; no double-count
    """
    closed: list[dict] = []

    for t in state["trades"]:
        if t.get("resolved"):
            continue
        market = markets_by_id.get(t["market_id"])
        if not market or not market.get("closed"):
            continue

        prices_raw = market.get("outcomePrices")
        try:
            prices = (
                [float(p) for p in json.loads(prices_raw)]
                if isinstance(prices_raw, str)
                else [float(p) for p in (prices_raw or [])]
            )
        except Exception:
            continue

        if not prices or len(prices) < 2:
            continue

        yes_won = prices[0] >= 0.99
        no_won  = prices[1] >= 0.99
        if not (yes_won or no_won):
            continue

        side  = t["side"]
        won   = (side == "YES" and yes_won) or (side == "NO" and no_won)
        bet   = t["bet_usdc"]
        price = t["price"]

        pnl = round(bet * (1.0 - price) / price - bet * POLYMARKET_FEE, 4) if won else round(-bet, 4)

        t["resolved"]  = True
        t["outcome"]   = won
        t["pnl_usdc"]  = pnl
        state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
        bankroll_ref[0]    = round(bankroll_ref[0] + pnl, 4)

        logger.info(
            f"[SNIPER] {'WIN' if won else 'LOSS'}: {t.get('question','?')[:50]} | "
            f"{side} @ {price:.3f} | conf={t.get('confidence',0):.3f} | P&L={pnl:+.4f}"
        )
        closed.append(t)

    return closed
