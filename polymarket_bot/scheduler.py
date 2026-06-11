"""
APScheduler-based job scheduler.

Jobs:
  tier1_scan      — every 10 min, 00:00–08:00 every 60 min
                    detects price changes, zero Claude cost
  tier2_evaluate  — triggered when tier1_scan finds changed markets
  niche_deepscan  — 06:00 / 12:00 / 18:00 daily, always evaluates niche markets
  crypto_scan     — every 15 min, 24/7 (crypto never sleeps)
  sports_scan     — configurable pre-game windows (currently every 30 min)
"""

import logging
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import SCAN_INTERVAL_SECONDS
from scanner import filter_markets
from tier1_monitor import fetch_price_snapshot, detect_changed_markets
from valuator import estimate_fair_probability
from strategy import evaluate_market
from executor import place_limit_order, cancel_all_open_orders
from tracker import Tracker

logger = logging.getLogger("scheduler")

# Category sets for targeted scans
NICHE_CATS   = {"weather", "science", "entertainment"}
CRYPTO_CATS  = {"crypto"}


# ─────────────────────────────────────────────────────────────────────────────
# Core evaluation logic (shared by all jobs)
# ─────────────────────────────────────────────────────────────────────────────

def _evaluate_and_trade(
    markets: list,
    clob_client,
    tracker: Tracker,
    job_name: str,
) -> None:
    if not tracker.is_alive():
        cancel_all_open_orders(clob_client)
        raise SystemExit("Kill switch: balance at or below minimum.")

    tradeable = filter_markets(markets)
    if not tradeable:
        logger.info(f"[{job_name}] No tradeable markets after filtering.")
        return

    bankroll = tracker._cached_balance or tracker.get_usdc_balance()
    now_str = datetime.now(timezone.utc).isoformat()
    signals = orders = 0

    for market in tradeable:
        bankroll = tracker.get_usdc_balance()
        if bankroll <= 0:
            cancel_all_open_orders(clob_client)
            raise SystemExit("Balance exhausted.")

        fair = estimate_fair_probability(market, tracker)
        if fair is None:
            continue

        signal = evaluate_market(market, fair, bankroll)
        if signal is None:
            continue
        signals += 1

        if place_limit_order(clob_client, signal, tracker, now_str):
            orders += 1

    logger.info(
        f"[{job_name}] Done — signals={signals} orders={orders} | "
        f"{tracker.summary()}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Individual job functions (injected with clob_client + tracker at schedule time)
# ─────────────────────────────────────────────────────────────────────────────

def make_tier1_job(clob_client, tracker: Tracker):
    def job():
        logger.info("[tier1] Price change scan starting...")
        snapshot = fetch_price_snapshot()
        changed = detect_changed_markets(snapshot)
        if changed:
            _evaluate_and_trade(changed, clob_client, tracker, "tier1")
        else:
            logger.info("[tier1] No price changes detected — Claude not called.")
    return job


def make_niche_deepscan_job(clob_client, tracker: Tracker):
    def job():
        logger.info("[niche_deepscan] Scheduled niche market scan...")
        snapshot = fetch_price_snapshot()
        niche = [m for m in snapshot if m.get("category", "").lower() in NICHE_CATS]
        if niche:
            _evaluate_and_trade(niche, clob_client, tracker, "niche_deepscan")
    return job


def make_crypto_job(clob_client, tracker: Tracker):
    def job():
        logger.info("[crypto] Crypto market scan...")
        snapshot = fetch_price_snapshot(limit=300)
        crypto = [m for m in snapshot if m.get("category", "").lower() in CRYPTO_CATS]
        if crypto:
            _evaluate_and_trade(crypto, clob_client, tracker, "crypto")
    return job


def make_sports_job(clob_client, tracker: Tracker):
    def job():
        logger.info("[sports] Sports market scan...")
        snapshot = fetch_price_snapshot(limit=400)
        sports = [m for m in snapshot if m.get("category", "").lower() == "sports"
                  and int(m.get("daysToResolution") or m.get("days_to_resolution") or 99) <= 2]
        if sports:
            _evaluate_and_trade(sports, clob_client, tracker, "sports")
    return job


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler builder
# ─────────────────────────────────────────────────────────────────────────────

def build_scheduler(clob_client, tracker: Tracker) -> BlockingScheduler:
    sched = BlockingScheduler(timezone="UTC")

    # Tier-1 price-change scan
    # Active hours (08:00–22:00 UTC): every 10 minutes
    sched.add_job(
        make_tier1_job(clob_client, tracker),
        CronTrigger(hour="8-22", minute="*/10"),
        id="tier1_active",
        name="Tier-1 active hours (10 min)",
        max_instances=1,
        coalesce=True,
    )
    # Off-peak (22:00–08:00 UTC): every 60 minutes
    sched.add_job(
        make_tier1_job(clob_client, tracker),
        CronTrigger(hour="22-23,0-7", minute="0"),
        id="tier1_offpeak",
        name="Tier-1 off-peak (60 min)",
        max_instances=1,
        coalesce=True,
    )

    # Niche deep scan: 06:00, 12:00, 18:00 UTC daily
    sched.add_job(
        make_niche_deepscan_job(clob_client, tracker),
        CronTrigger(hour="6,12,18", minute="0"),
        id="niche_deepscan",
        name="Niche deep scan (3×/day)",
        max_instances=1,
        coalesce=True,
    )

    # Crypto: every 15 minutes, 24/7
    sched.add_job(
        make_crypto_job(clob_client, tracker),
        CronTrigger(minute="*/15"),
        id="crypto_scan",
        name="Crypto 24/7 (15 min)",
        max_instances=1,
        coalesce=True,
    )

    # Sports: every 30 minutes (catches pre-game windows)
    sched.add_job(
        make_sports_job(clob_client, tracker),
        CronTrigger(minute="*/30"),
        id="sports_scan",
        name="Sports pre-game (30 min)",
        max_instances=1,
        coalesce=True,
    )

    return sched
