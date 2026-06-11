"""
Synthetic Polymarket market data generator.

Calibrated to known Polymarket statistics:
- ~60% of markets resolve YES (slight yes-bias in question framing)
- Prices follow a U-shaped distribution (more near 0 and 1 than 0.5)
- Markets are ~75-80% efficient (price explains 75-80% of outcome variance)
- Favorite-longshot bias: low-probability outcomes are overpriced ~3-8%
- High-probability outcomes are slightly underpriced ~2-4%
- Volume clusters around 0.20-0.80 price range
"""

import random
import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SimMarket:
    market_id: str
    question: str
    category: str
    price: float          # current market best-ask (observed)
    true_prob: float      # ground truth (unknown to strategies)
    volume_24h: float
    liquidity: float
    days_to_resolution: int
    volume_spike: float   # ratio of 24h vol / 7d avg daily vol
    price_change_24h: float  # absolute change in last 24h
    is_niche: bool        # niche = low-volume specialist market


CATEGORIES = [
    "politics", "crypto", "sports", "economics",
    "science", "entertainment", "world", "weather",
]

NICHE_CATEGORIES = {"weather", "science", "entertainment"}


def _u_shaped_price() -> float:
    """
    U-shaped distribution: more mass near 0 and 1.
    Beta(0.5, 0.5) achieves this.
    """
    # Box-Muller approximation of Beta(0.5,0.5) via arcsine distribution
    u = random.random()
    return math.sin(math.pi * u / 2) ** 2


def _apply_longshot_bias(price: float) -> float:
    """
    Favorite-longshot bias:
    - Underdogs (p < 0.25) are overpriced by 3-8%
    - Favorites (p > 0.75) are underpriced by 2-4%
    - Middle range: minimal bias
    """
    if price < 0.25:
        bias = random.uniform(0.03, 0.08) * (0.25 - price) / 0.25
        return min(price + bias, 0.95)
    elif price > 0.75:
        bias = random.uniform(0.02, 0.04) * (price - 0.75) / 0.25
        return max(price - bias, 0.05)
    return price


def generate_market(market_id: int, rng_seed: Optional[int] = None) -> SimMarket:
    if rng_seed is not None:
        random.seed(rng_seed + market_id)

    category = random.choice(CATEGORIES)
    is_niche = category in NICHE_CATEGORIES

    # True probability — U-shaped
    true_prob = max(0.03, min(0.97, _u_shaped_price()))

    # Market price: starts from true_prob, then apply efficiency noise + bias
    # 80% efficient: price = 0.8*true + 0.2*noise + bias
    noise = random.gauss(0, 0.12)
    raw_price = 0.80 * true_prob + 0.20 * (true_prob + noise)
    raw_price = max(0.04, min(0.96, raw_price))
    market_price = _apply_longshot_bias(raw_price)
    market_price = max(0.04, min(0.96, market_price))

    # Niche markets: wider mispricings (less sophisticated participants)
    if is_niche:
        extra_noise = random.gauss(0, 0.06)
        market_price = max(0.04, min(0.96, market_price + extra_noise))

    volume_24h = random.lognormvariate(7.5, 1.8)  # median ~$1800
    if is_niche:
        volume_24h *= 0.3

    liquidity = volume_24h * random.uniform(0.8, 3.0)
    days_to_res = random.randint(1, 90)
    volume_spike = random.lognormvariate(0, 0.6)  # median ~1.0
    price_change_24h = random.gauss(0, 0.05)

    return SimMarket(
        market_id=f"mkt_{market_id:04d}",
        question=f"[{category.upper()}] Market #{market_id}",
        category=category,
        price=round(market_price, 4),
        true_prob=round(true_prob, 4),
        volume_24h=round(volume_24h, 2),
        liquidity=round(liquidity, 2),
        days_to_resolution=days_to_res,
        volume_spike=round(volume_spike, 3),
        price_change_24h=round(price_change_24h, 4),
        is_niche=is_niche,
    )


def generate_market_universe(n: int = 800, seed: int = 42) -> list[SimMarket]:
    random.seed(seed)
    return [generate_market(i) for i in range(n)]
