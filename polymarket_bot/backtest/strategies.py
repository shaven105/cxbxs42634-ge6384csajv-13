"""
12 market-selection strategies for Polymarket.

Each strategy is a class with:
  filter(markets) -> list[SimMarket]   — which markets to consider
  fair_prob(market) -> float           — probability estimate (our signal)
  bet_fraction(fair, price) -> float   — Kelly fraction to wager

Simulation assumes:
  - Claude fair_prob = true_prob + N(0, 0.08)   (Claude has ~8% std error)
  - Transaction cost: 2% of bet (Polymarket fee)
  - Min edge enforced per strategy
"""

import random
import math
from dataclasses import dataclass
from typing import Optional
from backtest.market_generator import SimMarket
from backtest.wallet_simulator import (
    WalletProfile, copy_trade_edge,
)

POLYMARKET_FEE = 0.02   # 2% taker fee on CLOB


def _kelly(fair: float, price: float, fraction: float = 0.5, cap: float = 0.06) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    b = (1 - price) / price
    q = 1 - fair
    fk = (b * fair - q) / b
    if fk <= 0:
        return 0.0
    return min(fk * fraction, cap)


def _claude_estimate(market: SimMarket, noise_std: float = 0.08) -> float:
    """Simulate Claude's probability estimate."""
    est = market.true_prob + random.gauss(0, noise_std)
    return max(0.02, min(0.98, est))


# ─────────────────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class BaseStrategy:
    name: str = "base"
    description: str = ""
    min_edge: float = 0.08

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        return [
            m for m in markets
            if m.volume_24h >= 500 and m.liquidity >= 1000
            and 0.04 < m.price < 0.96
        ]

    def fair_prob(self, market: SimMarket) -> float:
        return _claude_estimate(market)

    def bet_fraction(self, fair: float, price: float) -> float:
        return _kelly(fair, price, fraction=0.5, cap=0.06)

    def get_edge(self, fair: float, price: float) -> float:
        return fair - price


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 1: Current baseline (8% edge, half-Kelly, all markets)
# ─────────────────────────────────────────────────────────────────────────────

class S1_Baseline(BaseStrategy):
    name = "S1_Baseline"
    description = "Current: 8% edge threshold, half-Kelly ≤6%, all liquid markets"
    min_edge = 0.08


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 2: Copy-wallet following
# ─────────────────────────────────────────────────────────────────────────────

class S2_CopyWallet(BaseStrategy):
    name = "S2_CopyWallet"
    description = "Follow top-10 high-ROI specialist wallets; min 2 agreeing wallets per trade"
    min_edge = 0.05

    def __init__(self, top_wallets: list[WalletProfile]):
        self._wallets = top_wallets
        self._cache: dict[str, tuple[bool, float]] = {}

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        base = super().filter(markets)
        result = []
        for m in base:
            should, edge = copy_trade_edge(m, self._wallets)
            if should:
                self._cache[m.market_id] = (should, edge)
                result.append(m)
        return result

    def fair_prob(self, market: SimMarket) -> float:
        _, edge = self._cache.get(market.market_id, (False, 0.0))
        # Our effective price is market.price + 40% latency decay toward true
        # Consensus prob embedded in edge: consensus = market.price + edge / (1 - 0.4)
        consensus = market.price + edge / 0.60
        return max(0.02, min(0.98, consensus))

    def bet_fraction(self, fair: float, price: float) -> float:
        return _kelly(fair, price, fraction=0.5, cap=0.05)  # slightly smaller cap

    def get_edge(self, fair: float, price: float) -> float:
        return fair - price


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 3: Resolution proximity (close-to-expiry only, ≤7 days)
# ─────────────────────────────────────────────────────────────────────────────

class S3_NearResolution(BaseStrategy):
    name = "S3_NearResolution"
    description = "Only markets resolving within 7 days — price converges fast, reducing risk window"
    min_edge = 0.08

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        base = super().filter(markets)
        return [m for m in base if m.days_to_resolution <= 7]

    def fair_prob(self, market: SimMarket) -> float:
        # Near resolution: Claude has less uncertainty → tighter estimate
        return _claude_estimate(market, noise_std=0.05)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 4: High-liquidity only ($50k+)
# ─────────────────────────────────────────────────────────────────────────────

class S4_DeepLiquidity(BaseStrategy):
    name = "S4_DeepLiquidity"
    description = "Only markets with $50k+ liquidity — tighter spreads, easier fills"
    min_edge = 0.08

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        return [
            m for m in markets
            if m.liquidity >= 50_000 and m.volume_24h >= 5_000
            and 0.04 < m.price < 0.96
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 5: Niche/specialist markets only
# ─────────────────────────────────────────────────────────────────────────────

class S5_NicheSpecialist(BaseStrategy):
    name = "S5_NicheSpecialist"
    description = "Only niche categories (weather, science, entertainment) — wider mispricings, fewer sharp traders"
    min_edge = 0.07

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        base = super().filter(markets)
        return [m for m in base if m.is_niche]

    def fair_prob(self, market: SimMarket) -> float:
        # Niche markets: less efficient, Claude has good relative edge
        return _claude_estimate(market, noise_std=0.07)

    def bet_fraction(self, fair: float, price: float) -> float:
        return _kelly(fair, price, fraction=0.5, cap=0.06)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 6: Contrarian extreme (fade markets near certainty)
# ─────────────────────────────────────────────────────────────────────────────

class S6_ContrarianExtreme(BaseStrategy):
    name = "S6_ContrarianExtreme"
    description = "Exploit longshot bias: buy YES when price <15%, buy NO when price >85%"
    min_edge = 0.06

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        base = super().filter(markets)
        return [m for m in base if m.price < 0.15 or m.price > 0.85]

    def fair_prob(self, market: SimMarket) -> float:
        est = _claude_estimate(market, noise_std=0.06)
        # Longshot bias correction: push estimate slightly away from extremes
        if market.price < 0.15:
            correction = random.uniform(0.02, 0.06)
            est = min(est + correction, 0.95)
        elif market.price > 0.85:
            correction = random.uniform(0.02, 0.04)
            est = max(est - correction, 0.05)
        return est

    def get_edge(self, fair: float, price: float) -> float:
        if price < 0.15:
            return fair - price
        else:  # price > 0.85: we're buying NO
            return (1 - fair) - (1 - price)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 7: Volume spike — unusual activity signals incoming information
# ─────────────────────────────────────────────────────────────────────────────

class S7_VolumeSpike(BaseStrategy):
    name = "S7_VolumeSpike"
    description = "Trade markets with 24h volume ≥2× 7-day average — volume precedes price moves"
    min_edge = 0.08

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        base = super().filter(markets)
        return [m for m in base if m.volume_spike >= 2.0]

    def fair_prob(self, market: SimMarket) -> float:
        # Volume spike = information event; price likely moving toward true
        # Assume market price has already partially corrected (50%)
        # Our estimate leans more toward the direction of the spike
        correction = 0.5 * (market.true_prob - market.price)  # partial
        base_est = _claude_estimate(market, noise_std=0.07)
        return max(0.02, min(0.98, base_est + 0.3 * correction))


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 8: Mean reversion (price moved >20% in 24h — overreaction)
# ─────────────────────────────────────────────────────────────────────────────

class S8_MeanReversion(BaseStrategy):
    name = "S8_MeanReversion"
    description = "Fade 24h price moves >20% — markets overreact to news, then revert"
    min_edge = 0.09

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        base = super().filter(markets)
        return [m for m in base if abs(m.price_change_24h) >= 0.20]

    def fair_prob(self, market: SimMarket) -> float:
        # Price moved a lot; we expect partial reversion
        # Our fair value estimate leans opposite to the recent move
        reversion = -0.40 * market.price_change_24h
        base_est = _claude_estimate(market, noise_std=0.09)
        return max(0.02, min(0.98, base_est + reversion))


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 9: Conservative threshold (15% edge required)
# ─────────────────────────────────────────────────────────────────────────────

class S9_ConservativeThreshold(BaseStrategy):
    name = "S9_ConservativeThreshold"
    description = "Only trade when edge ≥15% — fewer trades but much higher conviction"
    min_edge = 0.15

    def bet_fraction(self, fair: float, price: float) -> float:
        return _kelly(fair, price, fraction=0.5, cap=0.06)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 10: Crypto-only specialist
# ─────────────────────────────────────────────────────────────────────────────

class S10_CryptoSpecialist(BaseStrategy):
    name = "S10_CryptoSpecialist"
    description = "Only crypto markets — Claude has strong knowledge edge vs average bettors"
    min_edge = 0.07

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        base = super().filter(markets)
        return [m for m in base if m.category == "crypto"]

    def fair_prob(self, market: SimMarket) -> float:
        # Claude has strong crypto knowledge — lower noise
        return _claude_estimate(market, noise_std=0.06)

    def bet_fraction(self, fair: float, price: float) -> float:
        return _kelly(fair, price, fraction=0.6, cap=0.06)  # slightly higher Kelly


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 11: Politics specialist
# ─────────────────────────────────────────────────────────────────────────────

class S11_PoliticsSpecialist(BaseStrategy):
    name = "S11_PoliticsSpecialist"
    description = "Only political markets — highest volume/liquidity, recency bias exploitable"
    min_edge = 0.08

    def filter(self, markets: list[SimMarket]) -> list[SimMarket]:
        base = super().filter(markets)
        return [m for m in base if m.category == "politics"]

    def fair_prob(self, market: SimMarket) -> float:
        # Political markets: recency bias inflates favorite prices
        # Correct by pulling toward 0.5 slightly
        base_est = _claude_estimate(market, noise_std=0.09)
        recency_correction = -0.05 * (base_est - 0.5) / abs(base_est - 0.5 + 1e-9)
        return max(0.02, min(0.98, base_est + recency_correction))


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 12: Kelly aggressive (full Kelly, no fraction, higher cap)
# ─────────────────────────────────────────────────────────────────────────────

class S12_AggressiveKelly(BaseStrategy):
    name = "S12_AggressiveKelly"
    description = "Full Kelly (no ×0.5), cap 10% — maximises EV but high variance"
    min_edge = 0.08

    def bet_fraction(self, fair: float, price: float) -> float:
        return _kelly(fair, price, fraction=1.0, cap=0.10)
