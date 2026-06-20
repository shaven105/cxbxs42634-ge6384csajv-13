"""
S2 Grid / Spread-Capture strategy for Polymarket prediction markets.

Concept
-------
Standard grid trading buys low and sells high in a price range.
On prediction markets, prices converge to 0 or 1 at resolution, so we:
  1. Only grid markets priced 35–65% ("coin-flip" range, uncertain outcome)
  2. Place two virtual limit orders inside the current bid-ask spread:
       BUY  limit at  mid - GRID_OFFSET
       SELL limit at  mid + GRID_OFFSET
  3. On the next scan (≥ 30 min later), if price crossed either level the
     order "filled".  If BOTH fill (price oscillated), we pocket 2×GRID_OFFSET
     per share.  If only one fills, we hold a small directional position.
  4. Hard stop-loss: if market moves outside STOP_BAND (e.g. <20% or >80%),
     exit and record the loss.

P&L accounting mirrors paper_trader.py.
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)

# ── Strategy parameters ────────────────────────────────────────────────────
GRID_MID_MIN = 0.42       # tightened: true coin-flip only (was 0.35)
GRID_MID_MAX = 0.58       # tightened: true coin-flip only (was 0.65)
GRID_OFFSET = 0.015       # place orders 1.5% either side of mid
GRID_MIN_SPREAD = 0.010   # skip if spread < 1.0% (not enough room)
GRID_MAX_SPREAD = 0.080   # tightened: skip illiquid (was 0.200)
GRID_STOP_BAND = 0.20     # stop-loss if price moves ±20% from entry mid
GRID_BET_FRACTION = 0.03  # 3% of bankroll per grid pair (buy + sell)
GRID_MIN_BET = 0.10
GRID_MIN_DAYS_TO_EXPIRY = 14  # skip markets resolving within 2 weeks (price converges, not oscillates)
GRID_FEE_RATE = 0.018     # Polymarket taker fee as of March 2026 (was 0.02)

STATE_FILE = Path("grid_trades.json")


@dataclass
class GridTrade:
    date: str
    market_id: str
    question: str
    entry_mid: float      # mid-price when we placed the grid
    buy_limit: float      # our virtual buy limit price
    sell_limit: float     # our virtual sell limit price
    bet_usdc: float       # notional per side
    # Fill tracking
    buy_filled: bool = False
    sell_filled: bool = False
    buy_fill_price: Optional[float] = None
    sell_fill_price: Optional[float] = None
    # Resolution
    closed: bool = False
    pnl_usdc: Optional[float] = None


def load_grid_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"trades": [], "total_pnl": 0.0}


def save_grid_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _days_to_expiry(market: dict) -> float | None:
    """Return days until market end date, or None if unparseable."""
    for key in ("endDate", "endDateIso", "end_date_iso"):
        raw = market.get(key)
        if not raw:
            continue
        try:
            if isinstance(raw, (int, float)):
                end_dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            else:
                s = str(raw).strip().rstrip("Z")
                if "T" in s:
                    end_dt = datetime.fromisoformat(s)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                else:
                    end_dt = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            continue
    return None


def is_grid_candidate(market: dict) -> bool:
    """True if market is a genuine coin-flip with enough time left to oscillate."""
    mid = market.get("_mid_price", 0)
    spread = market.get("_best_ask", 0) - market.get("_best_bid", 0)
    if not (GRID_MID_MIN <= mid <= GRID_MID_MAX
            and GRID_MIN_SPREAD <= spread <= GRID_MAX_SPREAD):
        return False
    # Markets close to expiry will converge to 0/1, not oscillate — skip them
    days = _days_to_expiry(market)
    if days is not None and days < GRID_MIN_DAYS_TO_EXPIRY:
        return False
    return True


def open_grid(market: dict, bankroll: float) -> Optional[GridTrade]:
    """Return a new GridTrade signal for this market, or None."""
    if not is_grid_candidate(market):
        return None

    bet = max(GRID_MIN_BET, bankroll * GRID_BET_FRACTION)
    mid = market["_mid_price"]
    return GridTrade(
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        market_id=market.get("id", "?"),
        question=(market.get("question") or "")[:120],
        entry_mid=mid,
        buy_limit=round(mid - GRID_OFFSET, 4),
        sell_limit=round(mid + GRID_OFFSET, 4),
        bet_usdc=round(bet, 2),
    )


def is_already_gridded(state: dict, market_id: str) -> bool:
    return any(
        t["market_id"] == market_id and not t.get("closed")
        for t in state["trades"]
    )


def record_grid_trade(state: dict, trade: GridTrade, bankroll_ref: list) -> None:
    """Reserve 2× bet (one for each side) from bankroll and record trade."""
    bankroll_ref[0] -= trade.bet_usdc * 2
    state["trades"].append(asdict(trade))
    logger.info(
        f"[GRID] {trade.question[:50]} | mid={trade.entry_mid:.3f} "
        f"BUY@{trade.buy_limit:.3f} SELL@{trade.sell_limit:.3f} "
        f"${trade.bet_usdc:.2f}/side"
    )


def check_grid_updates(state: dict, markets_by_id: dict, bankroll_ref: list) -> list[dict]:
    """
    For each open grid trade, simulate fills based on current market price
    and resolve closed/stop-loss positions.

    Returns list of closed trade dicts with pnl_usdc filled in.
    """
    closed_trades = []
    fee_rate = GRID_FEE_RATE

    for t in state["trades"]:
        if t.get("closed"):
            continue

        market = markets_by_id.get(t["market_id"])
        if market is None:
            continue

        # Check stop-loss or market resolution
        mid = market.get("_mid_price") or (
            (market.get("_best_bid", 0) + market.get("_best_ask", 0)) / 2
        )
        entry_mid = t["entry_mid"]
        market_closed = market.get("closed", False)

        # Simulate fill: if current price crossed our limit
        if not t["buy_filled"] and mid <= t["buy_limit"]:
            t["buy_filled"] = True
            t["buy_fill_price"] = t["buy_limit"]
            logger.info(f"[GRID] BUY filled: {t['question'][:45]} @ {t['buy_limit']:.3f}")

        if not t["sell_filled"] and mid >= t["sell_limit"]:
            t["sell_filled"] = True
            t["sell_fill_price"] = t["sell_limit"]
            logger.info(f"[GRID] SELL filled: {t['question'][:45]} @ {t['sell_limit']:.3f}")

        # Determine if we should close
        stop_hit = abs(mid - entry_mid) >= GRID_STOP_BAND
        should_close = market_closed or stop_hit or (t["buy_filled"] and t["sell_filled"])

        if not should_close:
            continue

        # Calculate P&L
        pnl = 0.0
        bet = t["bet_usdc"]

        if t["buy_filled"] and t["sell_filled"]:
            # Both sides filled — captured the spread
            buy_p = t["buy_fill_price"]
            sell_p = t["sell_fill_price"]
            shares = bet / buy_p
            gross = shares * (sell_p - buy_p)
            pnl = gross - (bet * 2 * fee_rate)
            logger.info(f"[GRID] Round-trip complete: P&L={pnl:+.3f}")

        elif t["buy_filled"] and not t["sell_filled"]:
            # Bought but couldn't sell — directional P&L at current price
            buy_p = t["buy_fill_price"]
            shares = bet / buy_p
            pnl = shares * (mid - buy_p) - bet * fee_rate
            # Refund unused sell-side reservation
            bankroll_ref[0] += bet

        elif t["sell_filled"] and not t["buy_filled"]:
            # Sold YES but never bought — equivalent to selling at sell_p, closing at mid
            sell_p = t["sell_fill_price"]
            shares = bet / sell_p
            pnl = shares * (sell_p - mid) - bet * fee_rate
            bankroll_ref[0] += bet

        else:
            # Neither filled — refund both sides
            bankroll_ref[0] += bet * 2
            pnl = 0.0

        t["closed"] = True
        t["pnl_usdc"] = round(pnl, 4)
        state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
        bankroll_ref[0] += pnl
        closed_trades.append(t)

    return closed_trades
