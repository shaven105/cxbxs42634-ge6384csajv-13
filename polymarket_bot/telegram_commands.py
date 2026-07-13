"""
Telegram command listener — query the bot from Telegram.

Commands:
  /summary  (aliases: /status /positions /s)
      Current bankroll + every open position with live price,
      unrealized P&L and expiry time in Taiwan time (UTC+8).
  /help
      List available commands.

How it runs:
  The continuous-scan workflow calls
      python telegram_commands.py --listen 150
  between scans, so the 2.5-minute sleep doubles as a command-listening
  window — while the hourly loop is running, replies arrive within seconds.
  run_paper_trade.py also does one non-blocking poll per scan as a fallback.

Offset handling is stateless: passing offset=<last_update_id + 1> to
getUpdates acknowledges older updates server-side, so no offset file is
needed across runs.

Security: only messages from TELEGRAM_CHAT_ID are answered; anything else
is acknowledged and dropped.
"""

import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from config import GAMMA_API_BASE
from telegram_reporter import _esc, _get_api_base, _get_chat_id, send_message

logger = logging.getLogger("telegram_commands")

TAIPEI = timezone(timedelta(hours=8))

_SUMMARY_ALIASES = {"/summary", "/status", "/positions", "/s"}


# ── Telegram getUpdates plumbing ────────────────────────────────────────────

def _get_updates(offset: int | None = None, timeout: int = 0) -> list[dict]:
    params = {"timeout": timeout, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    url = f"{_get_api_base()}/getUpdates?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout + 15) as resp:
            data = json.loads(resp.read())
        return data.get("result", []) if data.get("ok") else []
    except Exception as exc:
        logger.warning(f"getUpdates failed: {exc}")
        return []


def _ack(last_update_id: int) -> None:
    """Mark everything up to last_update_id as processed (server-side)."""
    _get_updates(offset=last_update_id + 1, timeout=0)


# ── Live market lookup ──────────────────────────────────────────────────────

def _fetch_market(market_id: str) -> dict | None:
    try:
        url = f"{GAMMA_API_BASE}/markets/{market_id}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _live_side_price(market: dict, side: str) -> float | None:
    raw = market.get("outcomePrices")
    try:
        prices = [float(p) for p in (json.loads(raw) if isinstance(raw, str) else raw or [])]
    except Exception:
        return None
    if len(prices) < 2:
        return None
    return prices[0] if side == "YES" else prices[1]


def _parse_end(market: dict) -> datetime | None:
    for key in ("endDate", "endDateIso", "end_date_iso", "endDateISO"):
        raw = market.get(key)
        if not raw:
            continue
        try:
            if isinstance(raw, (int, float)):
                return datetime.fromtimestamp(raw, tz=timezone.utc)
            s = str(raw).strip().rstrip("Z")
            if "T" in s:
                dt = datetime.fromisoformat(s)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _fmt_expiry(end: datetime | None) -> str:
    if end is None:
        return "到期日未知"
    now = datetime.now(timezone.utc)
    local = end.astimezone(TAIPEI).strftime("%m/%d %H:%M")
    hours = (end - now).total_seconds() / 3600
    if hours < 0:
        return f"{local} 台北（已過期 {abs(hours):.1f}h，等結算）"
    if hours < 48:
        return f"{local} 台北（剩 {hours:.1f}h）"
    return f"{local} 台北（剩 {hours / 24:.1f} 天）"


# ── /summary builder ────────────────────────────────────────────────────────

def build_summary() -> str:
    from paper_trader import load_state
    from strategy_sniper import load_sniper_state

    state = load_state()
    sniper = load_sniper_state()

    bankroll = state.get("virtual_bankroll", 0)
    start = state.get("start_bankroll", 1000)
    roi = (bankroll - start) / start * 100 if start else 0

    trades = sniper.get("trades", [])
    open_t = [t for t in trades if not t.get("resolved")]
    resolved = [t for t in trades if t.get("resolved")]
    wins = sum(1 for t in resolved if t.get("outcome"))
    wr = wins / len(resolved) * 100 if resolved else 0

    now_local = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M")
    total_staked = sum(t.get("bet_usdc", 0) for t in open_t)
    roi_arrow = "📈" if roi >= 0 else "📉"

    lines = [
        "*📋 即時持倉總覽*",
        f"_{_esc(now_local)} 台北_",
        "",
        f"💰 可用餘額　`${bankroll:.2f}`",
        f"🔒 持倉占用　`${total_staked:.2f}`",
        f"{roi_arrow} 累積 ROI　`{roi:+.1f}%`",
        f"🎯 已結算　　`{len(resolved)} 筆`（勝率 `{wr:.0f}%`）",
        f"{'─' * 26}",
    ]

    if not open_t:
        lines.append("\n_目前沒有未結算倉位_ ✨")
        return "\n".join(lines)

    lines.append(f"\n*⏳ 未結算倉位（{len(open_t)} 筆）*")
    for i, t in enumerate(open_t, 1):
        q = _esc(t.get("question", "")[:60])
        side = t.get("side", "?")
        entry = t.get("price", 0)
        bet = t.get("bet_usdc", 0)
        shares = t.get("shares", 0)

        market = _fetch_market(t.get("market_id", "")) or {}
        cur = _live_side_price(market, side)
        expiry = _fmt_expiry(_parse_end(market))

        if cur is not None and shares:
            value = shares * cur
            upnl = value - bet
            sign = "＋" if upnl >= 0 else "－"
            pnl_line = (
                f"　現價 `{cur:.3f}` → 市值 `${value:.2f}`"
                f"（未實現 `{sign}${abs(upnl):.2f}`）"
            )
        else:
            pnl_line = "　_即時價格暫時抓不到_"

        lines += [
            f"\n*{i}\\.* _{q}_",
            f"　`{side}` @ `{entry:.3f}` \\| 投入 `${bet:.2f}`",
            _esc_keep_code(pnl_line),
            f"　⏰ {_esc(expiry)}",
        ]

    return "\n".join(lines)


def _esc_keep_code(text: str) -> str:
    """Escape MarkdownV2 specials outside `code` spans."""
    out, in_code = [], False
    for ch in text:
        if ch == "`":
            in_code = not in_code
            out.append(ch)
        elif not in_code and ch in r"_*[]()~>#+-=|{}.!":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


_HELP = (
    "*🤖 可用指令*\n\n"
    "/summary — 即時持倉總覽（含到期日、未實現損益）\n"
    "/help — 顯示此說明\n\n"
    "_別名：/status /positions /s_"
)


def _send(reply: str) -> None:
    if send_message(reply):
        return
    # MarkdownV2 rejected → resend as plain text so the user still gets an answer
    try:
        plain = reply.replace("\\", "").replace("*", "").replace("`", "").replace("_", "")
        payload = json.dumps({"chat_id": _get_chat_id(), "text": plain}).encode()
        req = urllib.request.Request(
            f"{_get_api_base()}/sendMessage", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15)
        logger.info("sent plain-text fallback")
    except Exception as exc:
        logger.error(f"plain-text fallback failed: {exc}")


# ── Dispatcher ──────────────────────────────────────────────────────────────

def _handle_text(text: str) -> str | None:
    cmd = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
    if cmd in _SUMMARY_ALIASES:
        return build_summary()
    if cmd in ("/help", "/start"):
        return _HELP
    return None


def poll_once(timeout: int = 0) -> int:
    """One getUpdates pass; answer commands from the owner chat. Returns #handled."""
    updates = _get_updates(timeout=timeout)
    if not updates:
        return 0

    my_chat = str(_get_chat_id())
    handled = 0
    for u in updates:
        msg = u.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = msg.get("text", "")
        if chat_id != my_chat or not text.startswith("/"):
            continue
        logger.info(f"Telegram command: {text}")
        reply = _handle_text(text)
        if reply:
            _send(reply)
            handled += 1

    _ack(max(u["update_id"] for u in updates))
    return handled


def _register_menu() -> None:
    """Register the command menu shown when the user types '/' (idempotent)."""
    try:
        payload = json.dumps({"commands": [
            {"command": "summary", "description": "即時持倉總覽（到期日、未實現損益）"},
            {"command": "help", "description": "指令說明"},
        ]}).encode()
        req = urllib.request.Request(
            f"{_get_api_base()}/setMyCommands", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def listen(seconds: int) -> None:
    """Long-poll for commands until the deadline (used between scans)."""
    _register_menu()
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        try:
            poll_once(timeout=int(min(25, remaining)))
        except Exception as exc:
            logger.warning(f"command poll error: {exc}")
            time.sleep(min(10, max(1, remaining)))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    secs = 0
    if len(sys.argv) >= 3 and sys.argv[1] == "--listen":
        secs = int(sys.argv[2])
    if secs > 0:
        listen(secs)
    else:
        poll_once()
