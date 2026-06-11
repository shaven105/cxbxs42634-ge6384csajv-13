"""
Trade simulator and metrics engine.

For each strategy:
1. Filter the market universe
2. For each filtered market, compute fair_prob and edge
3. If edge >= min_edge, compute bet_fraction and simulate the trade outcome
4. Outcome = Bernoulli(true_prob) — the market resolves YES or NO
5. Track bankroll evolution over N trades (stop at 100 or universe exhausted)

Metrics returned:
  - final_bankroll (starting from $1.00)
  - total_return_pct
  - win_rate
  - sharpe (annualised, assumes 1 cycle/10min → ~144 cycles/day)
  - max_drawdown
  - n_trades
  - avg_edge
  - avg_bet_fraction
"""

import random
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backtest.market_generator import SimMarket, generate_market_universe
from backtest.wallet_simulator import generate_wallet_universe, get_top_wallets

if TYPE_CHECKING:
    from backtest.strategies import BaseStrategy


POLYMARKET_FEE = 0.02  # 2% taker fee


@dataclass
class TradeResult:
    market_id: str
    fair_prob: float
    price: float
    edge: float
    bet_fraction: float
    won: bool
    pnl_fraction: float   # fractional change in bankroll for this trade


@dataclass
class BacktestResult:
    strategy_name: str
    description: str
    n_trades: int
    win_rate: float
    final_bankroll: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    avg_edge: float
    avg_bet_fraction: float
    trades: list[TradeResult] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"{self.strategy_name:<30} | "
            f"trades={self.n_trades:>3} | "
            f"WR={self.win_rate*100:>5.1f}% | "
            f"return={self.total_return_pct:>+7.1f}% | "
            f"MDD={self.max_drawdown_pct:>5.1f}% | "
            f"Sharpe={self.sharpe:>5.2f} | "
            f"avgEdge={self.avg_edge*100:>4.1f}%"
        )


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var) if var > 0 else 1e-9
    # Annualise: ~144 10-min cycles per day × 365 days
    return (mean / std) * math.sqrt(144 * 365)


def _max_drawdown(bankroll_curve: list[float]) -> float:
    peak = bankroll_curve[0]
    max_dd = 0.0
    for b in bankroll_curve:
        if b > peak:
            peak = b
        dd = (peak - b) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd * 100


def run_backtest(
    strategy: "BaseStrategy",
    universe: list[SimMarket],
    max_trades: int = 100,
    starting_bankroll: float = 1.0,
    seed: int = 42,
) -> BacktestResult:
    random.seed(seed)

    filtered = strategy.filter(universe)
    random.shuffle(filtered)

    bankroll = starting_bankroll
    trades: list[TradeResult] = []
    bankroll_curve = [bankroll]
    returns: list[float] = []

    for market in filtered:
        if len(trades) >= max_trades:
            break
        if bankroll <= 0.001:
            break

        fair = strategy.fair_prob(market)
        edge = strategy.get_edge(fair, market.price)

        if edge < strategy.min_edge:
            continue

        bet_frac = strategy.bet_fraction(fair, market.price)
        if bet_frac <= 0:
            continue

        # Simulate outcome: Bernoulli(true_prob)
        won = random.random() < market.true_prob

        # P&L calculation
        bet_usdc = bankroll * bet_frac
        if won:
            # Win: received 1 share worth $1, paid market.price per share
            gross_profit = bet_usdc * (1 - market.price) / market.price
            fee = bet_usdc * POLYMARKET_FEE
            pnl = gross_profit - fee
        else:
            # Lose: shares expire worthless
            fee = bet_usdc * POLYMARKET_FEE
            pnl = -bet_usdc - fee

        pnl_fraction = pnl / bankroll
        bankroll += pnl
        bankroll = max(0.0, bankroll)
        bankroll_curve.append(bankroll)
        returns.append(pnl_fraction)

        trades.append(TradeResult(
            market_id=market.market_id,
            fair_prob=fair,
            price=market.price,
            edge=edge,
            bet_fraction=bet_frac,
            won=won,
            pnl_fraction=pnl_fraction,
        ))

    n = len(trades)
    if n == 0:
        return BacktestResult(
            strategy_name=strategy.name,
            description=strategy.description,
            n_trades=0,
            win_rate=0.0,
            final_bankroll=starting_bankroll,
            total_return_pct=0.0,
            max_drawdown_pct=0.0,
            sharpe=0.0,
            avg_edge=0.0,
            avg_bet_fraction=0.0,
        )

    win_rate = sum(1 for t in trades if t.won) / n
    total_return = (bankroll - starting_bankroll) / starting_bankroll * 100
    mdd = _max_drawdown(bankroll_curve)
    sharpe = _sharpe(returns)
    avg_edge = sum(t.edge for t in trades) / n
    avg_bet = sum(t.bet_fraction for t in trades) / n

    return BacktestResult(
        strategy_name=strategy.name,
        description=strategy.description,
        n_trades=n,
        win_rate=win_rate,
        final_bankroll=bankroll,
        total_return_pct=total_return,
        max_drawdown_pct=mdd,
        sharpe=sharpe,
        avg_edge=avg_edge,
        avg_bet_fraction=avg_bet,
        trades=trades,
    )
