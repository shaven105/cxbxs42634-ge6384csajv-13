"""
S3 Near-Expiry Certainty Sniper for Polymarket prediction markets.

Inspired by the "Late Analysis Sniper" used by top Polymarket traders.

Strategy
--------
Two entry conditions:

1. OVERDUE — market's endDate has passed but it's still active / accepting orders.
   The outcome is typically known by now; sellers exit at a discount rather than
   wait for the admin to finalize resolution.

2. NEAR-EXPIRY — market expires within SNIPE_HOURS_AHEAD hours AND the crowd
   price already signals near-certainty (bid ≥ SNIPE_MIN_PRICE for YES/NO).

For crypto price markets (BTC/ETH/SOL/etc.), we verify the outcome externally
via CoinGecko before entering, providing an objective edge on top of the price
signal.

For non-crypto markets, we rely on SNIPE_MIN_PRICE as a proxy for crowd
conviction.  High consensus + imminent resolution = very low uncertainty.

State lives in sniper_trades.json alongside paper_trades.json / grid_trades.json.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Strategy parameters ────────────────────────────────────────────────────
SNIPE_HOURS_AHEAD = 48     # scan markets expiring within this window
SNIPE_OVERDUE_GRACE = 72   # ignore markets overdue by more than 3 days (likely stale)
SNIPE_MIN_PRICE = 0.88     # crowd price must be ≥ this to signal near-certainty
SNIPE_MAX_ENTRY = 0.97     # don't pay more than this (≥3% upside needed)
SNIPE_BET_FRACTION = 0.04  # 4% of bankroll per snipe
SNIPE_MIN_BET = 0.10
SNIPE_MAX_POSITIONS = 6    # cap concurrent sniper trades

STATE_FILE = Path("sniper_trades.json")
COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"

# Crypto symbol → (CoinGecko id, regex)
CRYPTO_MAP: dict[str, tuple[str, re.Pattern]] = {
    "BTC":  ("bitcoin",      re.compile(r'\b(?:btc|bitcoin)\b', re.I)),
    "ETH":  ("ethereum",     re.compile(r'\b(?:eth|ethereum)\b', re.I)),
    "SOL":  ("solana",       re.compile(r'\b(?:sol|solana)\b', re.I)),
    "DOGE": ("dogecoin",     re.compile(r'\b(?:doge|dogecoin)\b', re.I)),
    "XRP":  ("ripple",       re.compile(r'\b(?:xrp|ripple)\b', re.I)),
    "BNB":  ("binancecoin",  re.compile(r'\b(?:bnb)\b', re.I)),
    "MATIC":("matic-network",re.compile(r'\b(?:matic|polygon)\b', re.I)),
    "ADA":  ("cardano",      re.compile(r'\b(?:ada|cardano)\b', re.I)),
    "AVAX": ("avalanche-2",  re.compile(r'\b(?:avax|avalanche)\b', re.I)),
}

_PRICE_RE  = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)')
_ABOVE_RE  = re.compile(r'\b(?:above|over|exceed|reaches?|hits?|surpasses?|breaks?|crosses?|at or above)\b', re.I)
_BELOW_RE  = re.compile(r'\b(?:below|under|falls?\s+(?:below|under)|drops?\s+(?:below|under)|at or below)\b', re.I)

# Polymarket 2% fee (applied on bet amount for simplicity, matching paper_trader.py)
POLYMARKET_FEE = 0.02


# ── Dataclass ──────────────────────────────────────────────────────────────

@dataclass
class SniperTrade:
    date: str
    market_id: str
    question: str
    side: str               # "YES" or "NO"
    price: float            # entry price per share
    bet_usdc: float
    shares: float
    reason: str             # "overdue_crypto" | "overdue_crowd" | "near_expiry_crypto" | "near_expiry_crowd"
    hours_to_expiry: float  # negative = already overdue
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
        f"| {trade.reason} | h_left={trade.hours_to_expiry:.1f} | ${trade.bet_usdc:.2f}"
    )


# ── Crypto price verification ──────────────────────────────────────────────

def fetch_crypto_prices() -> dict:
    """Fetch USD prices from CoinGecko. Returns {} on failure."""
    ids = ",".join({cg_id for cg_id, _ in CRYPTO_MAP.values()})
    url = f"{COINGECKO_API}?ids={ids}&vs_currencies=usd"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning(f"CoinGecko fetch failed: {exc}")
        return {}


def _parse_crypto_question(question: str) -> tuple[str, float, str] | None:
    """
    Extract (symbol, threshold, direction) from a question like
    "Will BTC be above $100k by June?" → ("BTC", 100000.0, "above").
    Returns None if not recognizable.
    """
    for sym, (_, pattern) in CRYPTO_MAP.items():
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
            return None  # direction unclear
    return None


def _verify_crypto_outcome(live_price: float, threshold: float, direction: str) -> str | None:
    """
    Returns "YES" or "NO" only when outcome is unambiguous (≥3% margin).
    Returns None when price is too close to call.
    """
    margin = 0.03
    if direction == "above":
        if live_price > threshold * (1 + margin):
            return "YES"
        if live_price < threshold * (1 - margin):
            return "NO"
    elif direction == "below":
        if live_price < threshold * (1 - margin):
            return "YES"
        if live_price > threshold * (1 + margin):
            return "NO"
    return None


# ── Date parsing ───────────────────────────────────────────────────────────

def _parse_end_date(market: dict) -> datetime | None:
    """Extract end date as UTC-aware datetime from various Gamma API field formats."""
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
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
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
) -> list[SniperTrade]:
    """
    Scan markets for high-certainty near-expiry or overdue sniper opportunities.

    markets       — list of market dicts (after legal filter; pre-filter ok)
    bankroll      — current virtual bankroll
    sniper_state  — current sniper state (to avoid duplicate positions)
    crypto_prices — CoinGecko prices dict (may be empty if fetch failed)

    Returns a list of SniperTrade signals (not yet recorded).
    """
    now = datetime.now(timezone.utc)
    signals: list[SniperTrade] = []
    open_count = sum(1 for t in sniper_state["trades"] if not t.get("resolved"))

    for market in markets:
        if open_count + len(signals) >= SNIPE_MAX_POSITIONS:
            break

        market_id = market.get("id", "")
        if not market_id:
            continue
        if is_already_sniped(sniper_state, market_id):
            continue

        # Basic price sanity
        bid = float(market.get("bestBid") or market.get("_best_bid") or 0)
        ask = float(market.get("bestAsk") or market.get("_best_ask") or 0)
        if bid <= 0 or ask <= 0 or bid >= ask:
            continue
        if not market.get("acceptingOrders", True):
            continue

        # Parse end date and classify
        end_date = _parse_end_date(market)
        if end_date is None:
            continue

        hours_left = (end_date - now).total_seconds() / 3600
        if hours_left < -SNIPE_OVERDUE_GRACE:
            continue  # too stale
        if hours_left > SNIPE_HOURS_AHEAD:
            continue  # too far out

        is_overdue = hours_left < 0
        base_reason = "overdue" if is_overdue else "near_expiry"

        question = market.get("question", "")
        side: str | None = None
        entry_price: float | None = None
        reason: str | None = None

        # ── Try crypto verification first ────────────────────────────────
        parsed = _parse_crypto_question(question)
        if parsed and crypto_prices:
            sym, threshold, direction = parsed
            cg_id = CRYPTO_MAP.get(sym, (None, None))[0]
            live_price = (crypto_prices.get(cg_id) or {}).get("usd")
            if live_price:
                outcome = _verify_crypto_outcome(float(live_price), threshold, direction)
                if outcome == "YES":
                    side = "YES"
                    entry_price = ask
                    reason = f"{base_reason}_crypto"
                elif outcome == "NO":
                    # Buy NO = buy at (1 - YES_bid) price
                    no_ask = round(1.0 - bid, 4)
                    if no_ask <= SNIPE_MAX_ENTRY:
                        side = "NO"
                        entry_price = no_ask
                        reason = f"{base_reason}_crypto"

        # ── Crowd-conviction fallback ────────────────────────────────────
        if side is None:
            if bid >= SNIPE_MIN_PRICE and ask <= SNIPE_MAX_ENTRY:
                side = "YES"
                entry_price = ask
                reason = f"{base_reason}_crowd"
            elif (1 - ask) >= SNIPE_MIN_PRICE:
                no_ask = round(1.0 - bid, 4)
                if no_ask <= SNIPE_MAX_ENTRY:
                    side = "NO"
                    entry_price = no_ask
                    reason = f"{base_reason}_crowd"

        if side is None or entry_price is None or reason is None:
            continue

        # Minimum upside check after fee (need gross profit > 2% fee on bet)
        # gross = bet*(1-price)/price; fee = bet*0.02; need (1-price)/price > 0.02 → price < 0.98
        if entry_price >= 0.98:
            continue

        bet = round(max(SNIPE_MIN_BET, bankroll * SNIPE_BET_FRACTION), 2)
        signals.append(SniperTrade(
            date=now.strftime("%Y-%m-%d"),
            market_id=market_id,
            question=question[:120],
            side=side,
            price=entry_price,
            bet_usdc=bet,
            shares=round(bet / entry_price, 4),
            reason=reason,
            hours_to_expiry=round(hours_left, 1),
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
    """
    closed: list[dict] = []

    for t in state["trades"]:
        if t.get("resolved"):
            continue

        market = markets_by_id.get(t["market_id"])
        if market is None:
            continue
        if not market.get("closed"):
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
        won = (side == "YES" and yes_won) or (side == "NO" and no_won)
        bet = t["bet_usdc"]
        price = t["price"]

        if won:
            gross = bet * (1.0 - price) / price
            pnl = round(gross - bet * POLYMARKET_FEE, 4)
        else:
            pnl = round(-bet - bet * POLYMARKET_FEE, 4)

        t["resolved"] = True
        t["outcome"] = won
        t["pnl_usdc"] = pnl
        state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
        bankroll_ref[0] = round(bankroll_ref[0] + pnl, 4)

        result = "WIN" if won else "LOSS"
        logger.info(
            f"[SNIPER] {result}: {t.get('question', '?')[:50]} | "
            f"{side} @ {price:.3f} | P&L={pnl:+.4f}"
        )
        closed.append(t)

    return closed
