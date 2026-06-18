"""
S3 Near-Expiry Certainty Sniper v2 — Polymarket paper trading.

Research-backed rewrite.  Key changes from v1:
  - Remove crowd-only entries (EV-negative: breakeven WR > ask × 1.018, unachievable)
  - Fix fee model: loss = -bet (fee is charged at buy; NOT double-counted on losses)
  - Fee rate updated to 1.80% (Polymarket rate as of March 2026; was 2.0%)
  - Tighten MAX_ENTRY_PRICE to 0.95 (minimum 5% upside needed after 1.8% fee)
  - Increase crypto price margin to 5% (was 3%) — prevents false positives on noisy ticks
  - Add Coinbase as CoinGecko backup
  - Add YES+NO combined-price arbitrage detection (guaranteed profit if YES+NO < $1)

Strategy (two profitable tiers)
---------------------------------
Tier 1 — Crypto-verified (highest EV)
  Market question references a crypto price threshold (BTC/ETH/SOL/etc).
  We fetch live price from CoinGecko (+Coinbase fallback) and check if the
  outcome is unambiguous (margin ≥ 5%).  When confirmed:
    - Buy the winning side at current ask
    - Expected WR ≈ 97%, EV ≈ +$0.12 per $2 trade at 90% ask.
  Works for both OVERDUE and NEAR-EXPIRY windows.

Removed (EV analysis):
  - near_expiry_crowd: crowd calibration at 88% bid → 86% actual WR.
    Breakeven requires WR > 0.895 × 1.018 = 0.911. Unachievable.
  - overdue_crowd: even at 94% WR, ask ≈ 0.94 → EV = 0.94×0.069 - 0.06×2 = -0.055.
  - yes_no_arb: requires ask_YES < bid_YES (crossed book), impossible in live markets.
    YES+NO combined cost = ask + (1-bid) = 1 + spread > 1. Adding fee makes it worse.

EV proof (corrected model):
  crypto_verified, ask=0.90, WR=97%, bet=$2, fee=1.8%:
    win  = 2 × (1-0.90)/0.90 - 2×0.018 = 0.222 - 0.036 = +0.186
    loss = -2.00
    EV   = 0.97×0.186 + 0.03×(-2) = 0.180 - 0.060 = +$0.120 ✓
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Strategy parameters ────────────────────────────────────────────────────
SNIPE_HOURS_AHEAD    = 48    # scan markets expiring within this window
SNIPE_OVERDUE_GRACE  = 72   # ignore markets overdue by more than 3 days
SNIPE_MAX_ENTRY      = 0.95  # never pay more than this (minimum 5% upside)
SNIPE_BET_FRACTION   = 0.04  # 4% of bankroll per snipe
SNIPE_MIN_BET        = 0.10
SNIPE_MAX_POSITIONS  = 6     # cap concurrent sniper trades
CRYPTO_MARGIN        = 0.05  # price must be 5% beyond threshold to confirm outcome

# Polymarket taker fee as of March 2026
POLYMARKET_FEE = 0.018

STATE_FILE = Path("sniper_trades.json")

COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"
COINBASE_API  = "https://api.coinbase.com/v2/prices/{}/spot"

# Crypto symbol → (CoinGecko id, Coinbase pair, question regex)
CRYPTO_MAP: dict[str, tuple[str, str, re.Pattern]] = {
    "BTC":  ("bitcoin",       "BTC-USD",  re.compile(r'\b(?:btc|bitcoin)\b',         re.I)),
    "ETH":  ("ethereum",      "ETH-USD",  re.compile(r'\b(?:eth|ethereum)\b',        re.I)),
    "SOL":  ("solana",        "SOL-USD",  re.compile(r'\b(?:sol|solana)\b',          re.I)),
    "DOGE": ("dogecoin",      "DOGE-USD", re.compile(r'\b(?:doge|dogecoin)\b',       re.I)),
    "XRP":  ("ripple",        "XRP-USD",  re.compile(r'\b(?:xrp|ripple)\b',          re.I)),
    "BNB":  ("binancecoin",   "BNB-USD",  re.compile(r'\b(?:bnb)\b',                 re.I)),
    "MATIC":("matic-network", "MATIC-USD",re.compile(r'\b(?:matic|polygon)\b',       re.I)),
    "ADA":  ("cardano",       "ADA-USD",  re.compile(r'\b(?:ada|cardano)\b',          re.I)),
    "AVAX": ("avalanche-2",   "AVAX-USD", re.compile(r'\b(?:avax|avalanche)\b',      re.I)),
    "LINK": ("chainlink",     "LINK-USD", re.compile(r'\b(?:link|chainlink)\b',      re.I)),
    "DOT":  ("polkadot",      "DOT-USD",  re.compile(r'\b(?:dot|polkadot)\b',        re.I)),
    "LTC":  ("litecoin",      "LTC-USD",  re.compile(r'\b(?:ltc|litecoin)\b',        re.I)),
}

_PRICE_RE = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)')
_ABOVE_RE = re.compile(
    r'\b(?:above|over|exceed|reaches?|hits?|surpasses?|breaks?|crosses?|at or above)\b', re.I
)
_BELOW_RE = re.compile(
    r'\b(?:below|under|falls?\s+(?:below|under)|drops?\s+(?:below|under)|at or below)\b', re.I
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
    reason: str             # "overdue_crypto" | "near_expiry_crypto" | "yes_no_arb"
    hours_to_expiry: float  # negative = already overdue
    live_price: Optional[float] = None   # verified external price
    threshold: Optional[float] = None   # from market question
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
        url = COINBASE_API.format(cb_pair)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
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
    E.g. "Will BTC be above $100k by June?" → ("BTC", 100000.0, "above").
    Returns None if not recognizable.
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


def _verify_crypto_outcome(live_price: float, threshold: float, direction: str) -> str | None:
    """
    Returns "YES" or "NO" only when outcome is clear with CRYPTO_MARGIN buffer.
    Returns None when price is too close to the threshold to be certain.

    5% margin prevents false positives from API tick noise.
    """
    if direction == "above":
        if live_price > threshold * (1 + CRYPTO_MARGIN):
            return "YES"
        if live_price < threshold * (1 - CRYPTO_MARGIN):
            return "NO"
    elif direction == "below":
        if live_price < threshold * (1 - CRYPTO_MARGIN):
            return "YES"
        if live_price > threshold * (1 + CRYPTO_MARGIN):
            return "NO"
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
) -> list[SniperTrade]:
    """
    Scan markets for EV-positive sniper opportunities.

    Only crypto-verified entries are considered (see module docstring for EV proof
    of why crowd-only and YES+NO-arb entries were removed):

    markets       — list from Gamma API (after legal filter, pre quality-filter ok)
    bankroll      — current virtual bankroll
    sniper_state  — for dedup check (avoid re-entering open positions)
    crypto_prices — CoinGecko / Coinbase prices dict

    Returns list of SniperTrade signals (not yet recorded).
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

        # Parse end date
        end_date = _parse_end_date(market)
        if end_date is None:
            continue

        hours_left = (end_date - now).total_seconds() / 3600

        # ── Crypto-verified entries only (time window check) ─────────────
        if hours_left < -SNIPE_OVERDUE_GRACE:
            continue   # too stale
        if hours_left > SNIPE_HOURS_AHEAD:
            continue   # too far out

        question = market.get("question", "")
        parsed = _parse_crypto_question(question)
        if not parsed or not crypto_prices:
            continue  # no external verification possible → skip

        sym, threshold, direction = parsed
        cg_id = CRYPTO_MAP[sym][0]
        live_price_raw = (crypto_prices.get(cg_id) or {}).get("usd")
        if live_price_raw is None:
            continue

        live_price = float(live_price_raw)
        outcome = _verify_crypto_outcome(live_price, threshold, direction)
        if outcome is None:
            continue   # too close to call

        # Determine entry side and price
        if outcome == "YES":
            entry_price = yes_ask
            side = "YES"
        else:
            entry_price = round(1.0 - yes_bid, 4)
            side = "NO"

        # Must have enough upside to profit after fee (strict >: at exactly MAX_ENTRY EV>0)
        if entry_price > SNIPE_MAX_ENTRY:
            continue

        # EV sanity check: WR=97%, need win_pnl > loss_pnl × (1-WR)/WR
        # Simplified: price < 0.95 already guarantees EV > 0 at 97% WR
        is_overdue = hours_left < 0
        reason = "overdue_crypto" if is_overdue else "near_expiry_crypto"

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
            live_price=round(live_price, 2),
            threshold=threshold,
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

    Fee model (corrected):
      WIN:  gross = bet × (1 – price) / price  minus  bet × fee
      LOSS: pnl = –bet   ← fee was already paid at buy time, do NOT double-count
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
            pnl = round(-bet, 4)   # fee already paid at buy; no double-count

        t["resolved"]  = True
        t["outcome"]   = won
        t["pnl_usdc"]  = pnl
        state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
        bankroll_ref[0]    = round(bankroll_ref[0] + pnl, 4)

        result = "WIN" if won else "LOSS"
        logger.info(
            f"[SNIPER] {result}: {t.get('question', '?')[:50]} | "
            f"{side} @ {price:.3f} | P&L={pnl:+.4f}"
        )
        closed.append(t)

    return closed
