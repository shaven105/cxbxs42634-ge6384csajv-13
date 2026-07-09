# Daily Loop Report — 2026-07-09

## Verdict
- **Bottleneck**: `market_efficient`
- **Diagnosis**: 過去 111 次掃描：模型驗證了 2225 個結果，但市場已定價（entry_reject=2222, kelly_reject=3）。边缘只在價格快速移動時出現 — 保持掃描，等待波動。
- **Action taken**: none
- **No-signal streak**: 8 cycles

## Funnel (last 111 scans)
| Stage | Count |
|---|---|
| crypto candidates verified→traded | 0 |
| crypto too-close-to-call | 520 |
| verified but market already priced | 2222 |
| verified but -EV at ask (Kelly=0) | 3 |
| weather candidates | 0 |
| sports non-matches | 55934 |

## Performance
- Resolved: 0 | Win rate: 0% | P&L: +0.0000

## Current params
| Param | Value |
|---|---|
| CRYPTO_MIN_CONFIDENCE | 0.9 |
| SNIPE_CRYPTO_MAX_HOURS | 12 |
| SNIPE_KELLY_CAP_NEAR | 0.2 |
| SNIPE_KELLY_CAP_OVERDUE | 0.35 |
| SNIPE_MAX_ENTRY | 0.95 |
| WEATHER_MIN_CONFIDENCE | 0.88 |

## Notes for next Claude Code session
- If bottleneck is `parser` for 2+ days: fetch live markets, compare question
  formats against `_parse_crypto_question` / `_parse_weather_question`, extend parsers.
- If `market_efficient` persists 7+ days: strategy edge may be gone at 5-min
  cadence; consider CLOB websocket or accept lower frequency.
- This file is auto-generated daily by reviewer.py (deterministic loop engine).
