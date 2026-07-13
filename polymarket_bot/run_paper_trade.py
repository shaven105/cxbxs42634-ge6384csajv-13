"""
Daily paper trading runner — called by GitHub Actions.

Flow:
  1. Fetch real Polymarket markets (Gamma API)
  2. Resolve any open S5/S2/S3 positions → update P&L
  3. Run S3 Sniper v3.1 (Resolution Lag — crypto + sports verified) → record signals
  4. Send Telegram report
  5. Save updated state

S5 and S2 new entries are permanently suspended.
All capital is allocated to S3 Sniper (Kelly-sized, externally verified).

Monthly budget:
  Each intraday scan counts toward MONTHLY_SCAN_LIMIT (default 1200).
  When exhausted, scans skip until the month rolls over.
  Daily reports and strategy reviews are never gated by the budget.
  Intraday job runs the scanner TWICE per cron tick (effective ~2.5min cadence).
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
    fetch_crypto_prices, fetch_sports_results, fetch_mlb_results,
)
from telegram_reporter import send_daily_report, send_signal_alert

# SEND_DAILY_REPORT=false → intraday scan mode (no full report, only signal alerts)
SEND_DAILY_REPORT = os.environ.get("SEND_DAILY_REPORT", "true").lower() == "true"

# Monthly scan budget — only applied to intraday scans, not daily reports.
# GitHub Actions cron minimum is 5 min; intraday job runs 2 scans per tick
# (effective interval ~2.5 min). Default 1200 ≈ ~2.5 days of max-rate scanning.
# Adjust MONTHLY_SCAN_LIMIT repo variable to taste.
MONTHLY_SCAN_LIMIT = int(os.environ.get("MONTHLY_SCAN_LIMIT", "1200"))

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


def _check_scan_budget(state: dict) -> bool:
    """
    Increment and check the monthly intraday scan counter.
    Returns False when the budget is exhausted for the current month.
    Resets automatically on month rollover.
    Only called for intraday scans (SEND_DAILY_REPORT=False).
    """
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")

    if state.get("scan_budget_month") != this_month:
        state["scan_budget_month"] = this_month
        state["scan_budget_count"] = 0

    state["scan_budget_count"] = state.get("scan_budget_count", 0) + 1
    used = state["scan_budget_count"]

    if used > MONTHLY_SCAN_LIMIT:
        logger.info(
            f"Monthly scan budget exhausted ({used}/{MONTHLY_SCAN_LIMIT}) "
            f"— skipping until {this_month} rolls over"
        )
        return False

    if used % 50 == 0:
        logger.info(f"Monthly scan budget: {used}/{MONTHLY_SCAN_LIMIT}")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = "daily report" if SEND_DAILY_REPORT else "intraday scan"
    logger.info(f"=== Paper trading run [{mode}] ===")

    # Answer any pending Telegram commands (/summary etc.).  The scan loop's
    # dedicated listener handles the fast path; this catches commands sent
    # while no listener was running (private-repo mode, daily jobs).
    try:
        from telegram_commands import poll_once
        poll_once()
    except Exception as exc:
        logger.warning(f"telegram command poll failed: {exc}")

    state = load_state()

    # Monthly budget gate — intraday scans only
    if not SEND_DAILY_REPORT:
        if not _check_scan_budget(state):
            save_state(state)   # persist incremented counter
            return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Step 1: Fetch markets
    logger.info("Fetching markets from Gamma API...")
    raw_markets = fetch_active_markets(limit=800)
    markets_by_id = {m.get("id"): m for m in raw_markets if m.get("id")}

    # Step 2: Resolve any open trades (S5 / S2 legacy + S3 active)
    newly_resolved = check_resolutions(state, markets_by_id)
    if newly_resolved:
        logger.info(f"Resolved {len(newly_resolved)} trades today")
        for t in newly_resolved:
            result = "WIN" if t.outcome else "LOSS"
            logger.info(f"  [{result}] {t.question[:50]} | P&L: {t.pnl_usdc:+.2f}")

    # Step 3: Legal filter
    blocked = [m for m in raw_markets if _is_blocked(m)]
    if blocked:
        logger.info(f"Blocked {len(blocked)} Taiwan politics/election markets")
    raw_markets_clean = [m for m in raw_markets if not _is_blocked(m)]

    new_trades: list = []

    # Step 4: S2 Grid — resolve existing positions only (no new entries)
    grid_state = load_grid_state()
    grid_bankroll = [state["virtual_bankroll"]]
    closed_grids = check_grid_updates(grid_state, markets_by_id, grid_bankroll)
    if closed_grids:
        logger.info(f"Grid: {len(closed_grids)} positions closed")
        for g in closed_grids:
            logger.info(f"  [GRID] {g['question'][:50]} | P&L: {g['pnl_usdc']:+.4f}")
    new_grid_trades: list = []
    state["virtual_bankroll"] = grid_bankroll[0]

    # Step 4b: S3 Sniper — resolve + scan
    sniper_state = load_sniper_state()
    sniper_bankroll = [state["virtual_bankroll"]]

    # Void esports false-positives (July 2026 substring-match bug): ESPN cannot
    # verify LoL/Valorant markets, so any such open "sports_verified" trade is
    # an error — remove it and refund the stake.  Idempotent.
    import re as _re
    _esports_void = _re.compile(
        r'\b(?:lol|valorant|cs2|csgo|dota|esports?)\b|\b(?:map|game)\s+\d+\s+winner\b', _re.I)
    _bad = [t for t in sniper_state["trades"]
            if not t.get("resolved") and t.get("reason") == "sports_verified"
            and _esports_void.search(t.get("question", ""))]
    if _bad:
        for t in _bad:
            sniper_bankroll[0] = round(sniper_bankroll[0] + t.get("bet_usdc", 0), 4)
            logger.info(f"VOIDED esports false-positive: {t.get('question','')[:60]} "
                        f"(+${t.get('bet_usdc', 0):.2f} refunded)")
        sniper_state["trades"] = [t for t in sniper_state["trades"] if t not in _bad]

    closed_snipers = check_sniper_resolutions(sniper_state, markets_by_id, sniper_bankroll)
    if closed_snipers:
        logger.info(f"Sniper: {len(closed_snipers)} positions resolved")

    crypto_prices  = fetch_crypto_prices()
    sports_results = fetch_sports_results()
    mlb_results    = fetch_mlb_results()
    # Merge MLB results (more reliable team names) with ESPN results
    all_sports = sports_results + mlb_results

    if crypto_prices:
        logger.info(f"Crypto prices fetched: {list(crypto_prices.keys())}")
    logger.info(f"Sports: {len(sports_results)} ESPN + {len(mlb_results)} MLB = {len(all_sports)} total")

    new_sniper_trades = []
    for signal in find_sniper_candidates(
        markets=raw_markets_clean,
        bankroll=sniper_bankroll[0],
        sniper_state=sniper_state,
        crypto_prices=crypto_prices,
        sports_results=all_sports,
    ):
        if sniper_bankroll[0] < signal.bet_usdc:
            continue
        record_sniper_trade(sniper_state, signal, sniper_bankroll)
        new_sniper_trades.append(signal.__dict__)

    state["virtual_bankroll"] = sniper_bankroll[0]
    logger.info(
        f"Sniper: {len(new_sniper_trades)} new | balance=${state['virtual_bankroll']:.2f} | "
        f"total_pnl={sniper_state['total_pnl']:+.4f}"
    )

    # Step 5: Save all state
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
