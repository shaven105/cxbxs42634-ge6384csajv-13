import logging
from dataclasses import dataclass
from typing import Optional

from config import (
    MISPRICING_THRESHOLD,
    HALF_KELLY_FRACTION,
    MAX_BET_FRACTION,
    MIN_BET_USDC,
)

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    market_question: str
    market_id: str
    token_id: str
    side_label: str       # "YES" or "NO"
    fair_prob: float
    market_price: float   # best ask for the chosen token
    edge: float           # fair_prob - market_price
    kelly_fraction: float
    bet_usdc: float


def _half_kelly(fair_prob: float, price: float) -> float:
    """
    Half-Kelly fraction, capped at MAX_BET_FRACTION.

    b  = net odds = (1 - price) / price
    f* = (b·p - q) / b   (full Kelly)
    return min(f* / 2, MAX_BET_FRACTION)
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    b = (1.0 - price) / price
    p = fair_prob
    q = 1.0 - p
    full_kelly = (b * p - q) / b
    if full_kelly <= 0.0:
        return 0.0
    return min(full_kelly * HALF_KELLY_FRACTION, MAX_BET_FRACTION)


def evaluate_market(
    market: dict,
    fair_prob_yes: float,
    bankroll_usdc: float,
) -> Optional[TradeSignal]:
    """
    Return a TradeSignal for the better of YES/NO if edge >= MISPRICING_THRESHOLD,
    else None.

    YES side: buy YES at best_ask_yes
    NO side:  buy NO at (1 - best_bid_yes)  — binary complement relationship
    """
    question = market.get("question", "?")
    market_id = market.get("id", "?")
    token_ids = market.get("_token_ids", [None, None])
    best_bid = market["_best_bid"]
    best_ask = market["_best_ask"]

    # YES edge
    edge_yes = fair_prob_yes - best_ask

    # NO edge — on a binary CLOB: ask_no = 1 - bid_yes
    ask_no = 1.0 - best_bid
    fair_prob_no = 1.0 - fair_prob_yes
    edge_no = fair_prob_no - ask_no

    if max(edge_yes, edge_no) < MISPRICING_THRESHOLD:
        return None

    if edge_yes >= edge_no:
        side_label, token_id = "YES", token_ids[0]
        market_price, fair_prob, edge = best_ask, fair_prob_yes, edge_yes
    else:
        side_label, token_id = "NO", token_ids[1]
        market_price, fair_prob, edge = ask_no, fair_prob_no, edge_no

    if token_id is None:
        logger.warning(f"Missing {side_label} token_id for {market_id}")
        return None

    frac = _half_kelly(fair_prob, market_price)
    if frac <= 0.0:
        return None

    bet_usdc = frac * bankroll_usdc
    if bet_usdc < MIN_BET_USDC:
        return None

    logger.info(
        f"SIGNAL: {question[:60]} | {side_label} @ {market_price:.3f} | "
        f"fair={fair_prob:.3f} edge={edge:.3f} bet=${bet_usdc:.2f}"
    )
    return TradeSignal(
        market_question=question,
        market_id=market_id,
        token_id=token_id,
        side_label=side_label,
        fair_prob=fair_prob,
        market_price=market_price,
        edge=edge,
        kelly_fraction=frac,
        bet_usdc=bet_usdc,
    )
