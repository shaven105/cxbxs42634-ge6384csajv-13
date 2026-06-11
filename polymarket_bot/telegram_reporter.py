"""
Telegram report sender for paper trading daily summary.
Uses Telegram Bot API sendMessage with MarkdownV2 formatting.

Requires env vars:
  TELEGRAM_BOT_TOKEN  — from @BotFather
  TELEGRAM_CHAT_ID    — your personal chat ID
"""

import logging
import urllib.request
import urllib.parse
import json
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _get_api_base() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable not set")
    return f"https://api.telegram.org/bot{token}"


def _get_chat_id() -> str:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID environment variable not set")
    return chat_id


def _esc(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _pnl_emoji(val) -> str:
    if val is None:
        return "⏳"
    return "✅" if val >= 0 else "❌"


def _fmt_pnl(val) -> str:
    if val is None:
        return "待結算"
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:.2f}"


def send_message(text: str) -> bool:
    """Send a MarkdownV2 message via Telegram Bot API."""
    url = f"{_get_api_base()}/sendMessage"
    payload = json.dumps({
        "chat_id": _get_chat_id(),
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                logger.info("Telegram message sent successfully")
                return True
            logger.error(f"Telegram API error: {result}")
            return False
    except Exception as exc:
        logger.error(f"Telegram send failed: {exc}")
        return False


def build_daily_report(state: dict, new_trades: list, newly_resolved: list) -> str:
    now = datetime.now(timezone.utc).strftime("%Y\\-%m\\-%d %H:%M UTC")
    bankroll = state["virtual_bankroll"]
    start = state["start_bankroll"]
    roi = (bankroll - start) / start * 100
    claude_cost = state["total_claude_cost_usd"]
    net_pnl = state["total_realized_pnl"] - claude_cost
    all_trades = state["trades"]
    open_trades = [t for t in all_trades if not t.get("resolved")]
    resolved = [t for t in all_trades if t.get("resolved")]
    win_count = sum(1 for t in resolved if t.get("outcome"))
    wr = win_count / len(resolved) * 100 if resolved else 0
    roi_arrow = "📈" if roi >= 0 else "📉"
    pnl_sign = "\\+" if net_pnl >= 0 else ""

    lines = [
        f"*📊 Polymarket 模擬日報*",
        f"_{now}_",
        "",
        f"{'─' * 28}",
        f"💰 虛擬餘額　　`${bankroll:.2f}`",
        f"{roi_arrow} 累積 ROI　　`{roi:+.1f}%`",
        f"📊 淨 P&L　　　`{pnl_sign}${abs(net_pnl):.2f}`",
        f"🎯 勝率　　　　`{wr:.0f}%`",
        f"🤖 API 費用　　`${claude_cost:.4f}`",
        f"{'─' * 28}",
    ]

    # New signals today
    if new_trades:
        lines.append(f"\n*🆕 今日新信號 \\({len(new_trades)} 筆\\)*")
        for t in new_trades[:5]:  # max 5 to keep message short
            q = _esc(t.get("question", "")[:45])
            side = t.get("side", "")
            price = t.get("price", 0)
            edge = t.get("edge", 0)
            bet = t.get("bet_usdc", 0)
            lines.append(
                f"• `{side}` @ `{price:.3f}` \\| edge `{edge:.1%}` \\| `${bet:.2f}`\n"
                f"  _{q}_"
            )
        if len(new_trades) > 5:
            lines.append(f"  _\\.\\.\\. 還有 {len(new_trades)-5} 筆_")
    else:
        lines.append("\n*🆕 今日新信號*\n_無信號_")

    # Resolved today
    if newly_resolved:
        lines.append(f"\n*✅ 今日結算 \\({len(newly_resolved)} 筆\\)*")
        for t in newly_resolved:
            emoji = _pnl_emoji(t.get("pnl_usdc"))
            q = _esc(t.get("question", "")[:40])
            pnl_str = _esc(_fmt_pnl(t.get("pnl_usdc")))
            lines.append(f"{emoji} `{pnl_str}` — _{q}_")
    else:
        lines.append("\n*✅ 今日結算*\n_無新結算_")

    # Open positions summary
    lines.append(
        f"\n*⏳ 未結算倉位*　`{len(open_trades)} 筆`"
    )

    return "\n".join(lines)


def send_daily_report(state: dict, new_trades: list, newly_resolved: list) -> None:
    msg = build_daily_report(state, new_trades, newly_resolved)
    send_message(msg)
