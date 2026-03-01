"""Groq text NLP: semantic match detection + personalised DM generation.

Uses the same Groq API key / base_url as Vision, but with a text prompt.
One API call per message: returns match verdict AND DM text together.
"""

import json
import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

# Signals that suggest a message is a marketplace listing
_LISTING_RE = re.compile(
    r'продаю|продам|отдам|отдаётся|продаётся|цена|стоит|₽|руб|rub|\d[\d\s]{2,6}\s*(?:руб|р\.|₽|тыс|к\b)',
    re.IGNORECASE,
)


def looks_like_listing(text: str) -> bool:
    """Quick pre-filter: does this text look like a sales listing?"""
    return bool(_LISTING_RE.search(text or ""))


_DEFAULT_SYSTEM = (
    "Ты — ассистент, анализирующий объявления на барахолке. "
    "Твоя задача: (1) определить, продаёт ли автор то, что ищет покупатель, "
    "(2) если да — написать короткое (1-2 предложения), естественное сообщение "
    "покупателя продавцу, как будто обычный человек спрашивает об объявлении. "
    "Отвечай СТРОГО в JSON без markdown-блоков:\n"
    '{"match": true/false, "type": "что продаётся или пусто", '
    '"price": число_или_null, "dm": "текст сообщения или пусто"}'
)


async def analyse_text(
    text: str,
    targets: list[str],
    config,
    limiter=None,
) -> dict | None:
    """Analyse text with Groq: returns {match, type, price, dm} or None on error/skip.

    Args:
        text: message text to analyse
        targets: what the buyer is looking for (e.g. ["колонка", "телевизор"])
        config: Config object (uses vision.api_key / vision.base_url)
        limiter: RateLimiter instance (uses text_nlp_per_minute budget)
    """
    if not text or not text.strip():
        return None
    if limiter and not limiter.consume():
        logger.debug("NLP rate limit hit, skipping text analysis")
        return None

    targets_str = ", ".join(targets) if targets else "электроника"
    user_prompt = (
        f"Покупатель ищет: {targets_str}.\n"
        f"Объявление:\n{text[:800]}"
    )

    payload = {
        "model": config.vision.model,
        "messages": [
            {"role": "system", "content": _DEFAULT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 256,
        "temperature": 0.3,
    }

    headers = {
        "Authorization": f"Bearer {config.vision.api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.vision.base_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Groq NLP returned %d", resp.status)
                    return None
                data = await resp.json()

        raw = data["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if model adds them
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        result = json.loads(raw)
        return {
            "match": bool(result.get("match")),
            "type": str(result.get("type") or ""),
            "price": int(result["price"]) if result.get("price") else None,
            "dm": str(result.get("dm") or ""),
        }
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Groq NLP parse error: %s | raw: %s", e, raw if 'raw' in dir() else "")
        return None
    except Exception as e:
        logger.error("Groq NLP request failed: %s", e)
        return None


async def generate_dm(
    listing_text: str,
    item_type: str,
    config,
) -> str | None:
    """Generate a personalised DM for a keyword-matched listing (no NLP step).

    Returns DM text string or None on failure.
    """
    system = (
        "Ты — покупатель на барахолке. Напиши 1-2 предложения продавцу: "
        "уточни, актуально ли его объявление и можно ли договориться о цене. "
        "Пиши естественно, как обычный человек. Без приветствий типа 'Здравствуйте'. "
        "Отвечай только текстом DM, без JSON, без кавычек."
    )
    user = f"Тип товара: {item_type}.\nТекст объявления:\n{listing_text[:600]}"

    payload = {
        "model": config.vision.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 120,
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {config.vision.api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.vision.base_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error("Groq DM generation failed: %s", e)
        return None
