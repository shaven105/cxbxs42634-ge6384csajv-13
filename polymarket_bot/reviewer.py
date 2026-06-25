"""
Daily strategy reviewer — auto-tunes Sniper, S5, and Grid parameters.

Flow:
  1. Load sniper_trades.json + paper_trades.json + grid_trades.json
  2. Compute performance metrics per strategy
  3. Parameter sensitivity sweep (simulate past trades with different param values)
  4. Claude (Haiku) outputs structured JSON with exact parameter changes
  5. Auto-apply via GitHub API (commit to new branch, open PR, auto-merge)
  6. Send Telegram report with findings + what was changed

The reviewer runs daily at 00:30 UTC via GitHub Actions.
It is the ONLY place where strategy params are changed — no manual edits needed.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
    os.environ.get("REVIEWER_USE_CLAUDE", "true").lower() == "true"
    and bool(_ANTHROPIC_KEY)
    and _ANTHROPIC_KEY != "dummy"
)
AUTO_PR    = os.environ.get("REVIEWER_AUTO_PR",    "true").lower()  == "true"
AUTO_MERGE = os.environ.get("REVIEWER_AUTO_MERGE", "true").lower()  == "true"
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPOSITORY", "")
# The branch that GH Actions checks out and that auto-PRs should target
DEPLOY_BRANCH = os.environ.get("DEPLOY_BRANCH", "master")

MIN_RESOLVED_FOR_SWEEP = 3   # need at least this many resolved trades to tune

# Maps parameter name → (file_key, repo_path)
_PARAM_FILE: dict[str, tuple[str, str]] = {
    "MISPRICING_THRESHOLD":    ("config",  "polymarket_bot/config.py"),
    "HALF_KELLY_FRACTION":     ("config",  "polymarket_bot/config.py"),
    "MAX_BET_FRACTION":        ("config",  "polymarket_bot/config.py"),
    "CRYPTO_MIN_CONFIDENCE":   ("sniper",  "polymarket_bot/strategy_sniper.py"),
    "SNIPE_CRYPTO_MAX_HOURS":  ("sniper",  "polymarket_bot/strategy_sniper.py"),
    "SNIPE_KELLY_CAP_NEAR":    ("sniper",  "polymarket_bot/strategy_sniper.py"),
    "SNIPE_KELLY_CAP_OVERDUE": ("sniper",  "polymarket_bot/strategy_sniper.py"),
    "SNIPE_MAX_ENTRY":         ("sniper",  "polymarket_bot/strategy_sniper.py"),
    "GRID_OFFSET":             ("grid",    "polymarket_bot/strategy_grid.py"),
}

# Hard safety bounds — Claude cannot push values outside these
_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "MISPRICING_THRESHOLD":    (0.01, 0.15),
    "HALF_KELLY_FRACTION":     (0.25, 1.0),
    "MAX_BET_FRACTION":        (0.03, 0.12),
    "CRYPTO_MIN_CONFIDENCE":   (0.80, 0.97),
    "SNIPE_CRYPTO_MAX_HOURS":  (1.0,  12.0),
    "SNIPE_KELLY_CAP_NEAR":    (0.08, 0.35),
    "SNIPE_KELLY_CAP_OVERDUE": (0.15, 0.50),
    "SNIPE_MAX_ENTRY":         (0.85, 0.97),
    "GRID_OFFSET":             (0.008, 0.030),
}


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


# ── Metrics ──────────────────────────────────────────────────────────────────

@dataclass
class Metrics:
    n_total:          int   = 0
    n_resolved:       int   = 0
    n_wins:           int   = 0
    win_rate:         float = 0.0
    total_pnl:        float = 0.0
    avg_pnl:          float = 0.0
    pnl_std:          float = 0.0
    sharpe:           float = 0.0
    max_drawdown:     float = 0.0
    avg_conf:         float = 0.0   # sniper only
    round_trip_rate:  float = 0.0   # grid only


def _base_metrics(pnls: list[float]) -> Metrics:
    m = Metrics(n_resolved=len(pnls))
    if not pnls:
        return m
    m.n_wins    = sum(1 for p in pnls if p > 0)
    m.win_rate  = m.n_wins / len(pnls) * 100
    m.total_pnl = sum(pnls)
    m.avg_pnl   = statistics.mean(pnls)
    m.pnl_std   = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    m.sharpe    = m.avg_pnl / m.pnl_std if m.pnl_std > 0 else 0.0
    cum = peak = worst = 0.0
    for p in pnls:
        cum  += p
        peak  = max(peak, cum)
        worst = min(worst, cum - peak)
    m.max_drawdown = worst
    return m


def calc_sniper_metrics(trades: list[dict]) -> Metrics:
    m          = _base_metrics([t.get("pnl_usdc", 0) for t in trades if t.get("resolved")])
    m.n_total  = len(trades)
    confs      = [t.get("confidence", 0) for t in trades if t.get("resolved")]
    m.avg_conf = statistics.mean(confs) if confs else 0.0
    return m


def calc_s5_metrics(trades: list[dict]) -> Metrics:
    resolved = [t for t in trades if t.get("resolved")]
    m        = _base_metrics([t["pnl_usdc"] for t in resolved])
    m.n_total = len(trades)
    return m


def calc_grid_metrics(trades: list[dict]) -> Metrics:
    closed = [t for t in trades if t.get("closed")]
    m      = _base_metrics([t.get("pnl_usdc") or 0 for t in closed])
    m.n_total = len(trades)
    rt = sum(1 for t in closed if t.get("buy_filled") and t.get("sell_filled"))
    m.round_trip_rate = rt / len(closed) * 100 if closed else 0.0
    return m


# ── Parameter sweeps ──────────────────────────────────────────────────────────

def sniper_conf_sweep(trades: list[dict]) -> list[dict]:
    resolved = [t for t in trades if t.get("resolved")]
    out = []
    for thresh in [0.80, 0.85, 0.90, 0.92, 0.95, 0.97]:
        kept = [t for t in resolved if t.get("confidence", 0) >= thresh]
        if not kept:
            out.append({"min_confidence": thresh, "n": 0, "win_rate": 0.0, "total_pnl": 0.0})
            continue
        pnls = [t.get("pnl_usdc", 0) for t in kept]
        wr   = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        out.append({
            "min_confidence": thresh,
            "n":          len(kept),
            "win_rate":   round(wr, 1),
            "total_pnl":  round(sum(pnls), 4),
        })
    return out


def sniper_hours_sweep(trades: list[dict]) -> list[dict]:
    resolved = [t for t in trades if t.get("resolved")]
    out = []
    for max_h in [2, 4, 6, 8, 12]:
        kept = [t for t in resolved if t.get("hours_to_expiry", 999) <= max_h]
        if not kept:
            out.append({"max_hours": max_h, "n": 0, "win_rate": 0.0, "total_pnl": 0.0})
            continue
        pnls = [t.get("pnl_usdc", 0) for t in kept]
        wr   = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        out.append({
            "max_hours":  max_h,
            "n":          len(kept),
            "win_rate":   round(wr, 1),
            "total_pnl":  round(sum(pnls), 4),
        })
    return out


def s5_threshold_sweep(trades: list[dict]) -> list[dict]:
    resolved = [t for t in trades if t.get("resolved")]
    results  = []
    for thresh in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
        kept = [t for t in resolved if t.get("edge", 0) >= thresh]
        if not kept:
            results.append({"threshold": thresh, "n": 0, "win_rate": 0.0, "total_pnl": 0.0})
            continue
        pnls = [t["pnl_usdc"] for t in kept]
        wr   = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        results.append({
            "threshold": thresh, "n": len(kept),
            "win_rate":  round(wr, 1),
            "total_pnl": round(sum(pnls), 4),
            "avg_pnl":   round(statistics.mean(pnls), 4),
        })
    return results


def grid_offset_sweep(closed_trades: list[dict]) -> list[dict]:
    results = []
    for offset in [0.008, 0.010, 0.012, 0.015, 0.018, 0.020, 0.025]:
        total = 0.0
        rt    = 0
        for t in closed_trades:
            if t.get("buy_filled") and t.get("sell_filled"):
                total += (t.get("pnl_usdc") or 0) * offset / 0.015
                rt    += 1
            elif t.get("buy_filled") or t.get("sell_filled"):
                total += (t.get("pnl_usdc") or 0)
        results.append({"offset": offset, "round_trips": rt, "total_pnl": round(total, 4)})
    return results


# ── Claude structured recommendation ─────────────────────────────────────────

def get_claude_param_changes(summary: dict) -> tuple[dict, str]:
    """
    Ask Claude Haiku to output JSON with exact parameter changes.
    Returns (param_changes: dict, observation_text: str).
    """
    if not USE_CLAUDE:
        return {}, ""
    try:
        import anthropic
        client = anthropic.Anthropic()

        bounds_desc = "\n".join(
            f"  {k}: [{lo}, {hi}]" for k, (lo, hi) in _PARAM_BOUNDS.items()
        )
        prompt = f"""你是量化交易策略自動調參系統。分析以下績效數據並輸出調整建議。

績效數據：
{json.dumps(summary, indent=2, ensure_ascii=False)}

調整規則：
1. 已解倉筆數 < 3：param_changes 必須回傳 {{}}（資料不足）
2. 每次最多調整 2 個參數
3. 每個參數單次調整幅度不超過目前值的 20%
4. 參數允許範圍：
{bounds_desc}

輸出 ONLY 以下 JSON 格式（不要加任何其他文字）：
{{
  "param_changes": {{}},
  "observation": "一兩句觀察（繁體中文）",
  "reasoning": "一兩句調整原因（繁體中文，若無改動則說明原因）"
}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data    = json.loads(raw[start:end])
            changes = data.get("param_changes") or {}
            obs     = f"{data.get('observation', '')} {data.get('reasoning', '')}".strip()
            # Enforce safety bounds
            safe = {}
            for k, v in changes.items():
                if k not in _PARAM_BOUNDS:
                    logger.warning(f"Claude suggested unknown param {k!r} — ignoring")
                    continue
                lo, hi = _PARAM_BOUNDS[k]
                clamped = max(lo, min(hi, float(v)))
                safe[k] = clamped
                if clamped != float(v):
                    logger.warning(f"Clamped {k}: {v} → {clamped}")
            return safe, obs
    except Exception as exc:
        logger.warning(f"Claude structured call failed: {exc}")
    return {}, ""


# ── File patching helpers ──────────────────────────────────────────────────────

def _patch_file(content: str, key: str, new_val: float) -> str:
    """Replace the first occurrence of `KEY = <value>` or `KEY: <type> = <value>`."""
    # Match both annotated (config.py) and plain (sniper/grid) styles
    pattern = rf"^({re.escape(key)}(?:\s*:\s*\S+)?\s*=\s*)(\S+)"
    # Format as int if the new value is a whole number and param name hints int
    fmt_val = int(new_val) if new_val == int(new_val) and "HOURS" in key else new_val
    replacement = rf"\g<1>{fmt_val}"
    return re.sub(pattern, replacement, content, flags=re.MULTILINE, count=1)


# ── Auto-PR: push updated params via GitHub API ───────────────────────────────

def apply_param_changes(param_changes: dict) -> tuple[bool, str]:
    """
    Commit changed param files to a new branch and open (+ optionally merge) a PR
    against DEPLOY_BRANCH.  Returns (pr_opened: bool, pr_url: str).
    """
    if not AUTO_PR or not GITHUB_TOKEN or not GITHUB_REPO or not param_changes:
        return False, ""

    import urllib.request, base64

    api     = f"https://api.github.com/repos/{GITHUB_REPO}"
    headers = {
        "Authorization":        f"Bearer {GITHUB_TOKEN}",
        "Accept":               "application/vnd.github+json",
        "Content-Type":         "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def gh(method: str, path: str, body: dict | None = None):
        url  = f"{api}{path}"
        data = json.dumps(body).encode() if body else None
        req  = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    try:
        # Resolve base branch SHA
        base_sha = gh("GET", f"/git/ref/heads/{DEPLOY_BRANCH}")["object"]["sha"]

        # Create tuning branch
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        branch   = f"auto/param-tune-{date_str}"
        try:
            gh("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        except Exception:
            logger.info(f"Branch {branch} may already exist")

        # Group changes by file
        file_changes: dict[str, list[tuple[str, float]]] = {}
        for param, val in param_changes.items():
            _, repo_path = _PARAM_FILE[param]
            file_changes.setdefault(repo_path, []).append((param, val))

        # Fetch, patch, and commit each file
        for repo_path, changes in file_changes.items():
            existing = gh("GET", f"/contents/{repo_path}?ref={DEPLOY_BRANCH}")
            content  = base64.b64decode(existing["content"]).decode()
            for param, val in changes:
                content = _patch_file(content, param, val)
                logger.info(f"Patched {param} → {val} in {repo_path}")
            change_names = ", ".join(f"{p}={v}" for p, v in changes)
            gh("PUT", f"/contents/{repo_path}", {
                "message": f"auto-tune: {change_names}",
                "content": base64.b64encode(content.encode()).decode(),
                "branch":  branch,
                "sha":     existing["sha"],
            })

        # Build PR body
        change_lines = "\n".join(
            f"- `{p}`: → `{v}`" for p, v in param_changes.items()
        )
        pr = gh("POST", "/pulls", {
            "title": f"[Auto-tune] {date_str}",
            "head":  branch,
            "base":  DEPLOY_BRANCH,
            "body":  f"Daily strategy reviewer parameter update.\n\n## Changes\n{change_lines}\n",
        })
        pr_url    = pr.get("html_url", "")
        pr_number = pr.get("number")
        logger.info(f"Auto-PR opened: {pr_url}")

        if AUTO_MERGE and pr_number:
            try:
                gh("PUT", f"/pulls/{pr_number}/merge", {
                    "commit_title":   f"[Auto-merge] param-tune {date_str}",
                    "commit_message": change_lines,
                    "merge_method":   "squash",
                })
                logger.info("Auto-merge complete")
            except Exception as exc:
                logger.warning(f"Auto-merge failed (PR still open): {exc}")

        return True, pr_url

    except Exception as exc:
        logger.error(f"apply_param_changes failed: {exc}")
        return False, ""


# ── Telegram report ───────────────────────────────────────────────────────────

def build_review_report(
    sniper_m:    Metrics,
    s5_m:        Metrics,
    grid_m:      Metrics,
    conf_sweep:  list[dict],
    hours_sweep: list[dict],
    param_changes: dict,
    observation: str,
    state:       dict,
    pr_opened:   bool = False,
    pr_url:      str  = "",
) -> str:
    from telegram_reporter import _esc

    now      = datetime.now(timezone.utc).strftime("%Y\\-%m\\-%d %H:%M UTC")
    bankroll = state.get("virtual_bankroll", 0)
    start    = state.get("start_bankroll", 100)
    roi      = (bankroll - start) / start * 100
    arrow    = "📈" if roi >= 0 else "📉"

    lines = [
        "*🔬 每日策略復盤*",
        f"_{now}_",
        f"{'─' * 28}",
        f"💰 餘額 `${bankroll:.2f}` {arrow} `{roi:+.1f}%`",
        "",
    ]

    # Sniper (primary active strategy)
    lines += ["*S3 Sniper 績效*",
              f"　倉位：`{sniper_m.n_total}` 筆　已結算：`{sniper_m.n_resolved}`"]
    if sniper_m.n_resolved > 0:
        lines += [
            f"　勝率：`{sniper_m.win_rate:.0f}%`　總P&L：`{sniper_m.total_pnl:+.4f}`",
            f"　平均P&L：`{sniper_m.avg_pnl:+.4f}`　Sharpe：`{sniper_m.sharpe:+.3f}`",
            f"　平均信心值：`{sniper_m.avg_conf:.3f}`",
        ]
    else:
        lines.append("　_尚無已結算倉位_")

    if conf_sweep and any(r["n"] > 0 for r in conf_sweep):
        lines += ["", "*📊 信心值閾值敏感度*"]
        for r in conf_sweep:
            marker = " ← 目前" if abs(r["min_confidence"] - 0.90) < 0.001 else ""
            if r["n"] == 0:
                lines.append(f"　`{r['min_confidence']:.2f}` → 無交易{marker}")
            else:
                lines.append(
                    f"　`{r['min_confidence']:.2f}` → {r['n']}筆 "
                    f"WR={r['win_rate']:.0f}% P&L={r['total_pnl']:+.4f}{_esc(marker)}"
                )

    # S5 & Grid (suspended but still tracked)
    lines += ["", "*S5/Grid 累積*",
              f"　S5={s5_m.n_resolved}筆結算 P&L={s5_m.total_pnl:+.4f}",
              f"　Grid={grid_m.n_resolved}筆結算 P&L={grid_m.total_pnl:+.4f}"]

    # AI observation
    if observation:
        lines += ["", f"{'─' * 28}", "*🤖 AI 分析*"]
        for line in observation.strip().split("\n"):
            lines.append(_esc(line) if line.strip() else "")

    # Param changes
    if param_changes:
        lines += ["", f"{'─' * 28}", "*⚙️ 自動調參*"]
        for p, v in param_changes.items():
            lines.append(f"　`{_esc(p)}` → `{v}`")
        if pr_opened:
            if AUTO_MERGE:
                lines.append("✅ 已 auto\\-merge 至部署分支")
            else:
                lines.append(f"🔀 PR 已開，請審核後 merge")
        if pr_url:
            lines.append(f"[GitHub PR]({pr_url})")
    else:
        lines += ["", "_本日無參數調整_"]
        if sniper_m.n_resolved < MIN_RESOLVED_FOR_SWEEP:
            remain = MIN_RESOLVED_FOR_SWEEP - sniper_m.n_resolved
            lines.append(f"_再累積 {remain} 筆解倉才啟動自動調參_")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=== Daily Strategy Review ===")
    logger.info(f"USE_CLAUDE={USE_CLAUDE}  AUTO_PR={AUTO_PR}  AUTO_MERGE={AUTO_MERGE}  DEPLOY_BRANCH={DEPLOY_BRANCH}")

    sniper_state = _load_json("sniper_trades.json")
    s5_state     = _load_json("paper_trades.json")
    grid_state   = _load_json("grid_trades.json")

    sniper_trades = sniper_state.get("trades", [])
    s5_trades     = s5_state.get("trades", [])
    grid_trades   = grid_state.get("trades", [])

    logger.info(f"Sniper trades: {len(sniper_trades)}")
    logger.info(f"S5 trades: {len(s5_trades)}")
    logger.info(f"Grid trades: {len(grid_trades)}")

    sniper_m = calc_sniper_metrics(sniper_trades)
    s5_m     = calc_s5_metrics(s5_trades)
    grid_m   = calc_grid_metrics(grid_trades)

    conf_sweep  = sniper_conf_sweep(sniper_trades)
    hours_sweep = sniper_hours_sweep(sniper_trades)
    s5_sweep    = s5_threshold_sweep(s5_trades)

    logger.info(
        f"Sniper: {sniper_m.n_resolved} resolved | WR={sniper_m.win_rate:.0f}% | "
        f"P&L={sniper_m.total_pnl:+.4f} | conf={sniper_m.avg_conf:.3f}"
    )

    # Build Claude summary
    summary = {
        "sniper": {
            "n_resolved":           sniper_m.n_resolved,
            "n_open":               sniper_m.n_total - sniper_m.n_resolved,
            "win_rate_pct":         round(sniper_m.win_rate, 1),
            "total_pnl":            round(sniper_m.total_pnl, 4),
            "avg_pnl_per_trade":    round(sniper_m.avg_pnl, 4),
            "sharpe":               round(sniper_m.sharpe, 3),
            "avg_confidence":       round(sniper_m.avg_conf, 3),
            "conf_threshold_sweep": conf_sweep,
            "hours_entry_sweep":    hours_sweep,
        },
        "s5":   {"n_resolved": s5_m.n_resolved,   "total_pnl": round(s5_m.total_pnl, 4)},
        "grid": {"n_resolved": grid_m.n_resolved, "total_pnl": round(grid_m.total_pnl, 4)},
        "current_params": {
            "CRYPTO_MIN_CONFIDENCE":   0.90,
            "SNIPE_CRYPTO_MAX_HOURS":  6,
            "SNIPE_KELLY_CAP_NEAR":    0.20,
            "SNIPE_KELLY_CAP_OVERDUE": 0.35,
            "SNIPE_MAX_ENTRY":         0.95,
            "MISPRICING_THRESHOLD":    0.03,
            "GRID_OFFSET":             0.015,
        },
    }

    param_changes, observation = get_claude_param_changes(summary)
    logger.info(f"Claude suggests: {param_changes}")
    logger.info(f"Claude observation: {observation}")

    # Only apply if we have enough data
    pr_opened, pr_url = False, ""
    if sniper_m.n_resolved >= MIN_RESOLVED_FOR_SWEEP and param_changes:
        pr_opened, pr_url = apply_param_changes(param_changes)
    elif param_changes:
        logger.info(
            f"Skipping auto-tune: need {MIN_RESOLVED_FOR_SWEEP} resolved trades "
            f"(have {sniper_m.n_resolved})"
        )
        param_changes = {}   # don't claim in report we changed something we didn't

    # Send Telegram
    from telegram_reporter import send_message
    report = build_review_report(
        sniper_m, s5_m, grid_m,
        conf_sweep, hours_sweep,
        param_changes, observation,
        s5_state,
        pr_opened=pr_opened,
        pr_url=pr_url,
    )
    sent = send_message(report)
    if not sent:
        logger.warning("Telegram send failed — printing to stdout")
        print(report)


if __name__ == "__main__":
    main()
