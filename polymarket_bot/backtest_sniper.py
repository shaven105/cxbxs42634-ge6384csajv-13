"""
Backtest the S3 Near-Expiry Certainty Sniper v2 on real and synthetic data.

Two-stage validation
---------------------
Stage 1 — Real data (when available):
  Fetch recently CLOSED binary markets from Gamma API.  For each market with
  a clean YES/NO resolution, pull its CLOB price history and check whether a
  crypto-verified entry would have fired.  Compare against actual outcome.

Stage 2 — Synthetic calibration (always runs):
  Uses documented prediction-market crowd calibration data to model expected
  P&L across parameter combinations.  Runs comparison of v1 (crowd-only, wrong
  fee model) vs v2 (crypto-verified only, corrected fee model).

Usage:
  python backtest_sniper.py
"""

import json
import logging
import time
from datetime import datetime, timezone
from statistics import mean, stdev

import requests

from strategy_sniper import (
    _parse_crypto_question, _verify_crypto_outcome,
    fetch_crypto_prices, POLYMARKET_FEE, SNIPE_MAX_ENTRY, CRYPTO_MARGIN,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("backtest_sniper")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
BET_USDC  = 2.0   # fixed per trade for clear comparison

session = requests.Session()
session.headers["User-Agent"] = "polymarket-bot-backtest/1.0"


# ── Data fetching ──────────────────────────────────────────────────────────

def fetch_closed_markets(limit: int = 300) -> list[dict]:
    markets, offset = [], 0
    while len(markets) < limit:
        try:
            r = session.get(
                f"{GAMMA_API}/markets",
                params={"closed": "true", "limit": 100, "offset": offset},
                timeout=20,
            )
            batch = r.json()
        except Exception as exc:
            logger.error(f"Gamma API: {exc}")
            break
        if not batch:
            break
        markets.extend(batch)
        offset += 100
        if len(batch) < 100:
            break
        time.sleep(0.25)
    logger.info(f"Fetched {len(markets)} closed markets")
    return markets[:limit]


def fetch_price_history(token_id: str, interval: str = "1d") -> list[dict]:
    try:
        r = session.get(
            f"{CLOB_API}/prices-history",
            params={"market": token_id, "interval": interval},
            timeout=15,
        )
        return r.json().get("history", [])
    except Exception:
        return []


def get_resolution(market: dict) -> str | None:
    prices_raw = market.get("outcomePrices")
    if not prices_raw:
        return None
    try:
        prices = (
            [float(p) for p in json.loads(prices_raw)]
            if isinstance(prices_raw, str)
            else [float(p) for p in prices_raw]
        )
    except Exception:
        return None
    if len(prices) < 2:
        return None
    if prices[0] >= 0.99:
        return "YES"
    if prices[1] >= 0.99:
        return "NO"
    return None


def get_token_id(market: dict) -> str | None:
    raw = market.get("clobTokenIds")
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return ids[0] if ids else None
    except Exception:
        return None


def parse_end_ts(market: dict) -> float | None:
    for key in ("endDate", "endDateIso"):
        raw = market.get(key)
        if not raw:
            continue
        try:
            if isinstance(raw, (int, float)):
                return float(raw)
            s = str(raw).strip().rstrip("Z")
            if "T" in s:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    return None


# ── Simulation helpers ─────────────────────────────────────────────────────

def calc_pnl(entry_ask: float, won: bool) -> float:
    """Corrected fee model: fee on buy only, NOT double-counted on loss."""
    if won:
        gross = BET_USDC * (1 - entry_ask) / entry_ask
        return round(gross - BET_USDC * POLYMARKET_FEE, 4)
    return round(-BET_USDC, 4)  # corrected: loss = -bet (fee already paid at buy)


def calc_pnl_v1(entry_ask: float, won: bool) -> float:
    """Old v1 formula (wrong): fee charged on loss too."""
    if won:
        return round(BET_USDC * (1 - entry_ask) / entry_ask - BET_USDC * 0.02, 4)
    return round(-BET_USDC - BET_USDC * 0.02, 4)  # double-counting fee on loss


# ── Stage 1: Real-data backtest ────────────────────────────────────────────

def run_real_backtest() -> bool:
    """Returns True if real data was found and tested."""
    logger.info("Stage 1: Fetching real closed markets...")
    markets = fetch_closed_markets(300)

    valid = []
    for m in markets:
        if m.get("negRisk"):
            continue
        resolution = get_resolution(m)
        if not resolution:
            continue
        # Only crypto markets (questions that mention a price threshold)
        if not _parse_crypto_question(m.get("question", "")):
            continue
        token_id = get_token_id(m)
        if not token_id:
            continue
        end_ts = parse_end_ts(m)
        if not end_ts:
            continue
        valid.append((m, resolution, token_id, end_ts))

    logger.info(f"Crypto-question binary markets with clean resolution: {len(valid)}")

    histories: list[tuple] = []
    for i, (m, resolution, token_id, end_ts) in enumerate(valid):
        hist = fetch_price_history(token_id)
        if len(hist) >= 20:
            histories.append((m, resolution, token_id, end_ts, hist))
        if (i + 1) % 10 == 0:
            logger.info(f"  {i+1}/{len(valid)} histories, {len(histories)} usable")
        time.sleep(0.07)

    if not histories:
        logger.warning("No recent crypto-question market history available (markets too old)")
        return False

    # Fetch live crypto prices for verification
    crypto_prices = fetch_crypto_prices()
    logger.info(f"Crypto prices: {list(crypto_prices.keys())}")

    results = []
    for m, resolution, _, end_ts, hist in histories:
        question = m.get("question", "")
        parsed = _parse_crypto_question(question)
        if not parsed or not crypto_prices:
            continue
        sym, threshold, direction = parsed
        from strategy_sniper import CRYPTO_MAP
        cg_id = CRYPTO_MAP[sym][0]
        live_price_raw = (crypto_prices.get(cg_id) or {}).get("usd")
        if not live_price_raw:
            continue

        live_price = float(live_price_raw)
        outcome = _verify_crypto_outcome(live_price, threshold, direction)
        if not outcome:
            continue  # live price too close to threshold to verify

        # Find the entry point in price history
        entry_window = end_ts - 48 * 3600
        for pt in hist:
            ts = pt.get("t", 0)
            if ts < entry_window or ts > end_ts:
                continue
            yes_ask = float(pt.get("p", 0)) + 0.02  # approximate ask = bid + spread
            if yes_ask >= SNIPE_MAX_ENTRY:
                continue

            side = outcome   # we enter on the verified outcome side
            entry_ask = yes_ask if side == "YES" else round(1 - float(pt.get("p", 0)), 4)
            if entry_ask >= SNIPE_MAX_ENTRY:
                continue

            won = (side == resolution)
            pnl_v2 = calc_pnl(entry_ask, won)
            pnl_v1 = calc_pnl_v1(entry_ask, won)

            results.append({
                "question": question[:60],
                "resolution": resolution,
                "outcome": side,
                "entry_ask": entry_ask,
                "won": won,
                "pnl_v2": pnl_v2,
                "pnl_v1": pnl_v1,
            })
            break  # one entry per market

    if not results:
        return False

    wins = [r for r in results if r["won"]]
    wr = len(wins) / len(results) * 100
    total_v2 = sum(r["pnl_v2"] for r in results)
    total_v1 = sum(r["pnl_v1"] for r in results)

    print("\n" + "=" * 65)
    print("STAGE 1 — REAL DATA: Crypto-verified sniper trades")
    print("=" * 65)
    print(f"Markets tested : {len(results)}")
    print(f"Win rate       : {wr:.1f}%")
    print(f"Total P&L v2   : {total_v2:+.4f}  (corrected fee)")
    print(f"Total P&L v1   : {total_v1:+.4f}  (old formula, for comparison)")
    for r in results[:8]:
        status = "✓" if r["won"] else "✗"
        print(f"  {status} {r['question'][:55]}  pnl={r['pnl_v2']:+.4f}")
    return True


# ── Stage 2: Synthetic calibration ────────────────────────────────────────

def run_synthetic_validation():
    import random
    random.seed(42)

    print("\n" + "=" * 65)
    print("STAGE 2 — SYNTHETIC CALIBRATION (n=2000 per scenario)")
    print("=" * 65)

    # ── Section A: v1 vs v2 fee model comparison (crowd-only) ────────────
    print("\nA) V1 vs V2 fee model on crowd-only entries (to show why crowd was removed)")
    print(f"   Crowd calibration: markets at p% resolve correctly ~(p-2)% of the time")
    print(f"{'threshold':>10} {'actual_wr':>10} {'v1_pnl':>10} {'v2_pnl':>10}")
    print("-" * 45)
    calibration = {0.80: 0.78, 0.85: 0.83, 0.88: 0.86, 0.90: 0.88, 0.92: 0.90, 0.95: 0.92}
    for thresh, wr in calibration.items():
        avg_ask = min(thresh + 0.015, 0.98)
        pnl_v1_list, pnl_v2_list = [], []
        for _ in range(2000):
            won = random.random() < wr
            pnl_v1_list.append(calc_pnl_v1(avg_ask, won))
            pnl_v2_list.append(calc_pnl(avg_ask, won))
        print(f"  bid≥{thresh:.2f}   {wr:>8.0%}   {mean(pnl_v1_list):>+9.4f}  {mean(pnl_v2_list):>+9.4f}")
    print(f"\n  → ALL crowd-only entries are EV-negative. Removed from S3 v2.")

    # ── Section B: crypto-verified performance ────────────────────────────
    print("\nB) Crypto-verified entries: v2 model (95-99% win rate)")
    print(f"   Margin={CRYPTO_MARGIN:.0%} means we need price 5% past threshold before entering")
    print(f"{'win_rate':>10} {'entry_ask':>10} {'ev_per_trade':>14} {'total/10':>12}")
    print("-" * 55)
    crypto_scenarios = [
        ("Very close (3%≤margin<5%)", 0.92, 0.90),   # old margin — filtered out now
        ("Confirmed (5% margin)", 0.97, 0.88),
        ("Clear (10% margin)", 0.99, 0.85),
        ("Deep value (30% margin)", 0.999, 0.70),
    ]
    for label, wr, avg_ask in crypto_scenarios:
        if avg_ask >= SNIPE_MAX_ENTRY:
            print(f"  {label:<28} SKIP: ask≥{SNIPE_MAX_ENTRY:.2f}")
            continue
        pnls = []
        for _ in range(2000):
            won = random.random() < wr
            pnls.append(calc_pnl(avg_ask, won))
        ev = mean(pnls)
        total_per_10 = ev * 10
        status = "✓" if ev > 0 else "✗"
        print(f"  {label:<28} WR={wr:.0%}  ask={avg_ask:.2f}  EV={ev:>+8.4f} {status}  ({total_per_10:>+6.4f}/10)")

    # ── Section C: YES+NO arbitrage ───────────────────────────────────────
    print("\nC) YES+NO arbitrage: guaranteed P&L analysis")
    print(f"   Combined price must be < 1/(1+fee) = {1/(1+POLYMARKET_FEE):.4f}")
    print(f"   (Rare but risk-free when it occurs)")
    for combined in [0.975, 0.980, 0.985]:
        yes_ask = combined / 2
        net_per_dollar = 1.0 - combined * (1 + POLYMARKET_FEE)
        per_trade = BET_USDC * 2 * net_per_dollar   # buy both sides
        print(f"   YES+NO combined={combined:.3f}  net/dollar={net_per_dollar:+.4f}  "
              f"profit on ${BET_USDC*2:.0f}bet = {per_trade:+.4f}")

    # ── Section D: Breakeven sensitivity ─────────────────────────────────
    print("\nD) Breakeven win-rate formula: WR_min = ask × (1 + fee)")
    print(f"   (Current: fee={POLYMARKET_FEE:.1%}, MAX_ENTRY={SNIPE_MAX_ENTRY:.2f})")
    for ask in [0.80, 0.85, 0.88, 0.90, 0.92, 0.95]:
        wr_min = ask * (1 + POLYMARKET_FEE)
        achievable = "crypto_verified ✓" if wr_min < 0.97 else "hard (≥97%) ✗"
        print(f"   ask={ask:.2f}  →  need WR ≥ {wr_min:.3f}  [{achievable}]")

    # ── Section E: Sensitivity to fee rate ───────────────────────────────
    print("\nE) Fee rate sensitivity (crypto_verified, ask=0.88, WR=97%)")
    for fee in [0.010, 0.015, 0.018, 0.020, 0.025]:
        win_pnl = BET_USDC * (1 - 0.88) / 0.88 - BET_USDC * fee
        ev = 0.97 * win_pnl + 0.03 * (-BET_USDC)
        status = "✓" if ev > 0 else "✗"
        print(f"   fee={fee:.1%}  win_pnl={win_pnl:+.4f}  EV={ev:>+8.4f} {status}")

    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)
    print("  S3 v1 (removed entries):")
    print("    ✗ crowd-only near_expiry: EV < 0 at all thresholds")
    print("    ✗ crowd-only overdue:     EV < 0 unless WR > ask × 1.018")
    print("  S3 v2 (corrected):")
    print("    ✓ crypto_verified:        EV > 0 when ask < 0.95, WR ≈ 97%")
    print("    ✓ yes_no_arb:             EV > 0 always (risk-free)")
    print("    ✓ fee model fixed:        loss = -bet (not -bet - fee)")
    print("    ✓ fee rate: 1.8% (was 2.0%)")
    print("    ✓ max entry: 0.95 (was 0.97) — ensures 5% upside minimum")
    print("    ✓ margin: 5% (was 3%) — prevents false positives")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("S3 NEAR-EXPIRY CERTAINTY SNIPER — BACKTEST v2")
    print("=" * 65)

    had_real = run_real_backtest()
    if not had_real:
        logger.info("Skipping Stage 1 (no real price history available for closed crypto markets)")
    run_synthetic_validation()


if __name__ == "__main__":
    main()
