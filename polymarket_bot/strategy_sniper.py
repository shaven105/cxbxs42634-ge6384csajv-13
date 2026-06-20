"""
S3 Near-Expiry Certainty Sniper v3 — Polymarket paper trading.

V3 changes (MoM 20% target):
  - Kelly-based bet sizing (half-Kelly capped at 35% of bankroll)
  - Confidence tiers: crypto margin 5%→0.97, 10%→0.99, 20%→0.995
  - ESPN sports result verification (NBA, NFL, MLB, NHL, soccer)
  - SNIPE_MIN_BET raised to $0.50, SNIPE_MAX_POSITIONS reduced to 3
  - SNIPE_HOURS_AHEAD extended to 72h, SNIPE_OVERDUE_GRACE to 168h (7 days)

EV model (crypto, 97% WR, ask=0.88, $50 bankroll → 35% Kelly = $17.50):
  win  = 17.50 × (1−0.88)/0.88 − 17.50×0.018 = 2.386 − 0.315 = +$2.07
  loss = −17.50
  EV   = 0.97×2.07 + 0.03×(−17.50) = 2.009 − 0.525 = +$1.48/trade

5 crypto + 2 sports trades/month → ~$10–12 = 20%+ MoM on $50 ✓

Fee model (unchanged):
  WIN:  gross = bet × (1 – price) / price  minus  bet × fee
  LOSS: pnl = –bet  ← fee paid at buy; do NOT double-count
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Strategy parameters ────────────────────────────────────────────────────
SNIPE_HOURS_AHEAD    = 72     # scan markets expiring within 3 days
SNIPE_OVERDUE_GRACE  = 168    # ignore markets overdue by more than 7 days
SNIPE_MAX_ENTRY      = 0.95   # never pay more than 95¢ (min 5% upside after fee)
SNIPE_HALF_KELLY_CAP = 0.35   # half-Kelly capped at 35% of bankroll
SNIPE_MIN_BET        = 0.50   # minimum bet in USDC (raised from $0.10)
SNIPE_MAX_POSITIONS  = 3      # maximum concurrent sniper positions (reduced from 6)
CRYPTO_MARGIN        = 0.05   # price must be 5% beyond threshold to confirm

# Polymarket taker fee as of March 2026
POLYMARKET_FEE = 0.018

STATE_FILE = Path("sniper_trades.json")

COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"
COINBASE_API  = "https://api.coinbase.com/v2/prices/{}/spot"

# Sport/league pairs for ESPN scoreboard API
ESPN_SPORTS = [
    ("basketball", "nba"),
    ("football",   "nfl"),
    ("baseball",   "mlb"),
    ("hockey",     "nhl"),
    ("soccer",     "eng.1"),
    ("soccer",     "esp.1"),
    ("soccer",     "uefa.champions"),
]

# Crypto symbol → (CoinGecko id, Coinbase pair, question regex)
CRYPTO_MAP: dict[str, tuple[str, str, re.Pattern]] = {
    "BTC":  ("bitcoin",       "BTC-USD",   re.compile(r'\b(?:btc|bitcoin)\b',        re.I)),
    "ETH":  ("ethereum",      "ETH-USD",   re.compile(r'\b(?:eth|ethereum)\b',       re.I)),
    "SOL":  ("solana",        "SOL-USD",   re.compile(r'\b(?:sol|solana)\b',         re.I)),
    "DOGE": ("dogecoin",      "DOGE-USD",  re.compile(r'\b(?:doge|dogecoin)\b',      re.I)),
    "XRP":  ("ripple",        "XRP-USD",   re.compile(r'\b(?:xrp|ripple)\b',         re.I)),
    "BNB":  ("binancecoin",   "BNB-USD",   re.compile(r'\b(?:bnb)\b',                re.I)),
    "MATIC":("matic-network", "MATIC-USD", re.compile(r'\b(?:matic|polygon)\b',      re.I)),
    "ADA":  ("cardano",       "ADA-USD",   re.compile(r'\b(?:ada|cardano)\b',        re.I)),
    "AVAX": ("avalanche-2",   "AVAX-USD",  re.compile(r'\b(?:avax|avalanche)\b',     re.I)),
    "LINK": ("chainlink",     "LINK-USD",  re.compile(r'\b(?:link|chainlink)\b',     re.I)),
    "DOT":  ("polkadot",      "DOT-USD",   re.compile(r'\b(?:dot|polkadot)\b',       re.I)),
    "LTC":  ("litecoin",      "LTC-USD",   re.compile(r'\b(?:ltc|litecoin)\b',       re.I)),
}

_PRICE_RE = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)')
_ABOVE_RE = re.compile(
    r'\b(?:above|over|exceed|reaches?|hits?|surpasses?|breaks?|crosses?|at or above)\b', re.I
)
_BELOW_RE = re.compile(
    r'\b(?:below|under|falls?\s+(?:below|under)|drops?\s+(?:below|under)|at or below)\b', re.I
)
_WILL_WIN_RE = re.compile(
    r'\bwill\s+(?:the\s+)?(.{2,40}?)\s+(?:win|beat|defeat|advance)', re.I
)


# ── Dataclass ──────────────────────────────────────────────────────────────

@dataclass
class SniperTrade:
    date: str
    market_id: str
    question: str
    side: str               # "YES" or "NO"
    price: float            # entry ask price per share
    bet_usdc: float
    shares: float
    reason: str             # "overdue_crypto" | "near_expiry_crypto" | "sports_verified"
    hours_to_expiry: float  # negative = already overdue
    live_price: Optional[float] = None
    threshold: Optional[float] = None
    confidence: float = 0.0          # Kelly confidence (0.97 / 0.99 / 0.995)
    # Resolution tracking
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
        f"| {trade.reason} | conf={trade.confidence:.3f} | h_left={trade.hours_to_expiry:.1f} "
        f"| ${trade.bet_usdc:.2f}"
    )


# ── Kelly bet sizing ───────────────────────────────────────────────────────

def _kelly_bet(confidence: float, ask: float, bankroll: float) -> float:
    """Half-Kelly bet size, capped at SNIPE_HALF_KELLY_CAP of bankroll."""
    if ask <= 0 or ask >= 1 or confidence <= 0:
        return 0.0
    b = (1.0 - ask) / ask        # net odds per unit wagered
    p = confidence
    q = 1.0 - p
    f_full = (b * p - q) / b     # full Kelly fraction
    if f_full <= 0:
        return 0.0
    fraction = min(f_full * 0.5, SNIPE_HALF_KELLY_CAP)
    return round(max(bankroll * fraction, SNIPE_MIN_BET), 2)


# ── Crypto price verification ──────────────────────────────────────────────

def _crypto_confidence(margin: float) -> float:
    """Map fractional price margin beyond threshold to confidence probability."""
    if margin >= 0.20:
        return 0.995
    if margin >= 0.10:
        return 0.990
    if margin >= 0.05:
        return 0.970
    return 0.0


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

    # Per-symbol Coinbase fallback
    prices: dict = {}
    for sym, (cg_id, cb_pair, _) in CRYPTO_MAP.items():
        cb_url = COINBASE_API.format(cb_pair)
        try:
            req = urllib.request.Request(cb_url, headers={"User-Agent": "polymarket-bot/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                d = json.loads(resp.read())
                amount = float(d["data"]["amount"])
                prices[cg_id] = {"usd": amount}
        except Exception:
            pass

    if prices:
        logger.info(f"Coinbase fallback: fetched {len(prices)} prices")
    return prices


def _parse_crypto_question(question: str) -> tuple[str, float, str] | None:
    """
    Extract (symbol, threshold, direction) from a crypto price question.
    Returns None if question is not recognizable as a crypto price market.
    """
    for sym, (_, _, pattern) in CRYPTO_MAP.items():
        if pattern.search(question):
            m = _PRICE_RE.search(question)
            if not m:
                return None
            num = float(m.group(1).replace(",", ""))
            mult = m.group(2).upper()
            if mult == "K":
                num *= 1_000
            elif mult == "M":
                num *= 1_000_000

            q = question.lower()
            if _ABOVE_RE.search(q):
                return sym, num, "above"
            if _BELOW_RE.search(q):
                return sym, num, "below"
            return None  # direction ambiguous
    return None


def _verify_crypto_outcome(
    live_price: float, threshold: float, direction: str
) -> tuple[str, float] | None:
    """
    Returns (outcome, confidence) when price is unambiguously past threshold.
    Returns None when price is too close to the threshold to be certain.
    """
    if direction == "above":
        if live_price > threshold * (1 + CRYPTO_MARGIN):
            margin = (live_price - threshold) / threshold
            return "YES", _crypto_confidence(margin)
        if live_price < threshold * (1 - CRYPTO_MARGIN):
            margin = (threshold - live_price) / threshold
            return "NO", _crypto_confidence(margin)
    elif direction == "below":
        if live_price < threshold * (1 - CRYPTO_MARGIN):
            margin = (threshold - live_price) / threshold
            return "YES", _crypto_confidence(margin)
        if live_price > threshold * (1 + CRYPTO_MARGIN):
            margin = (live_price - threshold) / threshold
            return "NO", _crypto_confidence(margin)
    return None


# ── Sports result verification ─────────────────────────────────────────────

def fetch_sports_results() -> list[dict]:
    """
    Fetch completed game results from ESPN public API for last 3 days.
    Returns list of {league, winner, winner_short, winner_abbr,
                      loser, loser_short, loser_abbr, completed, date}.
    No API key required.
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
                    team = comp.get("team") or {}
                    return {
                        "full":  team.get("displayName", ""),
                        "short": team.get("shortDisplayName", "") or team.get("name", ""),
                        "abbr":  team.get("abbreviation", ""),
                    }

                w = _names(winner_comp)
                l = _names(loser_comp) if loser_comp else {"full": "", "short": "", "abbr": ""}

                results.append({
                    "league":        league,
                    "winner":        w["full"],
                    "winner_short":  w["short"],
                    "winner_abbr":   w["abbr"],
                    "loser":         l["full"],
                    "loser_short":   l["short"],
                    "loser_abbr":    l["abbr"],
                    "completed":     True,
                    "date":          date_str,
                })

    logger.info(f"ESPN: {len(results)} completed results fetched")
    return results


def _check_sports_question(
    question: str, results: list[dict]
) -> tuple[str, float] | None:
    """
    Match a Polymarket question to a completed ESPN game result.

    Requires "Will [TEAM] win/beat/defeat..." pattern — conservative to avoid
    false positives. Returns (side, 0.99) or None.
    """
    q = question.lower()

    for r in results:
        if not r.get("completed"):
            continue

        w_names = {
            r.get("winner", "").lower(),
            r.get("winner_short", "").lower(),
            r.get("winner_abbr", "").lower(),
        } - {""}
        l_names = {
            r.get("loser", "").lower(),
            r.get("loser_short", "").lower(),
            r.get("loser_abbr", "").lower(),
        } - {""}

        # Both teams must appear in question
        if not (any(n in q for n in w_names) and any(n in q for n in l_names)):
            continue

        # "Will [X] win/beat/defeat/advance" — X is the YES subject
        m = _WILL_WIN_RE.search(question)
        if not m:
            continue

        subject = m.group(1).strip().lower()
        if any(n in subject for n in w_names):
            return "YES", 0.99
        if any(n in subject for n in l_names):
            return "NO", 0.99

    return None


# ── Date parsing ───────────────────────────────────────────────────────────

def _parse_end_date(market: dict) -> datetime | None:
    """Extract end date as UTC-aware datetime."""
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
    Scan markets for EV-positive sniper entries using external verification.

    Accepted signal types (both use Kelly sizing):
      - crypto_verified: crypto price beyond threshold with ≥5% margin
      - sports_verified: completed ESPN game matches Polymarket question

    markets        — from Gamma API (after legal filter)
    bankroll       — current virtual bankroll
    sniper_state   — for dedup (avoid re-entering open positions)
    crypto_prices  — CoinGecko/Coinbase prices dict
    sports_results — ESPN completed game results from fetch_sports_results()
    """
    now = datetime.now(timezone.utc)
    signals: list[SniperTrade] = []
    open_count = sum(1 for t in sniper_state["trades"] if not t.get("resolved"))

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
            continue

        end_date = _parse_end_date(market)
        if end_date is None:
            continue

        hours_left = (end_date - now).total_seconds() / 3600

        # Time window: not too stale, not too far out
        if hours_left < -SNIPE_OVERDUE_GRACE:
            continue
        if hours_left > SNIPE_HOURS_AHEAD:
            continue

        question = market.get("question", "")
        is_overdue = hours_left < 0

        # ── Try crypto verification ────────────────────────────────────────
        parsed = _parse_crypto_question(question)
        if parsed and crypto_prices:
            sym, threshold, direction = parsed
            cg_id = CRYPTO_MAP[sym][0]
            live_price_raw = (crypto_prices.get(cg_id) or {}).get("usd")
            if live_price_raw is not None:
                live_price = float(live_price_raw)
                result = _verify_crypto_outcome(live_price, threshold, direction)
                if result is not None:
                    outcome, confidence = result
                    entry_price = yes_ask if outcome == "YES" else round(1.0 - yes_bid, 4)
                    if entry_price <= SNIPE_MAX_ENTRY:
                        bet = _kelly_bet(confidence, entry_price, bankroll)
                        if bet > 0 and bankroll >= bet:
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
                            continue  # don't double-check sports for same market

        # ── Try sports verification ────────────────────────────────────────
        if sports_results:
            sport_result = _check_sports_question(question, sports_results)
            if sport_result is not None:
                outcome, confidence = sport_result
                entry_price = yes_ask if outcome == "YES" else round(1.0 - yes_bid, 4)
                if entry_price <= SNIPE_MAX_ENTRY:
                    bet = _kelly_bet(confidence, entry_price, bankroll)
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
      LOSS: pnl = –bet   ← fee was already paid at buy time; no double-count
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
            continue  # not fully resolved yet

        side = t["side"]
        won  = (side == "YES" and yes_won) or (side == "NO" and no_won)
        bet  = t["bet_usdc"]
        price = t["price"]

        if won:
            gross = bet * (1.0 - price) / price
            pnl = round(gross - bet * POLYMARKET_FEE, 4)
        else:
            pnl = round(-bet, 4)

        t["resolved"]  = True
        t["outcome"]   = won
        t["pnl_usdc"]  = pnl
        state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
        bankroll_ref[0]    = round(bankroll_ref[0] + pnl, 4)

        result = "WIN" if won else "LOSS"
        logger.info(
            f"[SNIPER] {result}: {t.get('question', '?')[:50]} | "
            f"{side} @ {price:.3f} | conf={t.get('confidence', 0):.3f} | P&L={pnl:+.4f}"
        )
        closed.append(t)

    return closed
