"""
Backtest: Polymarket 5-minute BTC Up/Down markets

Strategy: At T-120s before close, buy the stronger side if its ASK > THRESHOLD.
Ask = trade price + SPREAD (approximation).
Signal asks: does the WR justify the entry price vs. breakeven (ask × 1.018)?

Data access:
  - Markets discovered via Gamma API by ID scan (slug lookup only works for active markets).
  - Price history via CLOB /prices-history?market={token_id}&startTs=...&endTs=...&fidelity=1
  - Outcome from Gamma outcomePrices field.
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

SIGNAL_SECS    = 120    # seconds before close to check price
THRESHOLD      = 0.70   # minimum ask to count as a signal
SPREAD         = 0.03   # approximate bid-ask spread half-width
SCAN_STEP      = 3      # check every Nth market ID when scanning
SCAN_RANGE     = 9000   # how many IDs below start_id to search
TARGET_MARKETS = 200    # stop after collecting this many resolved markets

# ── HTTP helper ────────────────────────────────────────────────────────────────

def get(url: str) -> object:
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "btc5m-backtest/2.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == 2:
                return None
            time.sleep(0.5 * (attempt + 1))


# ── Step 1: find the most recent closed BTC 5-min market ID ───────────────────

def find_latest_closed_btc5m_id() -> int | None:
    """
    Probe the current 5-min window and nearby IDs to find the most recently
    closed BTC 5-min market. Returns the Gamma market ID (integer) or None.
    """
    now_ts  = int(time.time())
    cur_5m  = (now_ts // 300) * 300          # current interval start
    prev_5m = cur_5m - 300                   # previous (just closed)

    # Try slugs for the last several intervals
    for i in range(1, 8):
        ts   = cur_5m - i * 300
        slug = f"btc-updown-5m-{ts}"
        data = get(f"{GAMMA}/markets?slug={slug}")
        if data and isinstance(data, list) and data:
            m = data[0]
            if m.get("closed") and m.get("id"):
                print(f"  Anchor via slug: id={m['id']}  {m.get('question','')[:55]}")
                return int(m["id"])
        time.sleep(0.1)

    # Fallback: brute-force probe the 100 IDs just above the expected range
    # (We know the current active market has a higher ID than the last closed one.)
    # Find the active market's ID via a known-working slug.
    ts_active = cur_5m
    slug_active = f"btc-updown-5m-{ts_active}"
    data = get(f"{GAMMA}/markets?slug={slug_active}")
    if data and isinstance(data, list) and data:
        active_id = int(data[0]["id"])
        # Scan downward to find the last closed one
        for mid in range(active_id - 1, active_id - 200, -1):
            d = get(f"{GAMMA}/markets/{mid}")
            if d and isinstance(d, dict) and "btc-updown-5m-" in d.get("slug", "") and d.get("closed"):
                print(f"  Anchor via ID scan: id={mid}  {d.get('question','')[:55]}")
                return mid
            time.sleep(0.05)
    return None


# ── Step 2: scan backward from anchor to collect closed markets ───────────────

def fetch_btc5m_markets(start_id: int, target: int) -> list[dict]:
    """
    Scan market IDs from start_id downward, collecting closed btc-updown-5m markets.
    Uses step SCAN_STEP over a window of SCAN_RANGE IDs.
    """
    markets = []
    stop_id = max(1, start_id - SCAN_RANGE)
    total_checked = 0

    print(f"Scanning IDs {start_id} → {stop_id} (step={SCAN_STEP}) for closed BTC 5-min markets…")

    for mid in range(start_id, stop_id, -SCAN_STEP):
        total_checked += 1
        data = get(f"{GAMMA}/markets/{mid}")
        if data and isinstance(data, dict):
            slug = data.get("slug", "")
            if "btc-updown-5m-" in slug and data.get("closed"):
                # Verify resolved
                op_raw = data.get("outcomePrices")
                try:
                    op = [float(p) for p in (
                        json.loads(op_raw) if isinstance(op_raw, str) else (op_raw or [])
                    )]
                except Exception:
                    op = []
                if len(op) >= 2 and (op[0] >= 0.99 or op[1] >= 0.99):
                    markets.append(data)
                    if len(markets) % 20 == 1 or len(markets) <= 5:
                        result = "UP" if op[0] >= 0.99 else "DOWN"
                        print(f"  [{len(markets):3d}] id={mid}  {data.get('question','?')[:52]} → {result}")

        if len(markets) >= target:
            break
        time.sleep(0.04)

    print(f"\nScanned {total_checked} IDs → found {len(markets)} resolved BTC 5-min markets")
    return markets


# ── Step 3: CLOB price history for a 5-min window ─────────────────────────────

def fetch_window_prices(token_id: str, end_ts: int) -> list[dict]:
    """
    Fetch per-minute price points for the 5-min window ending at end_ts.
    Returns [{t, p}, …] sorted ascending, or [].
    IMPORTANT: use 'market=' not 'market_id=' in the CLOB request.
    """
    start_ts = end_ts - 300
    url = (
        f"{CLOB}/prices-history"
        f"?market={token_id}&startTs={start_ts}&endTs={end_ts + 30}&fidelity=1"
    )
    data = get(url)
    if not data or not isinstance(data, dict):
        return []
    history = data.get("history", [])
    history.sort(key=lambda x: x.get("t", 0))
    return history


# ── Step 4: simulate T-120s signal ───────────────────────────────────────────

def simulate(markets: list[dict]) -> list[dict]:
    records = []
    n = len(markets)

    for i, m in enumerate(markets, 1):
        question = (m.get("question") or "")[:60]

        # Parse end timestamp
        end_ts = None
        for key in ("endDate", "endDateIso"):
            raw = m.get(key)
            if not raw:
                continue
            try:
                s = str(raw).strip().rstrip("Z")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                end_ts = int(dt.timestamp())
                break
            except Exception:
                pass
        if end_ts is None:
            continue

        # Resolution outcome
        op_raw = m.get("outcomePrices")
        try:
            op = [float(p) for p in (
                json.loads(op_raw) if isinstance(op_raw, str) else (op_raw or [])
            )]
        except Exception:
            continue
        if len(op) < 2:
            continue
        yes_won = op[0] >= 0.99   # UP token won

        # YES token ID (token 0 = UP)
        tok_raw = m.get("clobTokenIds")
        try:
            toks = json.loads(tok_raw) if isinstance(tok_raw, str) else (tok_raw or [])
        except Exception:
            continue
        if not toks:
            continue
        yes_token = str(toks[0])

        # Fetch price history for the last 5 minutes of the market
        history = fetch_window_prices(yes_token, end_ts)
        time.sleep(0.12)

        if len(history) < 1:
            continue

        # Snapshot: last data point at or before T - SIGNAL_SECS
        target = end_ts - SIGNAL_SECS
        snap   = None
        for pt in history:
            if pt.get("t", 0) <= target:
                snap = pt

        # Fallback: use earliest available point
        if snap is None and history:
            snap = history[0]

        if snap is None:
            continue

        yes_mid  = float(snap.get("p", 0))
        yes_ask  = min(yes_mid + SPREAD, 0.98)
        no_ask   = min((1.0 - yes_mid) + SPREAD, 0.98)
        secs_before = end_ts - snap.get("t", end_ts)

        stronger   = "UP" if yes_ask >= no_ask else "DOWN"
        entry_ask  = yes_ask if stronger == "UP" else no_ask
        won        = (stronger == "UP" and yes_won) or (stronger == "DOWN" and not yes_won)

        if i % 50 == 0:
            print(
                f"  [{i}/{n}] UP_ask={yes_ask:.3f} DN_ask={no_ask:.3f} "
                f"→ {stronger}@{entry_ask:.3f} {'✓' if won else '✗'}  ({secs_before:.0f}s before close)"
            )

        records.append({
            "question":    question,
            "market_id":   m.get("id"),
            "end_ts":      end_ts,
            "secs_before": secs_before,
            "yes_mid":     round(yes_mid, 4),
            "yes_ask":     round(yes_ask, 4),
            "no_ask":      round(no_ask, 4),
            "side":        stronger,
            "entry_ask":   round(entry_ask, 4),
            "won":         won,
            "yes_won":     yes_won,
        })

    return records


# ── Step 5: analysis ──────────────────────────────────────────────────────────

def analyse(records: list[dict]) -> None:
    if not records:
        print("\nNo records to analyse.")
        return

    n = len(records)
    print(f"\n{'='*66}")
    print(f"BACKTEST RESULTS — {n} resolved BTC 5-min markets")
    print(f"{'='*66}")

    # Snapshot quality
    good_snaps = [r for r in records if 60 <= r["secs_before"] <= 300]
    print(f"Snapshots within [60s, 300s] before close: {len(good_snaps)}/{n} ({len(good_snaps)/n:.0%})")

    # Base rate — always bet the stronger side regardless of price
    overall_wr = sum(r["won"] for r in records) / n
    print(f"\nBase rate (always bet stronger side, n={n}): {overall_wr:.1%}")

    # Signal filter by ask threshold
    print(f"\n--- Win rate by ask threshold (all {n} records) ---")
    for thresh in [0.55, 0.60, 0.65, 0.70, 0.72, 0.75, 0.80, 0.85]:
        subset = [r for r in records if r["entry_ask"] >= thresh]
        if len(subset) < 3:
            continue
        wr  = sum(r["won"] for r in subset) / len(subset)
        avg = sum(r["entry_ask"] for r in subset) / len(subset)
        be  = avg * 1.018
        ev  = wr - be
        bar = "▓" * int(wr * 30)
        flag = "✅" if ev > 0 else "❌"
        print(
            f"  ask≥{thresh:.2f}  n={len(subset):4d}  WR={wr:.1%}  avg_ask={avg:.3f}  "
            f"breakeven={be:.1%}  EV={ev:+.3f}  {flag} {bar}"
        )

    # Distribution of entry ask
    print(f"\n--- Distribution of entry_ask ---")
    for lo, hi in [
        (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
        (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 1.01),
    ]:
        sub = [r for r in records if lo <= r["entry_ask"] < hi]
        if not sub:
            continue
        wr  = sum(r["won"] for r in sub) / len(sub)
        bar = "█" * min(len(sub), 30) + (f"…{len(sub)}" if len(sub) > 30 else "")
        print(f"  [{lo:.2f}–{hi:.2f}] n={len(sub):3d}  WR={wr:.1%}  {bar}")

    # Tight-snapshot subset (best data quality)
    if good_snaps and len(good_snaps) >= 5:
        print(f"\n--- Tight snapshots only (60–300s before close, n={len(good_snaps)}) ---")
        base_wr = sum(r["won"] for r in good_snaps) / len(good_snaps)
        print(f"Base rate (stronger side): {base_wr:.1%}")
        for thresh in [0.60, 0.65, 0.70, 0.75, 0.80]:
            sub = [r for r in good_snaps if r["entry_ask"] >= thresh]
            if len(sub) < 3:
                continue
            wr  = sum(r["won"] for r in sub) / len(sub)
            avg = sum(r["entry_ask"] for r in sub) / len(sub)
            be  = avg * 1.018
            ev  = wr - be
            flag = "✅" if ev > 0 else "❌"
            print(f"  ask≥{thresh:.2f}  n={len(sub):3d}  WR={wr:.1%}  avg_ask={avg:.3f}  EV={ev:+.3f}  {flag}")

    print(f"\n{'='*66}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Step 1: Finding anchor market ID…")
    anchor_id = find_latest_closed_btc5m_id()
    if anchor_id is None:
        print("Could not find anchor market. Exiting.")
        sys.exit(1)
    print(f"Anchor market ID: {anchor_id}")

    print(f"\nStep 2: Scanning backward for up to {TARGET_MARKETS} closed markets…")
    markets = fetch_btc5m_markets(anchor_id, TARGET_MARKETS)

    if not markets:
        print("No markets found. Exiting.")
        sys.exit(1)

    print(f"\nStep 3: Fetching CLOB price history for {len(markets)} markets…")
    records = simulate(markets)

    print(f"\nStep 4: Analysing {len(records)} records…")
    analyse(records)

    out_path = "/home/user/test1/btc5m_backtest_results.json"
    with open(out_path, "w") as f:
        json.dump(records, f, indent=2)
    print(f"\nResults saved to {out_path}")
