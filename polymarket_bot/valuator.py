import json
import logging
import re
from typing import Optional

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS
from tracker import Tracker

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_SYSTEM = """You are a calibrated probabilistic forecaster for prediction markets.
You will receive a binary prediction market question with metadata.
Estimate the probability that the YES outcome resolves TRUE.

Respond with ONLY valid JSON in this exact format:
{"probability": <float 0.01–0.99>, "reasoning": "<1–2 sentences>"}

Rules:
- Never simply echo the market's current price.
- Use base rates, your knowledge of the topic, and logical reasoning.
- If genuinely uncertain, return a value near 0.50.
- Do not include any text outside the JSON object."""


def _build_prompt(market: dict) -> str:
    parts = [
        f"Question: {market.get('question', 'Unknown')}",
        f"Resolution Date: {market.get('endDateIso') or market.get('endDate', 'Unknown')}",
    ]
    src = market.get("resolutionSource", "")
    if src:
        parts.append(f"Resolution Source: {src}")
    desc = (market.get("description") or "")[:800]
    if desc:
        parts.append(f"Description: {desc}")
    return "\n".join(parts)


def _parse_response(text: str) -> Optional[float]:
    text = text.strip()
    # Strip optional markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        prob = float(data["probability"])
        if 0.01 <= prob <= 0.99:
            return prob
        logger.warning(f"Probability {prob:.4f} out of [0.01, 0.99]")
        return None
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning(f"Parse failure: {exc!r} | raw: {text[:200]!r}")
        return None


def estimate_fair_probability(market: dict, tracker: Tracker) -> Optional[float]:
    """
    Ask Claude for the fair YES probability for a market.
    Charges token costs to tracker. Returns None on failure.
    """
    try:
        response = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": _build_prompt(market)}],
        )
    except anthropic.APIError as exc:
        logger.error(f"Claude API error for '{market.get('question','')[:50]}': {exc}")
        return None

    tracker.record_claude_usage(
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    if not response.content or response.content[0].type != "text":
        return None

    prob = _parse_response(response.content[0].text)
    if prob is not None:
        logger.debug(
            f"'{market.get('question','')[:50]}' → "
            f"fair={prob:.3f} mid={market.get('_mid_price', 0):.3f}"
        )
    return prob
