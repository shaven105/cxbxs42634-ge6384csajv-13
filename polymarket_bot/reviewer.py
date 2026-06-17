"""
Daily strategy reviewer — auto-tunes S5 and S2 Grid parameters.

Flow:
  1. Load trade history (paper_trades.json + grid_trades.json)
  2. Calculate performance metrics per strategy
  3. Parameter sensitivity sweep (simulate past trades with ±N% param shifts)
  4. Optional Claude analysis of findings (REVIEWER_USE_CLAUDE=true)
  5. Send Telegram review report

Run: python reviewer.py
Schedule: GitHub Actions daily at 00:30 UTC (08:30 Taiwan)
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("reviewer")

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
USE_CLAUDE = (
    os.environ.get("REVIEWER_USE_CLAUDE", "false").lower() == "true"
    and bool(_ANTHROPIC_KEY)
    and _ANTHROPIC_KEY != "dummy"
)
# Auto-PR: when True, reviewer pushes a branch and opens a GitHub PR
# with updated config when better parameters are found
AUTO_PR = os.environ.get("REVIEWER_AUTO_PR", "true").lower() == "true"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")  # e.g. shaven105/test1
MIN_RESOLVED_FOR_SWEEP = 5  # need at least N resolved trades to trust the sweep

# ── Data loading ─────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


# ── Metric calculation ────────────────────────────────────────────────────────

@dataclass
class Metrics:
    n_total: int = 0
    n_resolved: int = 0
    n_wins: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    pnl_std: float = 0.0
    sharpe: float = 0.0          # mean/std; annualized only if enough data
    max_drawdown: float = 0.0
    avg_edge: float = 0.0        # S5 only
    avg_holding_days: float = 0.0
    round_trip_rate: float = 0.0  # Grid only


def calc_s5_metrics(trades: list[dict]) -> Metrics:
    m = Metrics()
    m.n_total = len(trades)
    resolved = [t for t in trades if t.get("resolved")]
    m.n_resolved = len(resolved)
    if not resolved:
        return m

    pnls = [t["pnl_usdc"] for t in resolved]
    m.n_wins = sum(1 for p in pnls if p > 0)
    m.win_rate = m.n_wins / len(pnls) * 100
    m.total_pnl = sum(pnls)
    m.avg_pnl = statistics.mean(pnls)
    m.pnl_std = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    m.sharpe = m.avg_pnl / m.pnl_std if m.pnl_std > 0 else 0.0

    # Max drawdown on cumulative P&L curve
    cum = 0.0
    peak = 0.0
    worst_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst_dd = min(worst_dd, cum - peak)
    m.max_drawdown = worst_dd

    edges = [t.get("edge", 0) for t in resolved if t.get("edge") is not None]
    m.avg_edge = statistics.mean(edges) if edges else 0.0

    return m


def calc_grid_metrics(trades: list[dict]) -> Metrics:
    m = Metrics()
    m.n_total = len(trades)
    closed = [t for t in trades if t.get("closed")]
    m.n_resolved = len(closed)
    if not closed:
        return m

    pnls = [t["pnl_usdc"] or 0 for t in closed]
    m.n_wins = sum(1 for p in pnls if p > 0)
    m.win_rate = m.n_wins / len(pnls) * 100
    m.total_pnl = sum(pnls)
    m.avg_pnl = statistics.mean(pnls)
    m.pnl_std = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    m.sharpe = m.avg_pnl / m.pnl_std if m.pnl_std > 0 else 0.0

    cum = 0.0
    peak = 0.0
    worst_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst_dd = min(worst_dd, cum - peak)
    m.max_drawdown = worst_dd

    rt = sum(1 for t in closed if t.get("buy_filled") and t.get("sell_filled"))
    m.round_trip_rate = rt / len(closed) * 100 if closed else 0.0

    return m


# ── S5 parameter sensitivity ─────────────────────────────────────────────────

def s5_threshold_sweep(trades: list[dict]) -> list[dict]:
    """
    For each MISPRICING_THRESHOLD candidate, simulate which resolved trades
    we would have taken and what the win rate / P&L would have been.
    """
    resolved = [t for t in trades if t.get("resolved")]
    if not resolved:
        return []

    results = []
    for thresh in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
        kept = [t for t in resolved if t.get("edge", 0) >= thresh]
        if not kept:
            results.append({"threshold": thresh, "n": 0, "win_rate": 0.0, "total_pnl": 0.0})
            continue
        pnls = [t["pnl_usdc"] for t in kept]
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        results.append({
            "threshold": thresh,
            "n": len(kept),
            "win_rate": round(wr, 1),
            "total_pnl": round(sum(pnls), 4),
            "avg_pnl": round(statistics.mean(pnls), 4),
        })
    return results


def s5_kelly_sweep(trades: list[dict]) -> list[dict]:
    """Simulate different HALF_KELLY_FRACTION values on resolved trades."""
    resolved = [t for t in trades if t.get("resolved") and t.get("edge") and t.get("price")]
    if not resolved:
        return []

    results = []
    for frac in [0.25, 0.33, 0.5, 0.75, 1.0]:
        total_pnl = 0.0
        for t in resolved:
            # Rescale bet by ratio of new_frac / original_frac (original = 0.5)
            scale = frac / 0.5
            pnl = (t["pnl_usdc"] or 0) * scale
            total_pnl += pnl
        results.append({
            "kelly_fraction": frac,
            "total_pnl": round(total_pnl, 4),
            "scale_vs_current": round(frac / 0.5, 2),
        })
    return results


# ── Grid parameter sensitivity ────────────────────────────────────────────────

def grid_offset_sweep(closed_trades: list[dict]) -> list[dict]:
    """
    Simulate GRID_OFFSET variants on past grid trades.
    A larger offset means harder to fill but bigger profit per round-trip.
    """
    if not closed_trades:
        return []

    results = []
    for offset in [0.008, 0.010, 0.012, 0.015, 0.018, 0.020, 0.025]:
        total_pnl = 0.0
        round_trips = 0
        for t in closed_trades:
            # Approximate: if both legs filled, scale P&L by new_offset/old_offset
            if t.get("buy_filled") and t.get("sell_filled"):
                old_offset = 0.015
                scale = offset / old_offset
                total_pnl += (t.get("pnl_usdc") or 0) * scale
                round_trips += 1
            elif t.get("buy_filled") or t.get("sell_filled"):
                # One leg: directional P&L unchanged by offset
                total_pnl += (t.get("pnl_usdc") or 0)
        results.append({
            "offset": offset,
            "round_trips": round_trips,
            "total_pnl": round(total_pnl, 4),
        })
    return results


# ── Auto-PR: push updated config to GitHub ───────────────────────────────────

def _read_config() -> str:
    return Path("config.py").read_text()


def _patch_config(content: str, key: str, new_val: float) -> str:
    """Replace a float assignment line like KEY: float = 0.03 with new_val."""
    import re
    pattern = rf"^({re.escape(key)}\s*:\s*float\s*=\s*)\S+"
    replacement = rf"\g<1>{new_val}"
    return re.sub(pattern, replacement, content, flags=re.MULTILINE)


def _read_grid_strategy() -> str:
    return Path("strategy_grid.py").read_text()


def _patch_grid(content: str, key: str, new_val: float) -> str:
    import re
    pattern = rf"^({re.escape(key)}\s*=\s*)\S+"
    replacement = rf"\g<1>{new_val}"
    return re.sub(pattern, replacement, content, flags=re.MULTILINE)


def maybe_open_pr(
    best_threshold: dict | None,
    best_offset: dict | None,
    current_threshold: float = 0.03,
    current_offset: float = 0.015,
) -> bool:
    """
    If better parameters were found AND differ meaningfully from current,
    push a branch and open a GitHub PR via the API.
    Returns True if a PR was opened.
    """
    if not AUTO_PR or not GITHUB_TOKEN or not GITHUB_REPO:
        return False

    changes: list[tuple[str, str, float, float]] = []  # (file, key, old, new)

    if best_threshold and abs(best_threshold["threshold"] - current_threshold) >= 0.005:
        changes.append(("config", "MISPRICING_THRESHOLD", current_threshold, best_threshold["threshold"]))

    if best_offset and abs(best_offset["offset"] - current_offset) >= 0.002:
        changes.append(("grid", "GRID_OFFSET", current_offset, best_offset["offset"]))

    if not changes:
        logger.info("Auto-PR: parameters already optimal, no PR needed")
        return False

    # Apply patches in memory
    config_src = _read_config()
    grid_src = _read_grid_strategy()
    changed_files = {}

    for (file_, key, old, new) in changes:
        logger.info(f"Auto-PR: {key} {old} → {new}")
        if file_ == "config":
            config_src = _patch_config(config_src, key, new)
            changed_files["polymarket_bot/config.py"] = config_src
        else:
            grid_src = _patch_grid(grid_src, key, new)
            changed_files["polymarket_bot/strategy_grid.py"] = grid_src

    # Push via GitHub API
    import urllib.request, base64
    from datetime import datetime, timezone

    api = f"https://api.github.com/repos/{GITHUB_REPO}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def gh(method: str, path: str, body: dict | None = None):
        url = f"{api}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    try:
        # Get master SHA
        master = gh("GET", "/git/ref/heads/master")
        base_sha = master["object"]["sha"]

        # Create new branch
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        branch = f"auto/param-tune-{date_str}"
        try:
            gh("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        except Exception:
            logger.info(f"Branch {branch} already exists, reusing")

        # Commit each changed file
        commit_sha = base_sha
        for path, content in changed_files.items():
            # Get current file SHA
            try:
                existing = gh("GET", f"/contents/{path}?ref={branch}")
                file_sha = existing["sha"]
            except Exception:
                file_sha = None

            body: dict = {
                "message": f"auto-tune: update {path.split('/')[-1]} parameters",
                "content": base64.b64encode(content.encode()).decode(),
                "branch": branch,
            }
            if file_sha:
                body["sha"] = file_sha
            gh("PUT", f"/contents/{path}", body)

        # Build PR description
        change_lines = "\n".join(
            f"- `{key}`: `{old}` → `{new}`" for (_, key, old, new) in changes
        )
        pr_body = f"""Auto-generated by daily strategy reviewer.

## Parameter changes

{change_lines}

## Why

Based on parameter sensitivity sweep on {sum(1 for _ in changes)} parameters.
Human review required before merging — verify the suggested values make sense
given current market conditions.

> Merge to apply | Close to reject — reviewer will re-evaluate tomorrow.
"""
        pr = gh("POST", "/pulls", {
            "title": f"[Auto] Parameter tune {date_str}",
            "head": branch,
            "base": "master",
            "body": pr_body,
        })
        pr_url = pr.get("html_url", "")
        logger.info(f"Auto-PR opened: {pr_url}")
        return True

    except Exception as exc:
        logger.error(f"Auto-PR failed: {exc}")
        return False


# ── Claude analysis ───────────────────────────────────────────────────────────

def get_claude_recommendations(summary: dict) -> str:
    """
    Call Claude with a concise strategy review summary.
    Returns a short markdown text with recommendations.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()

        prompt = f"""You are a quantitative trading strategy advisor reviewing a Polymarket paper trading bot.

Current strategy performance summary:
{json.dumps(summary, indent=2, ensure_ascii=False)}

Please analyze this and provide:
1. The 2-3 most important parameter changes to improve performance
2. Any methodology issues you notice
3. One new signal filter or idea worth testing

Keep your response concise (under 200 words). Focus on actionable specifics.
Respond in Traditional Chinese (繁體中文)."""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as exc:
        logger.warning(f"Claude analysis failed: {exc}")
        return ""


# ── Report building ───────────────────────────────────────────────────────────

def _best_threshold(sweep: list[dict]) -> dict | None:
    if not sweep:
        return None
    return max((r for r in sweep if r["n"] > 0), key=lambda r: r["total_pnl"], default=None)


def _best_offset(sweep: list[dict]) -> dict | None:
    if not sweep:
        return None
    return max(sweep, key=lambda r: r["total_pnl"], default=None)


def build_review_report(
    s5_m: Metrics,
    grid_m: Metrics,
    thresh_sweep: list[dict],
    kelly_sweep: list[dict],
    offset_sweep: list[dict],
    claude_text: str,
    state: dict,
    pr_opened: bool = False,
) -> str:
    from telegram_reporter import _esc

    now = datetime.now(timezone.utc).strftime("%Y\\-%m\\-%d %H:%M UTC")
    bankroll = state.get("virtual_bankroll", 0)
    start = state.get("start_bankroll", 50)
    roi = (bankroll - start) / start * 100
    roi_arrow = "📈" if roi >= 0 else "📉"

    lines = [
        "*🔬 策略復盤報告*",
        f"_{now}_",
        "",
        f"{'─' * 28}",
        f"💰 餘額　`${bankroll:.2f}` {roi_arrow} `{roi:+.1f}%`",
        "",
        "*S5 策略績效*",
        f"　總倉位：`{s5_m.n_total}` 筆　已結算：`{s5_m.n_resolved}`",
    ]
    if s5_m.n_resolved > 0:
        lines += [
            f"　勝率：`{s5_m.win_rate:.0f}%`　總P&L：`{s5_m.total_pnl:+.4f}`",
            f"　平均P&L：`{s5_m.avg_pnl:+.4f}`　Sharpe：`{s5_m.sharpe:+.3f}`",
            f"　最大回撤：`{s5_m.max_drawdown:.4f}`　平均邊際：`{s5_m.avg_edge:.2%}`",
        ]

    lines += ["", "*S2 網格績效*",
              f"　總倉位：`{grid_m.n_total}` 筆　已結算：`{grid_m.n_resolved}`"]
    if grid_m.n_resolved > 0:
        lines += [
            f"　勝率：`{grid_m.win_rate:.0f}%`　總P&L：`{grid_m.total_pnl:+.4f}`",
            f"　雙腿成交率：`{grid_m.round_trip_rate:.0f}%`　Sharpe：`{grid_m.sharpe:+.3f}`",
        ]

    # Threshold sweep
    if thresh_sweep:
        best = _best_threshold(thresh_sweep)
        current = next((r for r in thresh_sweep if r["threshold"] == 0.03), None)
        lines += ["", "*📊 S5 閾值敏感度*"]
        for r in thresh_sweep:
            marker = " ← 目前" if r["threshold"] == 0.03 else ""
            if r["n"] == 0:
                lines.append(f"　`{r['threshold']:.0%}` → 無交易{marker}")
            else:
                lines.append(
                    f"　`{r['threshold']:.0%}` → {r['n']}筆 WR={r['win_rate']:.0f}% "
                    f"P&L={r['total_pnl']:+.4f}{_esc(marker)}"
                )
        if best and current and best["threshold"] != 0.03:
            lines.append(
                f"　💡 建議閾值：`{best['threshold']:.0%}` "
                f"\\(P&L {best['total_pnl']:+.4f} vs 目前 {current.get('total_pnl',0):+.4f}\\)"
            )

    # Grid offset sweep
    if offset_sweep:
        best_off = _best_offset(offset_sweep)
        lines += ["", "*📊 Grid Offset 敏感度*"]
        for r in offset_sweep:
            marker = " ← 目前" if r["offset"] == 0.015 else ""
            lines.append(
                f"　`{r['offset']:.1%}` → RT={r['round_trips']} P&L={r['total_pnl']:+.4f}{_esc(marker)}"
            )
        if best_off and best_off["offset"] != 0.015:
            lines.append(f"　💡 建議 offset：`{best_off['offset']:.1%}`")

    # Claude text
    if claude_text:
        lines += ["", f"{'─' * 28}", "*🤖 AI 分析建議*", ""]
        for line in claude_text.strip().split("\n"):
            lines.append(_esc(line) if line.strip() else "")

    if pr_opened:
        lines += ["", f"{'─' * 28}", "*🔀 已自動開 PR 建議更新參數*", "_請至 GitHub 審核後 merge_"]
    elif s5_m.n_resolved + grid_m.n_resolved < MIN_RESOLVED_FOR_SWEEP:
        remaining = MIN_RESOLVED_FOR_SWEEP - s5_m.n_resolved - grid_m.n_resolved
        lines.append(f"\n_需再累積 {remaining} 筆已結算交易才開始自動調參_")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=== Daily Strategy Review ===")

    s5_state = _load_json("paper_trades.json")
    grid_state = _load_json("grid_trades.json")

    s5_trades = s5_state.get("trades", [])
    grid_trades = grid_state.get("trades", [])

    logger.info(f"S5 trades loaded: {len(s5_trades)}")
    logger.info(f"Grid trades loaded: {len(grid_trades)}")

    s5_m = calc_s5_metrics(s5_trades)
    grid_m = calc_grid_metrics(grid_trades)

    thresh_sweep = s5_threshold_sweep(s5_trades)
    kelly_sweep = s5_kelly_sweep(s5_trades)
    offset_sweep = grid_offset_sweep(grid_trades)

    logger.info(
        f"S5: {s5_m.n_resolved} resolved | WR={s5_m.win_rate:.0f}% | "
        f"P&L={s5_m.total_pnl:+.4f} | Sharpe={s5_m.sharpe:+.3f}"
    )
    logger.info(
        f"Grid: {grid_m.n_resolved} closed | WR={grid_m.win_rate:.0f}% | "
        f"RT={grid_m.round_trip_rate:.0f}% | P&L={grid_m.total_pnl:+.4f}"
    )

    if thresh_sweep:
        best = _best_threshold(thresh_sweep)
        logger.info(f"Best S5 threshold: {best['threshold']:.0%} → P&L={best['total_pnl']:+.4f}")
    if offset_sweep:
        best_off = _best_offset(offset_sweep)
        logger.info(f"Best grid offset: {best_off['offset']:.1%} → P&L={best_off['total_pnl']:+.4f}")

    # Prepare summary for Claude
    summary = {
        "s5": {
            "n_resolved": s5_m.n_resolved,
            "win_rate_pct": round(s5_m.win_rate, 1),
            "total_pnl": round(s5_m.total_pnl, 4),
            "avg_pnl_per_trade": round(s5_m.avg_pnl, 4),
            "sharpe": round(s5_m.sharpe, 3),
            "max_drawdown": round(s5_m.max_drawdown, 4),
            "avg_edge": round(s5_m.avg_edge, 4),
        },
        "grid": {
            "n_closed": grid_m.n_resolved,
            "win_rate_pct": round(grid_m.win_rate, 1),
            "total_pnl": round(grid_m.total_pnl, 4),
            "round_trip_rate_pct": round(grid_m.round_trip_rate, 1),
            "sharpe": round(grid_m.sharpe, 3),
        },
        "threshold_sweep": thresh_sweep,
        "grid_offset_sweep": offset_sweep,
        "current_params": {
            "MISPRICING_THRESHOLD": 0.03,
            "HALF_KELLY_FRACTION": 0.5,
            "MAX_BET_FRACTION": 0.06,
            "GRID_OFFSET": 0.015,
            "GRID_MAX_SPREAD": 0.20,
            "GRID_STOP_BAND": 0.20,
        },
    }

    claude_text = get_claude_recommendations(summary) if USE_CLAUDE else ""

    # Auto-PR: only trigger when enough resolved data exists
    pr_opened = False
    if s5_m.n_resolved >= MIN_RESOLVED_FOR_SWEEP or grid_m.n_resolved >= MIN_RESOLVED_FOR_SWEEP:
        best_thresh = _best_threshold(thresh_sweep)
        best_off = _best_offset(offset_sweep)
        pr_opened = maybe_open_pr(best_thresh, best_off)
        if pr_opened:
            logger.info("Auto-PR opened with new parameter suggestions")
    else:
        logger.info(
            f"Auto-PR skipped: need {MIN_RESOLVED_FOR_SWEEP} resolved trades "
            f"(S5={s5_m.n_resolved}, Grid={grid_m.n_resolved})"
        )

    # Send Telegram
    from telegram_reporter import send_message
    report = build_review_report(
        s5_m, grid_m, thresh_sweep, kelly_sweep, offset_sweep, claude_text, s5_state,
        pr_opened=pr_opened,
    )
    sent = send_message(report)
    if not sent:
        logger.warning("Telegram send failed — printing report to stdout")
        print(report)


if __name__ == "__main__":
    main()
