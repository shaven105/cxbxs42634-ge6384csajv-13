"""
Run all 12 strategies over the synthetic market universe.
Simulates 100 trades per strategy, then ranks by composite score.

Composite score = total_return × win_rate_weight / (1 + MDD_penalty)
Eliminates strategies where:
  - win_rate < 50%   (worse than coin flip after edge requirement)
  - n_trades < 10    (not enough opportunity / too selective)
  - single-trade max-bet > 8% (extreme variance, single-flip risk)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import random
from backtest.market_generator import generate_market_universe
from backtest.wallet_simulator import generate_wallet_universe, get_top_wallets
from backtest.strategies import (
    S1_Baseline, S2_CopyWallet, S3_NearResolution, S4_DeepLiquidity,
    S5_NicheSpecialist, S6_ContrarianExtreme, S7_VolumeSpike,
    S8_MeanReversion, S9_ConservativeThreshold, S10_CryptoSpecialist,
    S11_PoliticsSpecialist, S12_AggressiveKelly,
)
from backtest.simulator import run_backtest, BacktestResult

N_SEEDS = 5          # run each strategy on N different random seeds, average
TRADES_PER_RUN = 100


def composite_score(r: BacktestResult) -> float:
    if r.n_trades < 10 or r.win_rate < 0.50:
        return -999.0
    # Penalise strategies with avg bet > 7% (single-flip blowup risk)
    if r.avg_bet_fraction > 0.07:
        return -999.0
    # Score: return × WR adjustment, penalised by MDD
    wr_adj = (r.win_rate - 0.50) * 2   # 0 at 50%, 1 at 100%
    return r.total_return_pct * (1 + wr_adj) / (1 + r.max_drawdown_pct / 100)


def run_all(verbose: bool = True) -> list[BacktestResult]:
    print("=" * 80)
    print("POLYMARKET STRATEGY BACKTEST — 100 trades × 5 seeds each")
    print("=" * 80)

    universe = generate_market_universe(n=800, seed=42)
    wallets  = generate_wallet_universe(n_wallets=200, seed=99)
    top_wallets = get_top_wallets(wallets, n=10)

    if verbose:
        print(f"\nMarket universe: {len(universe)} markets")
        print(f"Top copy-wallets: {len(top_wallets)} wallets\n")

    strategy_factories = [
        lambda: S1_Baseline(),
        lambda: S2_CopyWallet(top_wallets),
        lambda: S3_NearResolution(),
        lambda: S4_DeepLiquidity(),
        lambda: S5_NicheSpecialist(),
        lambda: S6_ContrarianExtreme(),
        lambda: S7_VolumeSpike(),
        lambda: S8_MeanReversion(),
        lambda: S9_ConservativeThreshold(),
        lambda: S10_CryptoSpecialist(),
        lambda: S11_PoliticsSpecialist(),
        lambda: S12_AggressiveKelly(),
    ]

    averaged_results: list[BacktestResult] = []

    for factory in strategy_factories:
        seed_results = []
        for seed in range(N_SEEDS):
            strat = factory()
            r = run_backtest(strat, universe, max_trades=TRADES_PER_RUN, seed=seed * 13 + 7)
            seed_results.append(r)

        # Average across seeds
        from backtest.simulator import BacktestResult as BR
        first = seed_results[0]
        avg = BR(
            strategy_name=first.strategy_name,
            description=first.description,
            n_trades=int(sum(r.n_trades for r in seed_results) / N_SEEDS),
            win_rate=sum(r.win_rate for r in seed_results) / N_SEEDS,
            final_bankroll=sum(r.final_bankroll for r in seed_results) / N_SEEDS,
            total_return_pct=sum(r.total_return_pct for r in seed_results) / N_SEEDS,
            max_drawdown_pct=sum(r.max_drawdown_pct for r in seed_results) / N_SEEDS,
            sharpe=sum(r.sharpe for r in seed_results) / N_SEEDS,
            avg_edge=sum(r.avg_edge for r in seed_results) / N_SEEDS,
            avg_bet_fraction=sum(r.avg_bet_fraction for r in seed_results) / N_SEEDS,
        )
        averaged_results.append(avg)

    # Print all results
    print(f"\n{'Strategy':<30} | {'Trades':>6} | {'WinRate':>7} | {'Return':>8} | {'MDD':>6} | {'Sharpe':>7} | {'AvgEdge':>8} | {'Score':>7}")
    print("-" * 110)

    ranked = sorted(averaged_results, key=composite_score, reverse=True)

    for r in ranked:
        score = composite_score(r)
        disq = " [DISQUALIFIED]" if score <= -999 else ""
        print(
            f"{r.strategy_name:<30} | "
            f"{r.n_trades:>6} | "
            f"{r.win_rate*100:>6.1f}% | "
            f"{r.total_return_pct:>+7.1f}% | "
            f"{r.max_drawdown_pct:>5.1f}% | "
            f"{r.sharpe:>6.2f} | "
            f"{r.avg_edge*100:>7.1f}% | "
            f"{score if score > -999 else 'N/A':>7}"
            + disq
        )

    # Top 3
    top3 = [r for r in ranked if composite_score(r) > -999][:3]
    print("\n" + "=" * 80)
    print("TOP 3 STRATEGIES")
    print("=" * 80)
    for i, r in enumerate(top3, 1):
        print(f"\n#{i}  {r.strategy_name}")
        print(f"    {r.description}")
        print(f"    Return: {r.total_return_pct:+.1f}%  |  Win rate: {r.win_rate*100:.1f}%  |  "
              f"MDD: {r.max_drawdown_pct:.1f}%  |  Sharpe: {r.sharpe:.2f}  |  "
              f"Avg edge: {r.avg_edge*100:.1f}%  |  Trades: {r.n_trades}")

    return ranked


if __name__ == "__main__":
    run_all()
