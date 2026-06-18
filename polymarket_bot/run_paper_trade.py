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
from strategy_grid import (
    load_grid_state, save_grid_state, is_grid_candidate,
    is_already_gridded, open_grid, record_grid_trade, check_grid_updates,
)
from strategy_sniper import (
    load_sniper_state, save_sniper_state, is_already_sniped,
    find_sniper_candidates, record_sniper_trade, check_sniper_resolutions,
    fetch_crypto_prices,
)
from telegram_reporter import send_daily_report, send_signal_alert

NICHE_CATEGORIES = {"weather", "science", "entertainment"}
ESTIMATOR_MODE = os.environ.get("PAPER_ESTIMATOR", "free").lower()
# SEND_DAILY_REPORT=false → intraday scan mode (no full report, only signal alerts)
SEND_DAILY_REPORT = os.environ.get("SEND_DAILY_REPORT", "true").lower() == "true"

# ── Legal exclusion: Taiwan politics / elections ──────────────────────────────
# Blocked for legal compliance — do not trade on these markets.
_TW_BLOCKED_KEYWORDS = {
    # Political parties
    "dpp", "democratic progressive party",
    "kmt", "kuomintang", "nationalist party",
    # Political figures
    "tsai ing-wen",
    "lai ching-te", "william lai", "賴清德",
    "han kuo-yu", "韓國瑜",
    "hou yu-ih", "侯友宜",
    "ko wen-je", "柯文哲",
    # Election / politics topics
    "taiwan election", "taiwan president",
    "taiwan legislative", "taiwan referendum",
    "taiwan politics", "taiwan independence",
    "cross-strait", "cross strait",
    "taiwan strait",
}


def _is_blocked(market: dict) -> bool:
    """Return True if the market touches Taiwan politics/elections."""
    tags = market.get("tags") or []
    tags_str = " ".join(tags) if isinstance(tags, list) else str(tags)
    text = " ".join([
        (market.get("question") or ""),
        (market.get("description") or ""),
        (market.get("groupSlug") or ""),
        (market.get("category") or ""),
        tags_str,
    ]).lower()
    return any(kw in text for kw in _TW_BLOCKED_KEYWORDS)


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

    # Step 3: Apply legal filter then scan all markets for edge
    # Legal filter: remove Taiwan politics/elections first
    blocked = [m for m in raw_markets if _is_blocked(m)]
    if blocked:
        logger.info(f"Blocked {len(blocked)} Taiwan politics/election markets")
    raw_markets_clean = [m for m in raw_markets if not _is_blocked(m)]

    # Apply quality filter (liquidity/spread/binary) to all remaining markets.
    # Gamma API returns empty category strings, so niche pre-filtering is dropped —
    # the edge threshold in evaluate_market naturally selects mispriced markets.
    tradeable = filter_markets(raw_markets_clean)
    logger.info(
        f"S5 candidates: {len(raw_markets_clean)} total → {len(tradeable)} tradeable "
        f"[estimator={ESTIMATOR_MODE}]"
    )

    niche_raw = tradeable  # kept for compatibility with downstream logging

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
        f"S5: {len(new_trades)} new signals | "
        f"Balance: ${state['virtual_bankroll']:.2f} | "
        f"Claude cost: ${state['total_claude_cost_usd']:.4f}"
    )

    # Step 4: S2 Grid strategy scan
    grid_state = load_grid_state()
    grid_bankroll = [state["virtual_bankroll"]]

    closed_grids = check_grid_updates(grid_state, markets_by_id, grid_bankroll)
    if closed_grids:
        logger.info(f"Grid: {len(closed_grids)} positions closed")
        for g in closed_grids:
            logger.info(f"  [GRID] {g['question'][:50]} | P&L: {g['pnl_usdc']:+.4f}")

    new_grid_trades = []
    for market in tradeable:
        if not is_grid_candidate(market):
            continue
        if is_already_gridded(grid_state, market.get("id", "")):
            continue
        grid_signal = open_grid(market, grid_bankroll[0])
        if grid_signal is None:
            continue
        record_grid_trade(grid_state, grid_signal, grid_bankroll)
        new_grid_trades.append(grid_signal.__dict__ if hasattr(grid_signal, '__dict__') else vars(grid_signal))

    state["virtual_bankroll"] = grid_bankroll[0]

    logger.info(
        f"Grid: {len(new_grid_trades)} new positions | "
        f"Total P&L: ${grid_state['total_pnl']:+.4f}"
    )

    # Step 4b: S3 Near-Expiry Certainty Sniper
    sniper_state = load_sniper_state()
    sniper_bankroll = [state["virtual_bankroll"]]

    # Resolve existing sniper positions
    closed_snipers = check_sniper_resolutions(sniper_state, markets_by_id, sniper_bankroll)
    if closed_snipers:
        logger.info(f"Sniper: {len(closed_snipers)} positions resolved")

    # Fetch crypto prices once for all sniper candidates
    crypto_prices = fetch_crypto_prices()
    if crypto_prices:
        logger.info(f"Crypto prices fetched: {list(crypto_prices.keys())}")

    # Scan all legal markets (not just tradeable) to catch more near-expiry candidates
    new_sniper_trades = []
    sniper_candidates = find_sniper_candidates(
        markets=raw_markets_clean,
        bankroll=sniper_bankroll[0],
        sniper_state=sniper_state,
        crypto_prices=crypto_prices,
    )
    for signal in sniper_candidates:
        if sniper_bankroll[0] < signal.bet_usdc:
            continue
        record_sniper_trade(sniper_state, signal, sniper_bankroll)
        new_sniper_trades.append(signal.__dict__)

    state["virtual_bankroll"] = sniper_bankroll[0]

    logger.info(
        f"Sniper: {len(new_sniper_trades)} new positions | "
        f"Total P&L: ${sniper_state['total_pnl']:+.4f}"
    )

    # Step 5: Save state
    state["last_run"] = today
    save_state(state)
    save_grid_state(grid_state)
    save_sniper_state(sniper_state)

    # Step 6: Notify via Telegram
    if SEND_DAILY_REPORT:
        send_daily_report(
            state=state,
            new_trades=new_trades,
            newly_resolved=[t.__dict__ for t in newly_resolved],
            new_grid_trades=new_grid_trades,
            closed_grids=closed_grids,
            new_sniper_trades=new_sniper_trades,
            closed_snipers=closed_snipers,
        )
    elif new_trades or new_grid_trades or new_sniper_trades:
        send_signal_alert(
            new_trades=new_trades,
            state=state,
            new_grid_trades=new_grid_trades,
            new_sniper_trades=new_sniper_trades,
        )


if __name__ == "__main__":
    main()
