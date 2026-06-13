"""
Daily paper trading runner — called by GitHub Actions.

Flow:
  1. Fetch real Polymarket markets (Gamma API)
  2. Check if any previously recorded markets have resolved → update P&L
  3. Run S5 NicheSpecialist strategy on today's markets → record new signals
  4. Send HTML email report
  5. Save updated state

Estimator modes (set PAPER_ESTIMATOR env var):
  "free"   (default) — bias-corrected mid-price heuristic, zero Claude cost
  "claude"           — real Claude API calls (~$0.65–2/day)
"""

import json
import logging
import os
import random
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("paper_trade")

# ── Import bot modules ──────────────────────────────────────────────────────

from scanner import fetch_active_markets, filter_markets
from strategy import evaluate_market
from tracker import Tracker
from paper_trader import (
    load_state, save_state, check_resolutions,
    record_paper_trade, is_already_open, PaperTrade, STARTING_BANKROLL,
)
from telegram_reporter import send_daily_report, send_signal_alert

NICHE_CATEGORIES = {"weather", "science", "entertainment"}
ESTIMATOR_MODE = os.environ.get("PAPER_ESTIMATOR", "free").lower()
# SEND_DAILY_REPORT=false → intraday scan mode (no full report, only signal alerts)
SEND_DAILY_REPORT = os.environ.get("SEND_DAILY_REPORT", "true").lower() == "true"


# ── Free heuristic estimator (zero Claude cost) ──────────────────────────────

def _free_estimate(market: dict) -> float:
    """
    Bias-corrected mid-price estimator — no API cost.

    Applies two known Polymarket biases:
    1. Favorite-longshot: prices <25% are overpriced ~4%, prices >75% underpriced ~3%
    2. Niche market noise: extra ±3% random correction (wider mispricings expected)

    This won't be as accurate as Claude but is good enough to identify
    directional signals for observation purposes.
    """
    bid = float(market.get("_best_bid") or market.get("bestBid") or 0)
    ask = float(market.get("_best_ask") or market.get("bestAsk") or 0)
    if bid <= 0 or ask <= 0:
        return 0.5
    mid = (bid + ask) / 2.0

    # Longshot bias correction
    if mid < 0.25:
        mid = min(mid + 0.04 * (0.25 - mid) / 0.25, 0.95)
    elif mid > 0.75:
        mid = max(mid - 0.03 * (mid - 0.75) / 0.25, 0.05)

    # Niche market extra noise (wider spread = more uncertainty)
    spread = ask - bid
    if spread > 0.06:
        mid += random.gauss(0, spread * 0.3)

    return max(0.04, min(0.96, mid))


def get_fair_probability(market: dict, tracker) -> float | None:
    """Route to free heuristic or real Claude depending on PAPER_ESTIMATOR."""
    if ESTIMATOR_MODE == "claude":
        from valuator import estimate_fair_probability
        return estimate_fair_probability(market, tracker)
    return _free_estimate(market)


# ── Fake CLOB client for paper trading (no real orders) ─────────────────────

class PaperClobClient:
    def get_balance(self):
        state = load_state()
        return str(state["virtual_bankroll"])


class PaperTracker(Tracker):
    def __init__(self, state: dict):
        self._state = state
        self._clob = PaperClobClient()
        self.cumulative_claude_cost_usd = state["total_claude_cost_usd"]
        self.realized_pnl_usdc = state["total_realized_pnl"]
        self.open_trades = []
        self._cached_balance = state["virtual_bankroll"]

    def record_claude_usage(self, input_tokens: int, output_tokens: int) -> float:
        cost = super().record_claude_usage(input_tokens, output_tokens)
        self._state["total_claude_cost_usd"] = self.cumulative_claude_cost_usd
        return cost

    def get_usdc_balance(self) -> float:
        self._cached_balance = self._state["virtual_bankroll"]
        return self._cached_balance


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "daily report" if SEND_DAILY_REPORT else "intraday scan"
    logger.info(f"=== Paper trading run [{mode}] ===")
    state = load_state()
    tracker = PaperTracker(state)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Step 1: Fetch markets
    logger.info("Fetching markets from Gamma API...")
    raw_markets = fetch_active_markets(limit=800)
    markets_by_id = {m.get("id"): m for m in raw_markets if m.get("id")}

    # Step 2: Resolve any open trades
    newly_resolved = check_resolutions(state, markets_by_id)
    if newly_resolved:
        logger.info(f"Resolved {len(newly_resolved)} trades today")
        for t in newly_resolved:
            result = "WIN" if t.outcome else "LOSS"
            logger.info(f"  [{result}] {t.question[:50]} | P&L: {t.pnl_usdc:+.2f}")

    # Step 3: Run S5 strategy on niche markets
    # Log actual categories returned by Gamma API (helps debug mismatches)
    all_cats = {m.get("category", "") or "" for m in raw_markets}
    logger.info(f"Gamma API categories in batch: {sorted(all_cats)}")

    # Primary: category field match; fallback: keyword in question text
    NICHE_KEYWORDS = {"weather", "science", "entertainment", "pop culture",
                      "nature", "climate", "space", "award", "movie", "music",
                      "celebrity", "tv", "film", "show"}

    def is_niche(m: dict) -> bool:
        cat = (m.get("category") or "").lower()
        if cat in NICHE_CATEGORIES:
            return True
        if any(kw in cat for kw in NICHE_KEYWORDS):
            return True
        question = (m.get("question") or m.get("groupSlug") or "").lower()
        return any(kw in question for kw in NICHE_KEYWORDS)

    niche_raw = [m for m in raw_markets if is_niche(m)]
    tradeable = filter_markets(niche_raw)
    logger.info(
        f"S5 candidates: {len(niche_raw)} niche → {len(tradeable)} tradeable "
        f"[estimator={ESTIMATOR_MODE}]"
    )

    bankroll = state["virtual_bankroll"]
    new_trades = []

    for market in tradeable:
        if bankroll <= 1.0:
            logger.warning("Virtual bankroll too low — stopping paper scan")
            break

        fair = get_fair_probability(market, tracker)
        if fair is None:
            continue

        signal = evaluate_market(market, fair, bankroll)
        if signal is None:
            continue

        if is_already_open(state, signal.market_id):
            continue  # already have a position on this market

        trade = PaperTrade(
            date=today,
            market_id=signal.market_id,
            question=signal.market_question,
            category=market.get("category", ""),
            side=signal.side_label,
            price=signal.market_price,
            fair_prob=signal.fair_prob,
            edge=signal.edge,
            bet_usdc=round(signal.bet_usdc, 2),
            shares=round(signal.bet_usdc / signal.market_price, 2),
        )
        record_paper_trade(state, trade)
        new_trades.append(trade.__dict__)
        bankroll = state["virtual_bankroll"]

    logger.info(
        f"Today: {len(new_trades)} new signals | "
        f"Balance: ${state['virtual_bankroll']:.2f} | "
        f"Claude cost: ${state['total_claude_cost_usd']:.4f}"
    )

    # Step 4: Save state
    state["last_run"] = today
    save_state(state)

    # Step 5: Notify via Telegram
    if SEND_DAILY_REPORT:
        send_daily_report(
            state=state,
            new_trades=new_trades,
            newly_resolved=[t.__dict__ for t in newly_resolved],
        )
    elif new_trades:
        send_signal_alert(new_trades=new_trades, state=state)


if __name__ == "__main__":
    main()
