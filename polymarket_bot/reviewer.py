"""
Daily strategy reviewer + auto-healer.

Each morning at 00:30 UTC (08:30 Taiwan) this script:

  1. Loads trade history + reviewer metadata (persistent in sniper_trades.json)
  2. Computes Sniper / S5 / Grid performance metrics
  3. Counts consecutive review cycles with zero new signals (no_signal_streak)
  4. Decides which escalation tier to trigger:

     ─ Tier 0: Enough trades + decent EV
         → Claude Haiku tunes parameters (confidence, hours window, Kelly cap)

     ─ Tier 1: no_signal_streak ≥ 3  OR  slow param-only period
         → Claude Haiku relaxes parameters more aggressively

     ─ Tier 2: no_signal_streak ≥ 7  OR  n_resolved ≥ 10 AND win_rate < 45%
         → Claude Sonnet reads live market sample + strategy code
         → Returns code-level fixes (regex, logic, new signal patterns)
         → Auto-committed + auto-merged to DEPLOY_BRANCH

  5. Saves updated state (including metadata) to disk
  6. Sends Telegram report summarising metrics + what was changed

APIs used (no key needed): Polymarket Gamma, CoinGecko, ESPN
APIs with key:  Anthropic (tiny cost ~$0.002/day), Telegram (free)
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("reviewer")

# ── Environment ──────────────────────────────────────────────────────────────

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
USE_CLAUDE  = (
    os.environ.get("REVIEWER_USE_CLAUDE", "true").lower() == "true"
    and bool(_ANTHROPIC_KEY)
    and _ANTHROPIC_KEY != "dummy"
)
AUTO_PR     = os.environ.get("REVIEWER_AUTO_PR",    "true").lower() == "true"
AUTO_MERGE  = os.environ.get("REVIEWER_AUTO_MERGE", "true").lower() == "true"
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPOSITORY", "")
DEPLOY_BRANCH = os.environ.get("DEPLOY_BRANCH", "master")

# ── Escalation thresholds ─────────────────────────────────────────────────────

NO_SIGNAL_RELAX_CYCLES = 3   # param relaxation after this many zero-signal reviews
NO_SIGNAL_CODE_CYCLES  = 7   # code-level fix after this many zero-signal reviews
BAD_EV_MIN_TRADES      = 10  # need at least N resolved before judging EV
BAD_EV_WIN_RATE        = 45.0  # % WR below this is "bad"
MIN_TRADES_FOR_TUNE    = 3   # param tune requires at least this many resolved

# ── Parameter safety bounds (Claude cannot escape these) ─────────────────────

_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "MISPRICING_THRESHOLD":    (0.01, 0.15),
    "HALF_KELLY_FRACTION":     (0.25, 1.0),
    "MAX_BET_FRACTION":        (0.03, 0.12),
    "CRYPTO_MIN_CONFIDENCE":   (0.75, 0.97),
    "SNIPE_CRYPTO_MAX_HOURS":  (1.0,  24.0),
    "SNIPE_KELLY_CAP_NEAR":    (0.08, 0.40),
    "SNIPE_KELLY_CAP_OVERDUE": (0.15, 0.50),
    "SNIPE_MAX_ENTRY":         (0.80, 0.97),
    "GRID_OFFSET":             (0.008, 0.030),
}

# Param → (file_key, repo_path) for GitHub API commits
_PARAM_FILE: dict[str, tuple[str, str]] = {
    "MISPRICING_THRESHOLD":    ("config", "polymarket_bot/config.py"),
    "HALF_KELLY_FRACTION":     ("config", "polymarket_bot/config.py"),
    "MAX_BET_FRACTION":        ("config", "polymarket_bot/config.py"),
    "CRYPTO_MIN_CONFIDENCE":   ("sniper", "polymarket_bot/strategy_sniper.py"),
    "SNIPE_CRYPTO_MAX_HOURS":  ("sniper", "polymarket_bot/strategy_sniper.py"),
    "SNIPE_KELLY_CAP_NEAR":    ("sniper", "polymarket_bot/strategy_sniper.py"),
    "SNIPE_KELLY_CAP_OVERDUE": ("sniper", "polymarket_bot/strategy_sniper.py"),
    "SNIPE_MAX_ENTRY":         ("sniper", "polymarket_bot/strategy_sniper.py"),
    "GRID_OFFSET":             ("grid",   "polymarket_bot/strategy_grid.py"),
}


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


# ── Metrics ───────────────────────────────────────────────────────────────────

@dataclass
class Metrics:
    n_total:         int   = 0
    n_resolved:      int   = 0
    n_wins:          int   = 0
    win_rate:        float = 0.0
    total_pnl:       float = 0.0
    avg_pnl:         float = 0.0
    pnl_std:         float = 0.0
    sharpe:          float = 0.0
    max_drawdown:    float = 0.0
    avg_conf:        float = 0.0
    round_trip_rate: float = 0.0


def _base_metrics(pnls: list[float]) -> Metrics:
    m = Metrics(n_resolved=len(pnls))
    if not pnls:
        return m
    m.n_wins     = sum(1 for p in pnls if p > 0)
    m.win_rate   = m.n_wins / len(pnls) * 100
    m.total_pnl  = sum(pnls)
    m.avg_pnl    = statistics.mean(pnls)
    m.pnl_std    = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    m.sharpe     = m.avg_pnl / m.pnl_std if m.pnl_std > 0 else 0.0
    cum = peak = worst = 0.0
    for p in pnls:
        cum  += p; peak = max(peak, cum)
        worst = min(worst, cum - peak)
    m.max_drawdown = worst
    return m


def calc_sniper_metrics(trades: list[dict]) -> Metrics:
    resolved = [t for t in trades if t.get("resolved")]
    m        = _base_metrics([t.get("pnl_usdc", 0) for t in resolved])
    m.n_total = len(trades)
    confs     = [t.get("confidence", 0) for t in resolved]
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


# ── Streak tracking ───────────────────────────────────────────────────────────

def update_no_signal_streak(sniper_state: dict, sniper_trades: list[dict]) -> int:
    """
    Increment the no-signal counter if no new trades were entered since last review.
    Resets to 0 if new trades appeared.  Persists in sniper_state['_review_meta'].
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta  = sniper_state.setdefault("_review_meta", {})

    last_review = meta.get("last_review_date")
    if last_review:
        new_since = [t for t in sniper_trades if t.get("date", "") > last_review]
    else:
        new_since = []  # first ever review → treat as no new data yet

    streak = 0 if new_since else meta.get("no_signal_streak", 0) + 1

    meta["last_review_date"] = today
    meta["no_signal_streak"] = streak
    meta["total_reviews"]    = meta.get("total_reviews", 0) + 1
    return streak


# ── Sweeps ────────────────────────────────────────────────────────────────────

def sniper_conf_sweep(trades: list[dict]) -> list[dict]:
    resolved = [t for t in trades if t.get("resolved")]
    out = []
    for thresh in [0.80, 0.85, 0.88, 0.90, 0.92, 0.95]:
        kept = [t for t in resolved if t.get("confidence", 0) >= thresh]
        if not kept:
            out.append({"min_confidence": thresh, "n": 0, "win_rate": 0.0, "total_pnl": 0.0})
            continue
        pnls = [t.get("pnl_usdc", 0) for t in kept]
        out.append({
            "min_confidence": thresh,
            "n":          len(kept),
            "win_rate":   round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
            "total_pnl":  round(sum(pnls), 4),
        })
    return out


def s5_threshold_sweep(trades: list[dict]) -> list[dict]:
    resolved = [t for t in trades if t.get("resolved")]
    out = []
    for thresh in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
        kept = [t for t in resolved if t.get("edge", 0) >= thresh]
        if not kept:
            out.append({"threshold": thresh, "n": 0, "win_rate": 0.0, "total_pnl": 0.0})
            continue
        pnls = [t["pnl_usdc"] for t in kept]
        out.append({
            "threshold": thresh, "n": len(kept),
            "win_rate":  round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
            "total_pnl": round(sum(pnls), 4),
        })
    return out


# ── Live market sampling (for code-fix mode) ──────────────────────────────────

def _fetch_sample_markets(n: int = 15) -> list[dict]:
    """Fetch N newest active markets to show Claude what titles/formats look like."""
    import urllib.request
    try:
        url = (
            "https://gamma-api.polymarket.com/markets"
            "?active=true&closed=false&limit=20&order=id&ascending=false"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/reviewer"})
        with urllib.request.urlopen(req, timeout=10) as r:
            markets = json.loads(r.read())
        return [
            {"id": m.get("id"), "question": m.get("question", ""), "endDate": m.get("endDate", "")}
            for m in markets[:n]
        ]
    except Exception as exc:
        logger.warning(f"Sample market fetch failed: {exc}")
        return []


def _read_strategy_excerpt() -> str:
    """Read the core signal-detection code from strategy_sniper.py."""
    path = Path("strategy_sniper.py")
    if not path.exists():
        return ""
    lines = path.read_text().split("\n")
    # Extract lines that contain key signal logic
    keep_sections: list[str] = []
    include = False
    depth   = 0
    for i, line in enumerate(lines):
        triggers = [
            "CRYPTO_MIN_CONFIDENCE", "SNIPE_CRYPTO_MAX_HOURS", "_PRICE_RE",
            "_ABOVE_RE", "_BELOW_RE", "CRYPTO_MAP", "_CRYPTO_VOL_ANNUAL",
            "def _parse_crypto_question", "def _verify_crypto_outcome",
            "def _crypto_confidence", "_norm_cdf", "def find_sniper_candidates",
            "SNIPE_OVERDUE_GRACE", "SNIPE_KELLY_CAP",
        ]
        if any(t in line for t in triggers):
            # Include surrounding context
            start = max(0, i - 1)
            keep_sections.append(f"# --- line {start+1} ---")
            keep_sections.extend(lines[start : min(len(lines), i + 25)])
            keep_sections.append("")
    return "\n".join(keep_sections)[:6000]  # cap to avoid huge prompts


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _gh_client():
    """Return a simple GitHub API callable."""
    import urllib.request, base64 as _b64
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

    return gh, _b64


def _open_and_merge_pr(gh, branch: str, title: str, body: str) -> str:
    """Open a PR from `branch` → DEPLOY_BRANCH, optionally merge. Returns PR URL."""
    pr  = gh("POST", "/pulls", {"title": title, "head": branch, "base": DEPLOY_BRANCH, "body": body})
    url = pr.get("html_url", "")
    num = pr.get("number")
    if AUTO_MERGE and num:
        try:
            gh("PUT", f"/pulls/{num}/merge", {
                "commit_title": title, "merge_method": "squash",
            })
            logger.info(f"Auto-merged PR #{num}")
        except Exception as exc:
            logger.warning(f"Auto-merge failed: {exc}")
    return url


# ── File patching ─────────────────────────────────────────────────────────────

def _patch_param(content: str, key: str, new_val: float) -> str:
    """Replace `KEY = <old>` or `KEY: type = <old>` with new_val."""
    fmt = int(new_val) if new_val == int(new_val) and "HOURS" in key else new_val
    pattern = rf"^({re.escape(key)}(?:\s*:\s*\S+)?\s*=\s*)(\S+)"
    return re.sub(pattern, rf"\g<1>{fmt}", content, flags=re.MULTILINE, count=1)


# ── Claude Tier 0/1: parameter tune (Haiku) ──────────────────────────────────

def claude_param_tune(summary: dict, aggressive: bool = False) -> tuple[dict, str]:
    """
    Ask Claude Haiku for parameter changes.
    aggressive=True when no_signal_streak >= NO_SIGNAL_RELAX_CYCLES:
    Claude is instructed to be more permissive (lower thresholds, wider windows).
    Returns (param_changes: dict, observation: str).
    """
    if not USE_CLAUDE:
        return {}, ""
    try:
        import anthropic
        client = anthropic.Anthropic()

        mode_note = (
            "⚠️ 系統已連續多個週期沒有產生任何交易信號。你必須降低門檻（CRYPTO_MIN_CONFIDENCE 降至少 0.03，"
            "或 SNIPE_CRYPTO_MAX_HOURS 提高至少 2 小時）以恢復信號生成。"
            if aggressive else
            "根據績效數據給出最合理的調整。"
        )
        bounds_desc = "\n".join(f"  {k}: [{lo}, {hi}]" for k, (lo, hi) in _PARAM_BOUNDS.items())

        prompt = f"""你是量化交易策略自動調參系統。{mode_note}

績效數據：
{json.dumps(summary, indent=2, ensure_ascii=False)}

參數允許範圍：
{bounds_desc}

規則：
- 每次最多調整 2 個參數
- 單次調整幅度不超過目前值的 25%
- {'aggressive 模式：必須調整至少 1 個參數以放寬限制' if aggressive else '若已解倉 < 3 筆，param_changes 回傳 {}'}

輸出 ONLY 以下 JSON（不要加任何其他文字）：
{{"param_changes": {{}}, "observation": "一兩句繁體中文觀察", "reasoning": "一兩句繁體中文理由"}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        d   = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        raw_changes = d.get("param_changes") or {}
        obs = f"{d.get('observation', '')} {d.get('reasoning', '')}".strip()
        safe = {
            k: max(lo, min(hi, float(v)))
            for k, v in raw_changes.items()
            if k in _PARAM_BOUNDS
            for lo, hi in [_PARAM_BOUNDS[k]]
        }
        return safe, obs
    except Exception as exc:
        logger.warning(f"claude_param_tune failed: {exc}")
        return {}, ""


# ── Claude Tier 2: code-level strategy fix (Sonnet) ──────────────────────────

def claude_code_fix(no_signal_streak: int, bad_ev: bool) -> tuple[list[dict], str]:
    """
    Ask Claude Sonnet to diagnose why the strategy isn't generating signals
    (or is generating losing trades) and return code-level text replacements.
    Returns (fixes: list[{repo_path, old_text, new_text}], diagnosis: str).
    """
    if not USE_CLAUDE:
        return [], ""

    reasons = []
    if no_signal_streak >= NO_SIGNAL_CODE_CYCLES:
        reasons.append(f"連續 {no_signal_streak} 次每日審查都沒有任何交易信號")
    if bad_ev:
        reasons.append("已結算交易勝率低於 45%，策略存在根本問題")

    sample_markets = _fetch_sample_markets(15)
    code_excerpt   = _read_strategy_excerpt()

    if not code_excerpt:
        logger.warning("Could not read strategy code for Sonnet fix")
        return [], ""

    prompt = f"""你是 Polymarket 量化交易機器人的資深工程師，負責自動修復策略問題。

問題：
{chr(10).join(f'- {r}' for r in reasons)}

目前掃描到的最新 Polymarket 市場（用來判斷問題格式是否符合正則）：
{json.dumps(sample_markets, indent=2, ensure_ascii=False)}

策略核心程式碼摘錄（strategy_sniper.py）：
{code_excerpt}

分析步驟：
1. 對照市場問題（question 欄位），判斷 _ABOVE_RE / _BELOW_RE / _PRICE_RE 正則能否匹配
2. 判斷 CRYPTO_MIN_CONFIDENCE 是否太高（目前 0.90）
3. 判斷 SNIPE_CRYPTO_MAX_HOURS 是否太窄（目前 6 小時）
4. 如果問題格式完全不符合，考慮擴充 _ABOVE_RE / _BELOW_RE 的關鍵字
5. 最多提出 3 個修改

約束：
- old_text 必須是程式碼中實際存在的連續幾行（字串替換）
- 保持 Python 語法正確
- 不能引入未在原檔 import 的模組
- 修改後的程式碼必須比原來更能產生信號（不能讓條件更嚴格）

輸出 ONLY 以下 JSON（不要加任何其他文字）：
{{
  "diagnosis": "診斷說明（2-3句繁體中文）",
  "fixes": [
    {{
      "repo_path": "polymarket_bot/strategy_sniper.py",
      "old_text": "原始程式碼片段（完整連續幾行，含縮排）",
      "new_text": "替換後的程式碼（保持相同縮排）"
    }}
  ]
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw  = msg.content[0].text.strip()
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        return data.get("fixes") or [], data.get("diagnosis", "")
    except Exception as exc:
        logger.warning(f"claude_code_fix (Sonnet) failed: {exc}")
        return [], ""


# ── Apply param changes via GitHub API ────────────────────────────────────────

def apply_param_changes(param_changes: dict) -> tuple[bool, str]:
    if not AUTO_PR or not GITHUB_TOKEN or not GITHUB_REPO or not param_changes:
        return False, ""
    try:
        gh, b64 = _gh_client()
        base_sha = gh("GET", f"/git/ref/heads/{DEPLOY_BRANCH}")["object"]["sha"]
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        branch   = f"auto/param-tune-{date_str}"
        try:
            gh("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        except Exception:
            pass

        # Group by file
        by_file: dict[str, list] = {}
        for param, val in param_changes.items():
            _, repo_path = _PARAM_FILE[param]
            by_file.setdefault(repo_path, []).append((param, val))

        for repo_path, changes in by_file.items():
            existing = gh("GET", f"/contents/{repo_path}?ref={DEPLOY_BRANCH}")
            content  = b64.b64decode(existing["content"]).decode()
            for param, val in changes:
                content = _patch_param(content, param, val)
                logger.info(f"Patched {param} → {val}")
            change_desc = ", ".join(f"{p}={v}" for p, v in changes)
            gh("PUT", f"/contents/{repo_path}", {
                "message": f"auto-tune: {change_desc}",
                "content": b64.b64encode(content.encode()).decode(),
                "branch":  branch,
                "sha":     existing["sha"],
            })

        change_lines = "\n".join(f"- `{p}` → `{v}`" for p, v in param_changes.items())
        url = _open_and_merge_pr(gh, branch, f"[Auto-tune] {date_str}",
                                 f"Parameter update by daily reviewer.\n\n## Changes\n{change_lines}\n")
        return True, url
    except Exception as exc:
        logger.error(f"apply_param_changes failed: {exc}")
        return False, ""


# ── Apply code fixes via GitHub API ───────────────────────────────────────────

def apply_code_fixes(fixes: list[dict]) -> tuple[bool, str]:
    """
    Apply code-level text substitutions returned by Claude Sonnet.
    Each fix: {repo_path, old_text, new_text}.
    """
    if not AUTO_PR or not GITHUB_TOKEN or not GITHUB_REPO or not fixes:
        return False, ""
    try:
        gh, b64 = _gh_client()
        base_sha = gh("GET", f"/git/ref/heads/{DEPLOY_BRANCH}")["object"]["sha"]
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        branch   = f"auto/strategy-fix-{date_str}"
        gh("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})

        # Group by file
        by_file: dict[str, list] = {}
        for fix in fixes:
            by_file.setdefault(fix["repo_path"], []).append(fix)

        applied_total = 0
        for repo_path, file_fixes in by_file.items():
            existing = gh("GET", f"/contents/{repo_path}?ref={DEPLOY_BRANCH}")
            content  = b64.b64decode(existing["content"]).decode()
            applied  = 0
            for fix in file_fixes:
                old = fix.get("old_text", "")
                new = fix.get("new_text", "")
                if old and old in content:
                    content = content.replace(old, new, 1)
                    applied += 1
                    logger.info(f"Applied fix in {repo_path}: {old[:60]!r} → …")
                else:
                    logger.warning(f"old_text not found in {repo_path}: {old[:80]!r}")
            if applied:
                gh("PUT", f"/contents/{repo_path}", {
                    "message": f"auto-fix: strategy code ({applied} changes) {date_str}",
                    "content": b64.b64encode(content.encode()).decode(),
                    "branch":  branch,
                    "sha":     existing["sha"],
                })
                applied_total += applied

        if applied_total == 0:
            logger.warning("No fixes could be applied — old_text not found in any file")
            return False, ""

        url = _open_and_merge_pr(gh, branch, f"[Auto-fix] Strategy code {date_str}",
                                 f"Automated strategy code fix ({applied_total} changes).\n")
        return True, url
    except Exception as exc:
        logger.error(f"apply_code_fixes failed: {exc}")
        return False, ""


# ── Telegram report ───────────────────────────────────────────────────────────

def build_report(
    sniper_m: Metrics,
    s5_m:     Metrics,
    grid_m:   Metrics,
    conf_sweep: list[dict],
    observation: str,
    action_taken: str,
    pr_url: str,
    streak: int,
    state: dict,
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

    # Sniper
    lines += ["*S3 Sniper*",
              f"　倉位：`{sniper_m.n_total}` 筆　已結算：`{sniper_m.n_resolved}`"]
    if sniper_m.n_resolved > 0:
        lines += [
            f"　勝率：`{sniper_m.win_rate:.0f}%`　總P&L：`{sniper_m.total_pnl:+.4f}`",
            f"　平均P&L：`{sniper_m.avg_pnl:+.4f}`　Sharpe：`{sniper_m.sharpe:+.3f}`",
            f"　平均信心值：`{sniper_m.avg_conf:.3f}`",
        ]
    else:
        lines.append("　_尚無已結算倉位_")

    if streak > 0:
        lines.append(f"　⚠️ 連續 `{streak}` 次審查無新信號")

    if conf_sweep and any(r["n"] > 0 for r in conf_sweep):
        lines += ["", "*📊 信心閾值敏感度*"]
        for r in conf_sweep:
            if r["n"] == 0:
                continue
            cur = " ← 目前" if abs(r["min_confidence"] - 0.90) < 0.005 else ""
            lines.append(
                f"　`{r['min_confidence']:.2f}` {r['n']}筆 "
                f"WR={r['win_rate']:.0f}% P&L={r['total_pnl']:+.4f}{_esc(cur)}"
            )

    lines += ["", f"S5={s5_m.n_resolved}筆 P&L={s5_m.total_pnl:+.4f}　"
              f"Grid={grid_m.n_resolved}筆 P&L={grid_m.total_pnl:+.4f}"]

    if observation:
        lines += ["", f"{'─' * 28}", "*🤖 AI 分析*"]
        for line in observation.strip().split("\n"):
            lines.append(_esc(line) if line.strip() else "")

    if action_taken:
        lines += ["", f"{'─' * 28}", f"*⚙️ {_esc(action_taken)}*"]
        if pr_url:
            if AUTO_MERGE:
                lines.append("✅ 已自動 merge")
            else:
                lines.append(f"[GitHub PR]({pr_url})")
    else:
        lines += ["", "_本日無變更_"]

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=== Daily Strategy Review ===")
    logger.info(
        f"USE_CLAUDE={USE_CLAUDE}  AUTO_MERGE={AUTO_MERGE}  "
        f"DEPLOY_BRANCH={DEPLOY_BRANCH}"
    )

    sniper_state = _load_json("sniper_trades.json")
    s5_state     = _load_json("paper_trades.json")
    grid_state   = _load_json("grid_trades.json")

    sniper_trades = sniper_state.get("trades", [])
    s5_trades     = s5_state.get("trades", [])
    grid_trades   = grid_state.get("trades", [])

    sniper_m = calc_sniper_metrics(sniper_trades)
    s5_m     = calc_s5_metrics(s5_trades)
    grid_m   = calc_grid_metrics(grid_trades)

    conf_sweep  = sniper_conf_sweep(sniper_trades)
    s5_sweep    = s5_threshold_sweep(s5_trades)

    logger.info(
        f"Sniper: {sniper_m.n_resolved} resolved | WR={sniper_m.win_rate:.0f}% | "
        f"P&L={sniper_m.total_pnl:+.4f} | conf={sniper_m.avg_conf:.3f}"
    )

    # Update streak BEFORE deciding action
    streak = update_no_signal_streak(sniper_state, sniper_trades)
    logger.info(f"No-signal streak: {streak} review cycles")

    # ── Determine action tier ───────────────────────────────────────────────
    bad_ev = (
        sniper_m.n_resolved >= BAD_EV_MIN_TRADES
        and sniper_m.win_rate < BAD_EV_WIN_RATE
    )

    use_code_fix = (streak >= NO_SIGNAL_CODE_CYCLES) or bad_ev
    use_aggressive_params = (streak >= NO_SIGNAL_RELAX_CYCLES) and not use_code_fix

    action_taken = ""
    observation  = ""
    pr_url       = ""

    if use_code_fix:
        logger.info("Tier 2: calling Claude Sonnet for code-level fix")
        fixes, diagnosis = claude_code_fix(streak, bad_ev)
        observation = diagnosis
        if fixes:
            ok, pr_url = apply_code_fixes(fixes)
            if ok:
                action_taken = f"自動程式碼修復（{len(fixes)} 處變更）"
            else:
                action_taken = "程式碼修復嘗試失敗（old_text 未找到）"
        else:
            action_taken = "Claude Sonnet 無法生成有效修復"

    else:
        # Tier 0/1: parameter tune via Haiku
        summary = {
            "sniper": {
                "n_resolved":          sniper_m.n_resolved,
                "n_open":              sniper_m.n_total - sniper_m.n_resolved,
                "win_rate_pct":        round(sniper_m.win_rate, 1),
                "total_pnl":           round(sniper_m.total_pnl, 4),
                "sharpe":              round(sniper_m.sharpe, 3),
                "avg_confidence":      round(sniper_m.avg_conf, 3),
                "no_signal_streak":    streak,
                "conf_threshold_sweep": conf_sweep,
            },
            "s5":   {"n_resolved": s5_m.n_resolved, "total_pnl": round(s5_m.total_pnl, 4)},
            "grid": {"n_resolved": grid_m.n_resolved, "total_pnl": round(grid_m.total_pnl, 4)},
            "current_params": {
                "CRYPTO_MIN_CONFIDENCE":   0.90,
                "SNIPE_CRYPTO_MAX_HOURS":  6,
                "SNIPE_KELLY_CAP_NEAR":    0.20,
                "SNIPE_KELLY_CAP_OVERDUE": 0.35,
                "SNIPE_MAX_ENTRY":         0.95,
            },
        }
        param_changes, observation = claude_param_tune(
            summary, aggressive=use_aggressive_params
        )
        logger.info(f"Tier {'1 aggressive' if use_aggressive_params else '0'} params: {param_changes}")

        guard = MIN_TRADES_FOR_TUNE if not use_aggressive_params else 0
        if param_changes and sniper_m.n_resolved >= guard:
            ok, pr_url = apply_param_changes(param_changes)
            if ok:
                change_desc = "、".join(f"{p}={v}" for p, v in param_changes.items())
                action_taken = f"參數調整：{change_desc}"
        elif use_aggressive_params and not param_changes:
            action_taken = "Claude 無法建議放寬參數（下週期觸發程式碼修復）"

    # ── Save updated sniper state (metadata) ────────────────────────────────
    from strategy_sniper import save_sniper_state
    save_sniper_state(sniper_state)

    # ── Send Telegram ────────────────────────────────────────────────────────
    from telegram_reporter import send_message
    report = build_report(
        sniper_m, s5_m, grid_m,
        conf_sweep, observation, action_taken, pr_url,
        streak, s5_state,
    )
    if not send_message(report):
        logger.warning("Telegram failed — printing to stdout")
        print(report)

    logger.info(f"Review complete. Streak={streak}  Action={action_taken or 'none'}")


if __name__ == "__main__":
    main()
