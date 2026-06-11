"""
Daily paper trading runner — called by GitHub Actions.

Flow:
  1. Fetch real Polymarket markets (Gamma API)
  2. Check if any previously recorded markets have resolved → update P&L
  3. Run S5 NicheSpecialist strategy on today's markets → record new signals
  4. Send HTML email report
  5. Save updated state
"""

import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("paper_trade")

# ── Import bot modules ──────────────────────────────────────────────────────

from scanner import fetch_active_markets, filter_markets
from valuator import estimate_fair_probability
from strategy import evaluate_market
from tracker import Tracker
from paper_trader import (
    load_state, save_state, check_resolutions,
    record_paper_trade, PaperTrade, STARTING_BANKROLL,
)
from reporter import build_html_report, build_subject

NICHE_CATEGORIES = {"weather", "science", "entertainment"}
REPORT_TO = "shaven52014@gmail.com"


# ── Fake CLOB client for paper trading (no real orders) ─────────────────────

class PaperClobClient:
    def get_balance(self):
        state = load_state()
        return str(state["virtual_bankroll"])


class PaperTracker(Tracker):
    def __init__(self, state: dict):
        self._state = state
        self._clob = PaperClobClient()
        self.cumulative_claude_cost_usd = state["total_claude_cost_usd"]
        self.realized_pnl_usdc = state["total_realized_pnl"]
        self.open_trades = []
        self._cached_balance = state["virtual_bankroll"]

    def record_claude_usage(self, input_tokens: int, output_tokens: int) -> float:
        cost = super().record_claude_usage(input_tokens, output_tokens)
        self._state["total_claude_cost_usd"] = self.cumulative_claude_cost_usd
        return cost

    def get_usdc_balance(self) -> float:
        self._cached_balance = self._state["virtual_bankroll"]
        return self._cached_balance


# ── Email sender ─────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> None:
    smtp_user = os.environ.get("GMAIL_USER")
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD")

    if not smtp_user or not smtp_pass:
        logger.warning("GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping email.")
        logger.info("=== REPORT (stdout) ===")
        logger.info(subject)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = REPORT_TO
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, REPORT_TO, msg.as_string())
        logger.info(f"Email sent to {REPORT_TO}")
    except Exception as exc:
        logger.error(f"Email send failed: {exc}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=== Paper trading daily run ===")
    state = load_state()
    tracker = PaperTracker(state)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Step 1: Fetch markets
    logger.info("Fetching markets from Gamma API...")
    raw_markets = fetch_active_markets(limit=800)
    markets_by_id = {m.get("id"): m for m in raw_markets if m.get("id")}

    # Step 2: Resolve any open trades
    newly_resolved = check_resolutions(state, markets_by_id)
    if newly_resolved:
        logger.info(f"Resolved {len(newly_resolved)} trades today")
        for t in newly_resolved:
            result = "WIN" if t.outcome else "LOSS"
            logger.info(f"  [{result}] {t.question[:50]} | P&L: {t.pnl_usdc:+.2f}")

    # Step 3: Run S5 strategy on niche markets
    niche_raw = [m for m in raw_markets if m.get("category", "").lower() in NICHE_CATEGORIES]
    tradeable = filter_markets(niche_raw)
    logger.info(f"S5 candidates: {len(niche_raw)} niche → {len(tradeable)} tradeable")

    bankroll = state["virtual_bankroll"]
    new_trades = []

    for market in tradeable:
        if bankroll <= 1.0:
            logger.warning("Virtual bankroll too low — stopping paper scan")
            break

        fair = estimate_fair_probability(market, tracker)
        if fair is None:
            continue

        signal = evaluate_market(market, fair, bankroll)
        if signal is None:
            continue

        trade = PaperTrade(
            date=today,
            market_id=signal.market_id,
            question=signal.market_question,
            category=market.get("category", ""),
            side=signal.side_label,
            price=signal.market_price,
            fair_prob=signal.fair_prob,
            edge=signal.edge,
            bet_usdc=round(signal.bet_usdc, 2),
            shares=round(signal.bet_usdc / signal.market_price, 2),
        )
        record_paper_trade(state, trade)
        new_trades.append(trade.__dict__)
        bankroll = state["virtual_bankroll"]

    logger.info(
        f"Today: {len(new_trades)} new signals | "
        f"Balance: ${state['virtual_bankroll']:.2f} | "
        f"Claude cost: ${state['total_claude_cost_usd']:.4f}"
    )

    # Step 4: Save state
    state["last_run"] = today
    save_state(state)

    # Step 5: Send report
    html = build_html_report(
        state=state,
        new_trades=new_trades,
        newly_resolved=[t.__dict__ for t in newly_resolved],
    )
    subject = build_subject(state)
    send_email(subject, html)


if __name__ == "__main__":
    main()
