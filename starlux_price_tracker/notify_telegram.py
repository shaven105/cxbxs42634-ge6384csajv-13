#!/usr/bin/env python3
"""Send today's fare reading to Telegram, in Traditional Chinese."""
from __future__ import annotations

import csv
import json
import os
import urllib.parse
from pathlib import Path
from urllib.request import Request, urlopen

CSV_PATH = Path(__file__).parent / "data" / "price_history.csv"


def load_last_two() -> tuple[dict | None, dict | None]:
    with CSV_PATH.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None, None
    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else None
    return latest, previous


def fmt_price(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return f"TWD {float(v):,.0f}"
    except ValueError:
        return None


def build_message(latest: dict, previous: dict | None) -> str:
    lines = ["✈️ 星宇航空 桃園↔曼谷 票價追蹤", f"目標行程：12/4 出發 → 12/8 回程（單人）", ""]

    month_price = fmt_price(latest.get("target_month_price_twd"))
    if month_price:
        seen = latest.get("target_month_seen") or ""
        lines.append(f"📅 {latest.get('target_month_label')} 最低來回經濟艙起價：{month_price}")
        if seen:
            lines.append(f"　（星宇官網快取資料，{seen}）")

        prev_price = previous.get("target_month_price_twd") if previous else None
        if prev_price:
            try:
                diff = float(latest["target_month_price_twd"]) - float(prev_price)
                if abs(diff) >= 1:
                    arrow = "🔺漲" if diff > 0 else "🔻跌"
                    lines.append(f"　較上次記錄{arrow} TWD {abs(diff):,.0f}")
                else:
                    lines.append("　與上次記錄持平")
            except (TypeError, ValueError):
                pass
    else:
        lines.append("⚠️ 這次沒能從官網抓到當月起價資料（頁面結構可能變了）")

    exact = fmt_price(latest.get("exact_match_price_twd"))
    if exact:
        lines.append("")
        lines.append(f"🎯 剛好抓到 12/4-12/8 這組日期的快取報價：{exact}（{latest.get('exact_match_note')}）")

    nearby = fmt_price(latest.get("nearby_price_twd"))
    if nearby and not exact:
        lines.append("")
        lines.append(
            f"👀 附近日期的快取報價參考：{latest.get('nearby_departure')} → "
            f"{latest.get('nearby_return')}：{nearby}"
        )

    lines.append("")
    lines.append(
        "說明：星宇官網目前公開的是「當月快取最低價」，不是逐日的 12/4-8 精確報價"
        "（要拿到精確報價得走實際訂票流程）。這裡追的是可穩定每日抓取的價格訊號，"
        "僅供參考，實際訂票請以官網查詢為準。"
    )
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    req = Request(url, data=data, method="POST")
    with urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        if not result.get("ok"):
            raise RuntimeError(f"Telegram send failed: {result}")


if __name__ == "__main__":
    latest, previous = load_last_two()
    if latest is None:
        raise SystemExit("No fare data found in price_history.csv — run track_fare.py first")
    message = build_message(latest, previous)
    print(message)
    send_telegram(message)
