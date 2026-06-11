import logging
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from config import (
    CLOB_API_BASE,
    PRIVATE_KEY,
    CHAIN_ID,
    SIGNATURE_TYPE,
    FUNDER_ADDRESS,
)
from strategy import TradeSignal
from tracker import TradeRecord, Tracker

logger = logging.getLogger(__name__)


def build_clob_client() -> ClobClient:
    """
    Initialise and authenticate the CLOB client.

    One-time setup required before first trade:
      client.set_allowance(USDC_ADDRESS, amount)
    See py-clob-client README for the USDC contract address on Polygon.
    """
    c = ClobClient(
        CLOB_API_BASE,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER_ADDRESS,
    )
    c.set_api_creds(c.create_or_derive_api_creds())
    logger.info("CLOB client initialised")
    return c


def _fresh_best_ask(clob_client: ClobClient, token_id: str) -> Optional[float]:
    try:
        book = clob_client.get_order_book(token_id)
        if not book or not book.asks:
            return None
        return float(book.asks[0].price)
    except Exception as exc:
        logger.warning(f"Order book refresh failed for {token_id[:16]}...: {exc}")
        return None


def place_limit_order(
    clob_client: ClobClient,
    signal: TradeSignal,
    tracker: Tracker,
    timestamp: str,
) -> bool:
    """
    Submit a GTC limit order to buy shares at signal.market_price.

    Anti-drift guard: re-fetch live best ask; skip if ask has risen > 2 ticks
    since the market was scanned. This prevents stale-price executions during
    the multi-minute scan window.
    """
    limit_price = round(signal.market_price, 2)

    fresh_ask = _fresh_best_ask(clob_client, signal.token_id)
    if fresh_ask is not None:
        drift = fresh_ask - limit_price
        if drift > 0.02:
            logger.warning(
                f"Price drifted +{drift:.3f} to {fresh_ask:.3f}; "
                f"skipping {signal.market_question[:50]}"
            )
            return False
        if fresh_ask < limit_price:
            limit_price = round(fresh_ask, 2)

    shares = round(signal.bet_usdc / limit_price, 2)
    if shares <= 0:
        return False

    logger.info(
        f"ORDER: BUY {shares:.2f} {signal.side_label} @ {limit_price:.3f} | "
        f"{signal.market_question[:50]}"
    )

    try:
        order_args = OrderArgs(
            token_id=signal.token_id,
            price=limit_price,
            size=shares,
            side=BUY,
        )
        signed = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed, OrderType.GTC)
    except Exception as exc:
        logger.error(
            f"Order submission exception for {signal.market_question[:50]}: {exc}",
            exc_info=True,
        )
        return False

    if not resp or resp.get("status") in ("error", "failed", None):
        logger.error(f"Order rejected: {resp}")
        return False

    order_id = resp.get("orderID") or resp.get("order_id", "?")
    usdc_spent = shares * limit_price
    logger.info(f"Order accepted: {order_id} | ${usdc_spent:.2f} USDC")

    tracker.record_trade(TradeRecord(
        market_question=signal.market_question,
        token_id=signal.token_id,
        side=signal.side_label,
        price=limit_price,
        size_shares=shares,
        usdc_spent=usdc_spent,
        timestamp=timestamp,
    ))
    return True


def cancel_all_open_orders(clob_client: ClobClient) -> None:
    try:
        result = clob_client.cancel_all()
        logger.info(f"Cancelled all open orders: {result}")
    except Exception as exc:
        logger.error(f"Failed to cancel all orders: {exc}")
