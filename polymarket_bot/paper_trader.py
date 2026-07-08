"""
Paper trading engine — virtual portfolio backed by paper_trades.json.

Virtual bankroll starts at $1,000 (raised from $100 in July 2026;
target: +20% MoM = $200/month).
Only S3 Sniper positions are recorded here now; S5 NicheSpecialist trades
are legacy and will resolve naturally as markets close.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = Path("paper_trades.json")
STARTING_BANKROLL = 1000.0


@dataclass
class PaperTrade:
    date: str
    market_id: str
    question: str
    category: str
    side: str           # YES or NO
    price: float        # market price at signal time
    fair_prob: float    # Claude's estimate
    edge: float
    bet_usdc: float
    shares: float
    resolved: bool = False
    outcome: Optional[bool] = None   # True=YES resolved, False=NO resolved
    pnl_usdc: Optional[float] = None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            # One-time capital raise: cached state predates the $1,000 bankroll.
            # Additive top-up keeps any open-position accounting intact.
            old_start = state.get("start_bankroll", STARTING_BANKROLL)
            if old_start < STARTING_BANKROLL:
                delta = STARTING_BANKROLL - old_start
                state["start_bankroll"] = STARTING_BANKROLL
                state["virtual_bankroll"] = round(
                    state.get("virtual_bankroll", old_start) + delta, 4
                )
                logger.info(
                    f"Bankroll migration: +${delta:.0f} → "
                    f"start=${STARTING_BANKROLL:.0f}, current=${state['virtual_bankroll']:.2f}"
                )
            return state
        except Exception:
            pass
    return {
        "virtual_bankroll": STARTING_BANKROLL,
        "start_bankroll": STARTING_BANKROLL,
        "total_claude_cost_usd": 0.0,
        "total_realized_pnl": 0.0,
        "trades": [],
        "last_run": None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def check_resolutions(state: dict, markets_by_id: dict) -> list[PaperTrade]:
    """
    For each open paper trade, check if the market has resolved.
    Returns list of newly resolved trades.
    """
    newly_resolved = []
    polymarket_fee = 0.018  # Polymarket taker fee as of March 2026

    for trade_dict in state["trades"]:
        if trade_dict.get("resolved"):
            continue

        market_id = trade_dict["market_id"]
        market = markets_by_id.get(market_id)
        if market is None:
            continue

        # Market resolved when closed=True and outcomePrices are 0 or 1
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

        # Determine YES/NO resolution: prices[0]=YES price, prices[1]=NO price
        yes_won = prices[0] >= 0.99
        no_won = prices[1] >= 0.99

        if not (yes_won or no_won):
            continue  # not yet fully resolved

        side = trade_dict["side"]
        won = (side == "YES" and yes_won) or (side == "NO" and no_won)

        bet = trade_dict["bet_usdc"]
        price = trade_dict["price"]
        if won:
            gross = bet * (1 - price) / price
            pnl = gross - bet * polymarket_fee
        else:
            pnl = -bet  # fee already paid at buy time; do not double-count on loss

        trade_dict["resolved"] = True
        trade_dict["outcome"] = won
        trade_dict["pnl_usdc"] = round(pnl, 4)
        state["virtual_bankroll"] += pnl
        state["total_realized_pnl"] += pnl
        newly_resolved.append(PaperTrade(**trade_dict))

    return newly_resolved


def is_already_open(state: dict, market_id: str) -> bool:
    """Return True if there is already an unresolved trade for this market."""
    return any(
        t["market_id"] == market_id and not t.get("resolved")
        for t in state["trades"]
    )


def record_paper_trade(state: dict, trade: PaperTrade) -> None:
    state["virtual_bankroll"] -= trade.bet_usdc  # reserve bet amount
    state["trades"].append(asdict(trade))
    logger.info(
        f"[PAPER] {trade.question[:55]} | {trade.side} @ {trade.price:.3f} "
        f"| edge={trade.edge:.2%} | ${trade.bet_usdc:.2f} USDC"
    )
