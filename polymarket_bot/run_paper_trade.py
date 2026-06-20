"""
Daily paper trading runner — called by GitHub Actions.

Flow:
  1. Fetch real Polymarket markets (Gamma API)
  2. Resolve any open S5/S2/S3 positions → update P&L
  3. Run S3 Sniper v3 (Kelly-sized, crypto + sports verified) → record new signals
  4. Send Telegram report
  5. Save updated state

S5 (NicheSpecialist) and S2 (Grid) new entries are permanently suspended.
All capital is allocated to S3 Sniper targeting MoM 20% returns.
Existing open S5/S2 positions continue to resolve normally.
"""

import logging
import os
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("paper_trade")

# ── Import bot modules ──────────────────────────────────────────────────────

from scanner import fetch_active_markets
from paper_trader import load_state, save_state, check_resolutions
from strategy_grid import load_grid_state, save_grid_state, check_grid_updates
from strategy_sniper import (
    load_sniper_state, save_sniper_state,
    find_sniper_candidates, record_sniper_trade, check_sniper_resolutions,
    fetch_crypto_prices, fetch_sports_results,
)
from telegram_reporter import send_daily_report, send_signal_alert

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


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "daily report" if SEND_DAILY_REPORT else "intraday scan"
    logger.info(f"=== Paper trading run [{mode}] ===")
    state = load_state()
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

    # Step 3: Apply legal filter
    blocked = [m for m in raw_markets if _is_blocked(m)]
    if blocked:
        logger.info(f"Blocked {len(blocked)} Taiwan politics/election markets")
    raw_markets_clean = [m for m in raw_markets if not _is_blocked(m)]

    # S5 (NicheSpecialist) and S2 (Grid) new entries are suspended — all capital
    # is allocated to S3 Sniper (Kelly-sized, externally verified, 97%+ WR).
    # Resolution of existing open S5/S2 positions still runs below.
    new_trades: list = []
    logger.info(
        f"S5 suspended (MoM 20% target via S3 only) | "
        f"Balance: ${state['virtual_bankroll']:.2f}"
    )

    # Step 4: S2 Grid strategy scan
    grid_state = load_grid_state()
    grid_bankroll = [state["virtual_bankroll"]]

    closed_grids = check_grid_updates(grid_state, markets_by_id, grid_bankroll)
    if closed_grids:
        logger.info(f"Grid: {len(closed_grids)} positions closed")
        for g in closed_grids:
            logger.info(f"  [GRID] {g['question'][:50]} | P&L: {g['pnl_usdc']:+.4f}")

    # Grid new entries suspended — only resolving existing positions
    new_grid_trades: list = []
    state["virtual_bankroll"] = grid_bankroll[0]

    logger.info(
        f"Grid suspended (S2 new entries off) | "
        f"Total P&L: ${grid_state['total_pnl']:+.4f}"
    )

    # Step 4b: S3 Near-Expiry Certainty Sniper
    sniper_state = load_sniper_state()
    sniper_bankroll = [state["virtual_bankroll"]]

    # Resolve existing sniper positions
    closed_snipers = check_sniper_resolutions(sniper_state, markets_by_id, sniper_bankroll)
    if closed_snipers:
        logger.info(f"Sniper: {len(closed_snipers)} positions resolved")

    # Fetch external price/result data for S3 Sniper verification
    crypto_prices = fetch_crypto_prices()
    if crypto_prices:
        logger.info(f"Crypto prices fetched: {list(crypto_prices.keys())}")

    sports_results = fetch_sports_results()

    # Scan all legal markets (not just tradeable) to catch more near-expiry candidates
    new_sniper_trades = []
    sniper_candidates = find_sniper_candidates(
        markets=raw_markets_clean,
        bankroll=sniper_bankroll[0],
        sniper_state=sniper_state,
        crypto_prices=crypto_prices,
        sports_results=sports_results,
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
