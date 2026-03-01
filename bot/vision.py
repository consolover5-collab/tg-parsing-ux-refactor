"""Groq Vision API (OpenAI-compatible) — analyse photos for relevant items."""

import base64
import logging
import time

import aiohttp

# Last known Groq rate-limit state, populated from response headers.
# Zero extra API calls — updated on every Vision response.
_groq_rate_info: dict = {}

from bot.models import VisionConfig

logger = logging.getLogger(__name__)


async def analyse_image(
    image_bytes: bytes,
    prompt: str,
    config: VisionConfig,
    timeout: float = 30.0,
) -> str | None:
    """Send image to Groq Vision and return the model's text reply (or None on error)."""
    b64 = base64.b64encode(image_bytes).decode()

    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 256,
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.base_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("Vision API error %s: %s", resp.status, body[:300])
                    return None
                _groq_rate_info.update({
                    "limit_requests":     resp.headers.get("x-ratelimit-limit-requests"),
                    "remaining_requests": resp.headers.get("x-ratelimit-remaining-requests"),
                    "limit_tokens":       resp.headers.get("x-ratelimit-limit-tokens"),
                    "remaining_tokens":   resp.headers.get("x-ratelimit-remaining-tokens"),
                    "reset_requests":     resp.headers.get("x-ratelimit-reset-requests"),
                    "updated_at":         time.monotonic(),
                })
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("Vision API request failed: %s", e)
        return None


def parse_vision_response(text: str) -> dict | None:
    """Parse vision model reply. Returns {'type': ..., 'price': ...} or None if 'НЕТ'."""
    if not text:
        return None
    upper = text.upper().strip()
    if upper.startswith("НЕТ") or upper == "НЕТ":
        return None

    result: dict = {"type": None, "price": None}

    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("ТИП:"):
            result["type"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("ЦЕНА:"):
            raw = line.split(":", 1)[1].strip()
            digits = "".join(c for c in raw if c.isdigit())
            if digits:
                result["price"] = int(digits)

    if result["type"]:
        return result
    return None
