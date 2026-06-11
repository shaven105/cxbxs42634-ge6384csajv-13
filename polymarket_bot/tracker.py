import logging
from dataclasses import dataclass, field
from typing import Optional

from config import (
    MIN_USDC_BALANCE,
    CLAUDE_INPUT_COST_PER_M,
    CLAUDE_OUTPUT_COST_PER_M,
)

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    market_question: str
    token_id: str
    side: str
    price: float
    size_shares: float
    usdc_spent: float
    timestamp: str


class Tracker:
    def __init__(self, clob_client):
        self._clob = clob_client
        self.cumulative_claude_cost_usd: float = 0.0
        self.realized_pnl_usdc: float = 0.0
        self.open_trades: list[TradeRecord] = []
        self._cached_balance: Optional[float] = None

    def get_usdc_balance(self) -> float:
        try:
            raw = self._clob.get_balance()
            # py-clob-client 0.34.x returns a plain string; newer may return dict
            usdc = float(raw) if isinstance(raw, (str, int, float)) else float(raw.get("available", 0))
            self._cached_balance = usdc
            logger.info(f"USDC balance: ${usdc:.2f}")
            return usdc
        except Exception as exc:
            logger.error(f"Failed to fetch balance: {exc}")
            return self._cached_balance if self._cached_balance is not None else 0.0

    def is_alive(self) -> bool:
        balance = self.get_usdc_balance()
        if balance <= MIN_USDC_BALANCE:
            logger.critical(
                f"Kill switch: balance ${balance:.2f} <= minimum ${MIN_USDC_BALANCE:.2f}"
            )
            return False
        return True

    def record_claude_usage(self, input_tokens: int, output_tokens: int) -> float:
        cost = (
            input_tokens * CLAUDE_INPUT_COST_PER_M / 1_000_000
            + output_tokens * CLAUDE_OUTPUT_COST_PER_M / 1_000_000
        )
        self.cumulative_claude_cost_usd += cost
        logger.debug(
            f"Claude: {input_tokens}in + {output_tokens}out = "
            f"${cost:.6f} (total ${self.cumulative_claude_cost_usd:.4f})"
        )
        return cost

    def record_trade(self, trade: TradeRecord) -> None:
        self.open_trades.append(trade)
        logger.info(
            f"Trade: {trade.market_question[:60]} | "
            f"{trade.side} @ {trade.price:.3f} | "
            f"{trade.size_shares:.2f} shares | ${trade.usdc_spent:.2f} USDC"
        )

    def net_pnl(self) -> float:
        return self.realized_pnl_usdc - self.cumulative_claude_cost_usd

    def summary(self) -> str:
        return (
            f"Balance: ${self._cached_balance or 0:.2f} | "
            f"Claude costs: ${self.cumulative_claude_cost_usd:.4f} | "
            f"Net P&L: ${self.net_pnl():.4f} | "
            f"Trades: {len(self.open_trades)}"
        )
