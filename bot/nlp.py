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
    price: int | None = None,
) -> str | None:
    """Generate a personalised DM for a keyword-matched listing (no NLP step).

    Returns DM text string or None on failure.
    """
    price_str = str(price) if price else "не указана"
    system = (
        "Ты — покупатель на барахолке. Напиши 1-2 предложения продавцу. "
        "Обязательно упомяни название товара. Спроси, актуально ли объявление. "
        "Если цена указана — спроси, можно ли немного скинуть. "
        "Пиши естественно, как обычный человек. Без приветствий типа 'Здравствуйте'. "
        "Отвечай только текстом DM, без JSON, без кавычек."
    )
    user = f"Товар: {item_type}.\nЦена: {price_str}.\nТекст объявления:\n{listing_text[:600]}"

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


async def classify_user_input(text: str, config) -> dict:
    """Classify free-form user input as a Telegram chat ref or a keyword/product.

    Returns:
        {"type": "chat", "value": "@username_or_link"}
        {"type": "keyword", "value": "main_keyword", "synonyms": [...]}
        {"type": "unknown"}
    """
    system = (
        "Ты — ассистент настройки Telegram-мониторинга. "
        "Пользователь прислал текст. Определи что это:\n"
        "1. \'chat\' — Telegram-группа или канал (есть @username, t.me/, или это явно название канала)\n"
        "2. \'keyword\' — название товара или категории для поиска (телевизор, колонка, ноутбук, велосипед и т.п.)\n"
        "3. \'unknown\' — ни то ни другое\n\n"
        "Ответь строго в JSON (без markdown, без пояснений):\n"
        "Для чата: {\"type\": \"chat\", \"value\": \"@username_или_ссылка\"}\n"
        "Для товара: {\"type\": \"keyword\", \"value\": \"основное_слово_в_им.п.\", "
        "\"synonyms\": [\"синоним1\", \"синоним2\"]}\n"
        "Для неизвестного: {\"type\": \"unknown\"}"
    )
    payload = {
        "model": config.vision.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text[:300]},
        ],
        "max_tokens": 150,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
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
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return {"type": "unknown"}
                data = await resp.json()
        raw = data["choices"][0]["message"]["content"].strip()
        result = json.loads(raw)
        return result if isinstance(result, dict) and "type" in result else {"type": "unknown"}
    except Exception as e:
        logger.error("classify_user_input failed: %s", e)
        return {"type": "unknown"}

async def expand_synonyms(keyword: str, existing: list, config) -> list:
    """Ask Groq to expand synonyms for a keyword. Returns only new (non-duplicate) items."""
    existing_str = ", ".join(existing) if existing else "нет"
    system = (
        "Ты помогаешь настроить поиск объявлений на Telegram-барахолках. "
        "Дано ключевое слово и уже известные синонимы. "
        "Предложи дополнительные синонимы: варианты написания, сленг, аббревиатуры, транслит, английские аналоги. "
        "Верни JSON: {\"synonyms\": [\"слово1\", \"слово2\", ...]} "
        "— только новые слова, без дублей с уже существующими. "
        "Если добавить нечего — {\"synonyms\": []}."
    )
    user = f"Товар: {keyword}\nУже есть: {existing_str}"
    payload = {
        "model": config.vision.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": 150,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {config.vision.api_key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.vision.base_url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        raw = data["choices"][0]["message"]["content"].strip()
        result = json.loads(raw)
        new_syns = result.get("synonyms", [])
        existing_lower = {s.lower() for s in existing}
        return [s for s in new_syns if s.strip() and s.lower() not in existing_lower]
    except Exception as e:
        logger.error("expand_synonyms failed: %s", e)
        return []
