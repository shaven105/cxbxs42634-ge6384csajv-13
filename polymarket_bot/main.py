"""
Polymarket trading bot.

Usage:
  python main.py              # smart scheduler mode (default, recommended)
  python main.py --loop       # legacy 10-min fixed loop mode

Scheduler mode runs tiered jobs:
  - Tier-1 price monitor every 10 min (free, Gamma API only)
  - Claude evaluation only when price changes >5% are detected
  - Niche deep scan at 06:00 / 12:00 / 18:00 UTC
  - Crypto scan every 15 min 24/7
  - Sports scan every 30 min
"""

import argparse
import logging
import logging.handlers
import time
from datetime import datetime, timezone

from config import (
    SCAN_INTERVAL_SECONDS,
    LOG_FILE,
    LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
)
from scanner import get_tradeable_markets
from valuator import estimate_fair_probability
from strategy import evaluate_market
from executor import build_clob_client, place_limit_order, cancel_all_open_orders
from tracker import Tracker

logger = logging.getLogger("main")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(ch)


def run_cycle(clob_client, tracker: Tracker, cycle_num: int) -> None:
    now_str = datetime.now(timezone.utc).isoformat()
    logger.info(f"=== Cycle #{cycle_num} | {now_str} ===")

    if not tracker.is_alive():
        cancel_all_open_orders(clob_client)
        raise SystemExit("Kill switch triggered: balance at or below minimum.")

    bankroll = tracker._cached_balance
    logger.info(f"Bankroll: ${bankroll:.2f} USDC")

    markets = get_tradeable_markets()
    if not markets:
        logger.warning("No tradeable markets found this cycle.")
        return

    logger.info(f"Evaluating {len(markets)} markets...")
    signals = orders = 0

    for i, market in enumerate(markets, start=1):
        if i % 50 == 0:
            logger.info(
                f"Progress {i}/{len(markets)} | signals={signals} "
                f"orders={orders} | {tracker.summary()}"
            )
            time.sleep(1.0)  # brief pause every 50 calls

        fair_prob = estimate_fair_probability(market, tracker)
        if fair_prob is None:
            continue

        # Refresh bankroll before sizing each potential trade
        bankroll = tracker.get_usdc_balance()
        if bankroll <= 0:
            cancel_all_open_orders(clob_client)
            raise SystemExit("Balance exhausted mid-cycle.")

        signal = evaluate_market(market, fair_prob, bankroll)
        if signal is None:
            continue
        signals += 1

        if place_limit_order(clob_client, signal, tracker, now_str):
            orders += 1

    logger.info(
        f"=== Cycle #{cycle_num} done | "
        f"markets={len(markets)} signals={signals} orders={orders} | "
        f"{tracker.summary()} ==="
    )


def _run_loop(clob_client, tracker: Tracker) -> None:
    """Legacy fixed-interval loop (--loop flag)."""
    cycle = 0
    while True:
        cycle += 1
        try:
            run_cycle(clob_client, tracker, cycle)
        except SystemExit as exc:
            logger.critical(str(exc))
            return
        except KeyboardInterrupt:
            logger.info("Interrupted — cancelling orders.")
            cancel_all_open_orders(clob_client)
            return
        except Exception as exc:
            logger.error(f"Unhandled error in cycle #{cycle}: {exc}", exc_info=True)
            time.sleep(30)

        logger.info(f"Sleeping {SCAN_INTERVAL_SECONDS}s until next cycle...")
        time.sleep(SCAN_INTERVAL_SECONDS)


def _run_scheduler(clob_client, tracker: Tracker) -> None:
    """Smart scheduler mode — tiered jobs, auto-scheduled."""
    from scheduler import build_scheduler
    sched = build_scheduler(clob_client, tracker)

    logger.info("Scheduler starting. Jobs registered:")
    for job in sched.get_jobs():
        logger.info(f"  [{job.id}] {job.name} — next: {job.next_run_time}")

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped — cancelling open orders.")
        cancel_all_open_orders(clob_client)
        sched.shutdown(wait=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket trading bot")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Use legacy fixed 10-min loop instead of smart scheduler",
    )
    args = parser.parse_args()

    setup_logging()
    logger.info(f"Polymarket bot starting ({'loop' if args.loop else 'scheduler'} mode).")

    clob_client = build_clob_client()
    tracker = Tracker(clob_client)

    balance = tracker.get_usdc_balance()
    logger.info(f"Initial balance: ${balance:.2f} USDC")
    if balance <= 0:
        logger.critical("No USDC balance. Exiting.")
        return

    if args.loop:
        _run_loop(clob_client, tracker)
    else:
        _run_scheduler(clob_client, tracker)


if __name__ == "__main__":
    main()
