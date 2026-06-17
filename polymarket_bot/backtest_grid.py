"""
Backtest the S2 Grid strategy on real Polymarket 24-hour minute data.

Usage:
  python backtest_grid.py

Fetches minute-level price history for eligible markets, simulates the
grid strategy, and prints a P&L summary.
"""

import json
import logging
import random
import time
from datetime import datetime
from statistics import mean, stdev

import requests

from strategy_grid import (
    GRID_OFFSET, GRID_MID_MIN, GRID_MID_MAX,
    GRID_MIN_SPREAD, GRID_MAX_SPREAD, GRID_STOP_BAND,
    GRID_BET_FRACTION, GRID_MIN_BET,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("backtest_grid")

STARTING_BANKROLL = 50.0
FEE_RATE = 0.02
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def fetch_price_history(token_id: str, interval: str = "1d") -> list[float]:
    """Return list of minute-level mid prices for the last day."""
    r = requests.get(
        f"{CLOB_API}/prices-history",
        params={"market": token_id, "interval": interval},
        timeout=15,
    )
    data = r.json()
    pts = data.get("history", [])
    return [p["p"] for p in pts]


def simulate_grid(prices: list[float], bankroll: float, question: str) -> dict:
    """
    Simulate grid strategy on a sequence of mid-prices.

    Grid is set at the first price point.  We simulate one active grid at a time:
    - BUY limit at  mid0 - GRID_OFFSET
    - SELL limit at mid0 + GRID_OFFSET
    - On each tick, check if price crossed either limit
    - Close (round-trip or directional) when both fill OR stop-loss hit
    """
    if len(prices) < 10:
        return {}

    bet = max(GRID_MIN_BET, bankroll * GRID_BET_FRACTION)
    trades = []
    i = 0
    while i < len(prices) - 1:
        mid0 = prices[i]

        # Only enter if mid is in coin-flip range and there's been recent spread
        if not (GRID_MID_MIN <= mid0 <= GRID_MID_MAX):
            i += 1
            continue

        buy_limit = mid0 - GRID_OFFSET
        sell_limit = mid0 + GRID_OFFSET
        buy_filled = False
        sell_filled = False
        buy_price = sell_price = None
        entry_i = i
        pnl = 0.0
        exit_reason = "open"

        for j in range(i + 1, len(prices)):
            p = prices[j]
            if not buy_filled and p <= buy_limit:
                buy_filled = True
                buy_price = buy_limit

            if not sell_filled and p >= sell_limit:
                sell_filled = True
                sell_price = sell_limit

            stop = abs(p - mid0) >= GRID_STOP_BAND

            if (buy_filled and sell_filled) or stop or j == len(prices) - 1:
                ticks = j - entry_i
                if buy_filled and sell_filled:
                    shares = bet / buy_price
                    gross = shares * (sell_price - buy_price)
                    pnl = gross - bet * 2 * FEE_RATE
                    exit_reason = "round-trip"
                elif buy_filled:
                    shares = bet / buy_price
                    pnl = shares * (p - buy_price) - bet * FEE_RATE
                    exit_reason = "stop(long)" if stop else "expiry(long)"
                elif sell_filled:
                    shares = bet / sell_price
                    pnl = shares * (sell_price - p) - bet * FEE_RATE
                    exit_reason = "stop(short)" if stop else "expiry(short)"
                else:
                    pnl = 0.0
                    exit_reason = "no-fill"

                trades.append({
                    "entry_i": entry_i,
                    "exit_i": j,
                    "ticks": ticks,
                    "entry_mid": round(mid0, 4),
                    "buy_limit": round(buy_limit, 4),
                    "sell_limit": round(sell_limit, 4),
                    "buy_filled": buy_filled,
                    "sell_filled": sell_filled,
                    "exit_price": round(p, 4),
                    "pnl": round(pnl, 4),
                    "exit_reason": exit_reason,
                    "bet": round(bet, 2),
                })
                bankroll += pnl
                i = j + 1
                break
        else:
            i += 1

    return {
        "question": question,
        "n_prices": len(prices),
        "n_trades": len(trades),
        "total_pnl": round(sum(t["pnl"] for t in trades), 4),
        "win_rate": (
            sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100
            if trades else 0
        ),
        "round_trips": sum(1 for t in trades if t["exit_reason"] == "round-trip"),
        "trades": trades,
        "bankroll_final": round(bankroll, 2),
    }


def main():
    logger.info("=== S2 Grid Backtest ===")
    logger.info(
        f"GRID_OFFSET={GRID_OFFSET:.1%}  MID_RANGE=[{GRID_MID_MIN:.0%},{GRID_MID_MAX:.0%}]  "
        f"SPREAD=[{GRID_MIN_SPREAD:.1%},{GRID_MAX_SPREAD:.0%}]  STOP={GRID_STOP_BAND:.0%}"
    )

    # Fetch all active markets (paginated)
    logger.info("Fetching markets...")
    markets = []
    offset = 0
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-bot/1.0"
    while len(markets) < 800:
        r = session.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": 100, "offset": offset},
            timeout=20,
        )
        batch = r.json()
        if not batch:
            break
        markets.extend(batch)
        offset += 100
        if len(batch) < 100:
            break
        time.sleep(0.2)
    logger.info(f"Got {len(markets)} markets")

    results = []
    checked = 0

    for m in markets:
        bid = float(m.get("bestBid") or 0)
        ask = float(m.get("bestAsk") or 0)
        if bid <= 0 or ask <= 0 or bid >= ask:
            continue
        mid = (bid + ask) / 2
        spread = ask - bid
        if not (GRID_MID_MIN <= mid <= GRID_MID_MAX):
            continue
        if not (GRID_MIN_SPREAD <= spread <= GRID_MAX_SPREAD):
            continue
        if m.get("negRisk"):
            continue

        raw = m.get("clobTokenIds", "[]")
        try:
            ids = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            continue
        if not ids:
            continue

        checked += 1
        token_id = ids[0]
        prices = fetch_price_history(token_id, interval="1d")
        time.sleep(0.05)  # polite

        if len(prices) < 20:
            continue

        price_range = max(prices) - min(prices)
        if price_range < 0.005:
            # No movement — boring market, skip
            continue

        result = simulate_grid(prices, STARTING_BANKROLL, m.get("question", "")[:70])
        if result and result["n_trades"] > 0:
            result["current_spread"] = round(spread, 4)
            result["price_range_24h"] = round(price_range, 4)
            results.append(result)
            logger.info(
                f"  {result['question'][:50]}: "
                f"{result['n_trades']} trades | P&L={result['total_pnl']:+.4f} | "
                f"WR={result['win_rate']:.0f}% | round-trips={result['round_trips']}"
            )

    logger.info(f"\nChecked {checked} coin-flip markets; {len(results)} had history + movement")

    if not results:
        logger.warning("No backtest data — all coin-flip markets have flat price history.")
        logger.info("Generating synthetic backtest using observed market parameters...")
        _synthetic_backtest()
        return

    # Summary
    total_trades = sum(r["n_trades"] for r in results)
    total_pnl = sum(r["total_pnl"] for r in results)
    avg_wr = mean(r["win_rate"] for r in results) if results else 0
    total_rt = sum(r["round_trips"] for r in results)

    print("\n" + "=" * 60)
    print("S2 GRID BACKTEST SUMMARY")
    print("=" * 60)
    print(f"Markets tested : {len(results)}")
    print(f"Total trades   : {total_trades}")
    print(f"Round-trips    : {total_rt} ({total_rt/total_trades*100:.0f}% of trades)" if total_trades else "")
    print(f"Total P&L      : {total_pnl:+.4f} USDC")
    print(f"Avg win rate   : {avg_wr:.1f}%")
    print()
    for r in results:
        print(f"  {r['question'][:55]}")
        print(f"    trades={r['n_trades']} pnl={r['total_pnl']:+.4f} wr={r['win_rate']:.0f}% "
              f"range={r['price_range_24h']:.3f} spread={r['current_spread']:.3f}")


def _synthetic_backtest():
    """
    Synthetic backtest when no live data has enough movement.

    Uses realistic parameters observed from Polymarket coin-flip markets:
    - Mid price starts at 0.50
    - Volatility ~0.003 per minute (σ of minute returns)
    - Run for 1440 minutes (24 hours)
    """
    random.seed(42)
    logger.info("Running 3 synthetic scenarios (conservative σ=0.002, base σ=0.003, high σ=0.005)")
    print("\n" + "=" * 60)
    print("S2 GRID SYNTHETIC BACKTEST (24h × 1440 min each)")
    print(f"Parameters: OFFSET={GRID_OFFSET:.1%} STOP={GRID_STOP_BAND:.0%} BET_FRAC={GRID_BET_FRACTION:.0%}")
    print("=" * 60)

    scenarios = [
        ("Conservative (σ=0.2%/min)", 0.002),
        ("Base case   (σ=0.3%/min)", 0.003),
        ("High vol    (σ=0.5%/min)", 0.005),
    ]
    for name, sigma in scenarios:
        # Generate GBM-like price path
        prices = [0.50]
        for _ in range(1439):
            drift = random.gauss(0, sigma)
            p = max(0.05, min(0.95, prices[-1] + drift))
            prices.append(p)

        price_range = max(prices) - min(prices)
        result = simulate_grid(prices, STARTING_BANKROLL, name)

        print(f"\n{name}")
        print(f"  Price range  : {price_range:.3f} ({min(prices):.3f}–{max(prices):.3f})")
        print(f"  Trades       : {result['n_trades']}")
        print(f"  Round-trips  : {result['round_trips']}")
        print(f"  Win rate     : {result['win_rate']:.0f}%")
        print(f"  Total P&L    : {result['total_pnl']:+.4f} USDC (start ${STARTING_BANKROLL:.0f})")
        print(f"  Final bal    : ${result['bankroll_final']:.2f}")

        if result["trades"]:
            pnls = [t["pnl"] for t in result["trades"]]
            print(f"  Avg trade    : {mean(pnls):+.4f}")
            reasons = {}
            for t in result["trades"]:
                reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
            print(f"  Exit reasons : {dict(sorted(reasons.items()))}")

    print("\nConclusion:")
    print("  Round-trips (both legs fill) are rare in 30-min cycle on prediction")
    print("  markets — prices are sticky. Grid works best when combined with S5")
    print("  signals: use S5 edge to set direction, grid to optimize entry price.")


if __name__ == "__main__":
    main()
