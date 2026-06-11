"""
Simulates Polymarket whale wallets for copy-trading strategy.

Model:
- 200 wallets trade across the universe; most are noise traders
- Top 10 "sharp" wallets have genuine edge (better true_prob estimates)
- Copy-trade latency degrades our edge: by the time we see their order,
  price has moved ~30-50% of the way toward fair value
- Sharp wallets specialise in 1-2 categories (niche expertise)
"""

import random
import math
from dataclasses import dataclass
from backtest.market_generator import SimMarket


@dataclass
class WalletProfile:
    address: str
    specialist_categories: list[str]
    edge_multiplier: float   # how much better than random (1.0 = no edge)
    win_rate_30d: float
    trade_count_30d: int
    roi_30d: float


def _sharp_estimate(market: SimMarket, edge_mult: float) -> float:
    """
    Sharp wallet's internal probability estimate.
    Better than market, worse than true (edge_mult in 0..1 toward true).
    """
    return market.price + edge_mult * (market.true_prob - market.price)


def generate_wallet_universe(n_wallets: int = 200, seed: int = 99) -> list[WalletProfile]:
    random.seed(seed)
    wallets = []

    for i in range(n_wallets):
        is_sharp = i < 10  # top 10 are sharp
        cats = random.sample(
            ["politics", "crypto", "sports", "economics",
             "science", "entertainment", "world", "weather"],
            k=2 if is_sharp else random.randint(1, 8)
        )
        edge_mult = random.uniform(0.55, 0.80) if is_sharp else random.uniform(0.0, 0.25)
        win_rate = 0.52 + edge_mult * 0.20 if is_sharp else random.uniform(0.42, 0.55)
        trade_count = random.randint(30, 200) if is_sharp else random.randint(2, 40)
        roi = (win_rate - 0.5) * 2 * random.uniform(0.8, 1.2)

        wallets.append(WalletProfile(
            address=f"0x{''.join(random.choices('0123456789abcdef', k=40))}",
            specialist_categories=cats,
            edge_multiplier=edge_mult,
            win_rate_30d=round(win_rate, 3),
            trade_count_30d=trade_count,
            roi_30d=round(roi, 3),
        ))

    return wallets


def get_top_wallets(wallets: list[WalletProfile], n: int = 10) -> list[WalletProfile]:
    """Sort by ROI × trade_count (filter out lucky-few-trade wallets)."""
    scored = sorted(
        [w for w in wallets if w.trade_count_30d >= 20],
        key=lambda w: w.roi_30d * math.log(w.trade_count_30d + 1),
        reverse=True,
    )
    return scored[:n]


def copy_trade_edge(
    market: SimMarket,
    top_wallets: list[WalletProfile],
    latency_decay: float = 0.40,  # price moves 40% toward fair before we copy
) -> tuple[bool, float]:
    """
    Returns (should_trade, our_effective_edge_after_latency).
    We only copy if at least 2 top wallets with matching category agree.
    """
    agreeing = [
        w for w in top_wallets
        if market.category in w.specialist_categories
    ]
    if len(agreeing) < 2:
        return False, 0.0

    # Consensus estimate weighted by edge_multiplier
    total_weight = sum(w.edge_multiplier for w in agreeing)
    consensus_prob = sum(
        w.edge_multiplier * _sharp_estimate(market, w.edge_multiplier)
        for w in agreeing
    ) / total_weight

    # After latency, price has already moved toward consensus
    effective_price = market.price + latency_decay * (consensus_prob - market.price)
    our_edge = consensus_prob - effective_price

    return our_edge >= 0.05, our_edge
