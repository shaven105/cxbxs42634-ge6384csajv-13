import json
import logging
import time
from typing import Any

import requests

from config import (
    GAMMA_API_BASE,
    MAX_MARKETS_PER_SCAN,
    MIN_VOLUME_24H,
    MIN_LIQUIDITY,
    MIN_BEST_BID,
    MAX_BEST_ASK,
)

logger = logging.getLogger(__name__)
Market = dict[str, Any]


def fetch_active_markets(limit: int = MAX_MARKETS_PER_SCAN) -> list[Market]:
    """
    Pages through Gamma API to collect up to `limit` active, non-closed markets.

    Uses `order=id&ascending=false` so the NEWEST markets (highest IDs) come first.
    This is critical: near-expiry BTC/ETH daily-threshold markets and 5-min
    crypto markets live at IDs 2,600,000+ while the default oldest-first sort
    returns only legacy markets from 2021-2022 (IDs 540k-700k), completely
    missing all short-term opportunities.
    """
    markets: list[Market] = []
    page_size = 100
    offset = 0

    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-bot/1.0"

    while len(markets) < limit:
        params = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
            "order": "id",
            "ascending": "false",
        }
        try:
            resp = session.get(
                f"{GAMMA_API_BASE}/markets",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            batch: list[Market] = resp.json()
        except requests.RequestException as exc:
            logger.error(f"Gamma API error (offset={offset}): {exc}")
            break

        if not batch:
            break

        markets.extend(batch)
        logger.debug(f"Fetched {len(batch)} markets at offset={offset}")
        offset += page_size

        if len(batch) < page_size:
            break  # last page

        time.sleep(0.2)

    logger.info(f"Raw markets fetched: {len(markets)}")
    return markets[:limit]


def filter_markets(markets: list[Market]) -> list[Market]:
    """
    Retain only binary, order-book-enabled, liquid markets with a valid spread.
    Attaches parsed convenience fields prefixed with _ for downstream modules.
    """
    filtered = []

    for m in markets:
        if not m.get("enableOrderBook"):
            continue
        if not m.get("acceptingOrders"):
            continue

        volume_24h = float(m.get("volume24hr") or 0)
        liquidity = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        if volume_24h < MIN_VOLUME_24H or liquidity < MIN_LIQUIDITY:
            continue

        # clobTokenIds is a JSON string in the Gamma response
        raw_ids = m.get("clobTokenIds")
        if not raw_ids:
            continue
        try:
            token_ids: list[str] = (
                json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            )
        except (json.JSONDecodeError, TypeError):
            continue
        if len(token_ids) != 2:
            continue  # must be binary YES/NO

        best_bid = float(m.get("bestBid") or 0)
        best_ask = float(m.get("bestAsk") or 0)

        if best_bid <= MIN_BEST_BID or best_ask >= MAX_BEST_ASK:
            continue
        if best_bid >= best_ask:
            continue  # crossed or invalid book

        if m.get("negRisk"):
            continue  # negRisk bundle markets have complex resolution

        # Parse outcomes / outcomePrices safely
        try:
            outcomes_raw = m.get("outcomes")
            outcomes: list[str] = (
                json.loads(outcomes_raw)
                if isinstance(outcomes_raw, str)
                else (outcomes_raw or ["Yes", "No"])
            )
            prices_raw = m.get("outcomePrices")
            outcome_prices: list[float] = (
                [float(p) for p in json.loads(prices_raw)]
                if isinstance(prices_raw, str)
                else [float(p) for p in (prices_raw or [])]
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            outcomes = ["Yes", "No"]
            outcome_prices = []

        m["_token_ids"] = token_ids
        m["_outcomes"] = outcomes
        m["_outcome_prices"] = outcome_prices
        m["_best_bid"] = best_bid
        m["_best_ask"] = best_ask
        m["_mid_price"] = (best_bid + best_ask) / 2.0

        filtered.append(m)

    logger.info(
        f"Filtered: {len(filtered)}/{len(markets)} markets "
        f"(binary + liquid + valid spread)"
    )
    return filtered


def get_tradeable_markets() -> list[Market]:
    return filter_markets(fetch_active_markets())
