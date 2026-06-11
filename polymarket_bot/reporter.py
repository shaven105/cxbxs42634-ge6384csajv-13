"""
Generates HTML email reports for paper trading daily summary.
"""

from datetime import datetime, timezone


def _pnl_color(val: float) -> str:
    if val is None:
        return "#888"
    return "#27ae60" if val >= 0 else "#e74c3c"


def _fmt_pnl(val) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:.2f}"


def build_html_report(state: dict, new_trades: list, newly_resolved: list) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bankroll = state["virtual_bankroll"]
    start = state["start_bankroll"]
    total_pnl = state["total_realized_pnl"]
    claude_cost = state["total_claude_cost_usd"]
    net_pnl = total_pnl - claude_cost
    roi = (bankroll - start) / start * 100
    all_trades = state["trades"]
    open_trades = [t for t in all_trades if not t.get("resolved")]
    resolved_trades = [t for t in all_trades if t.get("resolved")]
    win_count = sum(1 for t in resolved_trades if t.get("outcome"))
    wr = win_count / len(resolved_trades) * 100 if resolved_trades else 0

    def trade_row(t, highlight=False):
        bg = "#fff9e6" if highlight else "white"
        pnl = t.get("pnl_usdc")
        status = "✅ 獲利" if t.get("outcome") else ("❌ 虧損" if t.get("resolved") else "⏳ 待結算")
        return f"""
        <tr style="background:{bg}">
          <td>{t.get('date','')}</td>
          <td style="max-width:260px;font-size:13px">{t.get('question','')[:70]}</td>
          <td><b>{t.get('side','')}</b></td>
          <td>{t.get('price', 0):.3f}</td>
          <td>{t.get('fair_prob', 0):.3f}</td>
          <td>{t.get('edge', 0):.1%}</td>
          <td>${t.get('bet_usdc', 0):.2f}</td>
          <td style="color:{_pnl_color(pnl)};font-weight:bold">{_fmt_pnl(pnl)}</td>
          <td>{status}</td>
        </tr>"""

    new_rows = "".join(trade_row(t, highlight=True) for t in new_trades) if new_trades else \
        '<tr><td colspan="9" style="text-align:center;color:#888">今日無新信號</td></tr>'

    resolved_rows = "".join(trade_row(t) for t in newly_resolved) if newly_resolved else \
        '<tr><td colspan="9" style="text-align:center;color:#888">今日無新結算</td></tr>'

    open_rows = "".join(trade_row(t) for t in open_trades) if open_trades else \
        '<tr><td colspan="9" style="text-align:center;color:#888">無未結算倉位</td></tr>'

    roi_color = _pnl_color(roi)
    net_color = _pnl_color(net_pnl)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background: #f5f6fa; margin: 0; padding: 20px; }}
  .container {{ max-width: 900px; margin: 0 auto; background: white; border-radius: 12px;
                box-shadow: 0 2px 12px rgba(0,0,0,.1); overflow: hidden; }}
  .header {{ background: linear-gradient(135deg,#1a1a2e,#16213e); color: white;
             padding: 28px 32px; }}
  .header h1 {{ margin: 0; font-size: 22px; }}
  .header p {{ margin: 6px 0 0; opacity: .7; font-size: 13px; }}
  .stats {{ display: flex; gap: 0; border-bottom: 1px solid #eee; }}
  .stat {{ flex: 1; padding: 20px 24px; border-right: 1px solid #eee; }}
  .stat:last-child {{ border-right: none; }}
  .stat-label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: .5px; }}
  .stat-value {{ font-size: 26px; font-weight: 700; margin-top: 4px; }}
  .section {{ padding: 24px 32px; }}
  .section h2 {{ font-size: 15px; margin: 0 0 14px; color: #333; border-left: 3px solid #4a90d9;
                  padding-left: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f8f9fa; padding: 9px 10px; text-align: left; color: #555;
        font-weight: 600; border-bottom: 2px solid #eee; }}
  td {{ padding: 9px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
  .footer {{ background: #f8f9fa; padding: 16px 32px; font-size: 12px; color: #999; }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>📊 Polymarket 模擬交易日報</h1>
    <p>策略：S5 利基市場專項（天氣 / 科學 / 娛樂）｜{now}</p>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">虛擬餘額</div>
      <div class="stat-value" style="color:#2c3e50">${bankroll:.2f}</div>
    </div>
    <div class="stat">
      <div class="stat-label">累積 ROI</div>
      <div class="stat-value" style="color:{roi_color}">{roi:+.1f}%</div>
    </div>
    <div class="stat">
      <div class="stat-label">淨 P&L（扣 API 費）</div>
      <div class="stat-value" style="color:{net_color}">{_fmt_pnl(net_pnl)}</div>
    </div>
    <div class="stat">
      <div class="stat-label">勝率</div>
      <div class="stat-value" style="color:#8e44ad">{wr:.0f}%</div>
    </div>
    <div class="stat">
      <div class="stat-label">Claude API 費用</div>
      <div class="stat-value" style="color:#e67e22">${claude_cost:.3f}</div>
    </div>
  </div>

  <div class="section">
    <h2>🆕 今日新信號（模擬下注）</h2>
    <table>
      <tr>
        <th>日期</th><th>市場問題</th><th>方向</th>
        <th>市場價</th><th>Claude估值</th><th>Edge</th>
        <th>下注金額</th><th>P&L</th><th>狀態</th>
      </tr>
      {new_rows}
    </table>
  </div>

  <div class="section">
    <h2>✅ 今日新結算</h2>
    <table>
      <tr>
        <th>日期</th><th>市場問題</th><th>方向</th>
        <th>市場價</th><th>Claude估值</th><th>Edge</th>
        <th>下注金額</th><th>P&L</th><th>結果</th>
      </tr>
      {resolved_rows}
    </table>
  </div>

  <div class="section">
    <h2>⏳ 未結算倉位（{len(open_trades)} 筆）</h2>
    <table>
      <tr>
        <th>日期</th><th>市場問題</th><th>方向</th>
        <th>市場價</th><th>Claude估值</th><th>Edge</th>
        <th>下注金額</th><th>P&L</th><th>狀態</th>
      </tr>
      {open_rows}
    </table>
  </div>

  <div class="footer">
    本報告為模擬交易，不涉及真實資金。起始虛擬本金 ${start:.2f} USDC。
    累積已結算 {len(resolved_trades)} 筆，未結算 {len(open_trades)} 筆。
    Claude API 累積費用 ${claude_cost:.4f}（從淨 P&L 扣除）。
  </div>

</div>
</body>
</html>"""


def build_subject(state: dict) -> str:
    bankroll = state["virtual_bankroll"]
    start = state["start_bankroll"]
    roi = (bankroll - start) / start * 100
    sign = "📈" if roi >= 0 else "📉"
    date = datetime.now(timezone.utc).strftime("%m/%d")
    return f"{sign} Polymarket 模擬日報 {date} | 餘額 ${bankroll:.2f} | ROI {roi:+.1f}%"
