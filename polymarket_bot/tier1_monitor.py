"""
Tier-1 lightweight monitor: polls Gamma API every 10 minutes, zero Claude cost.
Detects price changes >5% or new markets since last scan.
Returns only the markets worth passing to Tier-2 Claude evaluation.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from config import GAMMA_API_BASE, MAX_MARKETS_PER_SCAN

logger = logging.getLogger(__name__)
Market = dict[str, Any]

CACHE_FILE = Path("price_cache.json")
PRICE_CHANGE_THRESHOLD = 0.05   # 5% move triggers Claude evaluation


def _load_cache() -> dict[str, float]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, float]) -> None:
    CACHE_FILE.write_text(json.dumps(cache))


def fetch_price_snapshot(limit: int = MAX_MARKETS_PER_SCAN) -> list[Market]:
    """Fetch active markets — only id, bestBid, bestAsk, category fields needed."""
    markets: list[Market] = []
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-bot/1.0"
    page_size, offset = 100, 0

    while len(markets) < limit:
        try:
            resp = session.get(
                f"{GAMMA_API_BASE}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": page_size,
                    "offset": offset,
                    "fields": "id,question,bestBid,bestAsk,volume24hr,liquidity,"
                              "clobTokenIds,outcomePrices,outcomes,endDateIso,"
                              "acceptingOrders,enableOrderBook,negRisk,"
                              "liquidityNum,resolutionSource,description",
                },
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as exc:
            logger.error(f"Tier-1 Gamma fetch error: {exc}")
            break

        if not batch:
            break
        markets.extend(batch)
        offset += page_size
        if len(batch) < page_size:
            break
        time.sleep(0.2)

    return markets[:limit]


def detect_changed_markets(markets: list[Market]) -> list[Market]:
    """
    Compare current mid-prices against the cached snapshot.
    Returns markets that are new or moved > PRICE_CHANGE_THRESHOLD.
    Updates the cache.
    """
    cache = _load_cache()
    changed: list[Market] = []
    new_cache: dict[str, float] = {}

    for m in markets:
        mid = m.get("mid_price")
        if mid is None:
            bid = float(m.get("bestBid") or 0)
            ask = float(m.get("bestAsk") or 0)
            if bid <= 0 or ask <= 0 or bid >= ask:
                continue
            mid = (bid + ask) / 2.0

        market_id = m.get("id", "")
        new_cache[market_id] = mid

        prev = cache.get(market_id)
        if prev is None:
            # New market — always evaluate
            changed.append(m)
        elif abs(mid - prev) >= PRICE_CHANGE_THRESHOLD:
            # Price moved enough to warrant Claude evaluation
            changed.append(m)

    _save_cache(new_cache)
    logger.info(
        f"Tier-1: {len(markets)} markets scanned, "
        f"{len(changed)} changed/new → queued for Claude"
    )
    return changed
