"""Extract price from text (supports RUB, EUR, USD)."""

import re

# Patterns: "15000", "15 000", "15000р", "15к", "15 тыс", "15.5к", "100€", "50 евро"
_PRICE_PATTERNS = [
    # "15 000 руб" / "15000р" / "15 000₽"
    re.compile(
        r"(\d{1,3}(?:[\s\u00a0]\d{3})+)\s*(?:руб|₽|р\b)",
        re.IGNORECASE,
    ),
    # "15000" standalone with currency marker (RUB)
    re.compile(
        r"(\d{4,7})\s*(?:руб|₽|р\b)",
        re.IGNORECASE,
    ),
    # Euro: "100€" / "100 евро" / "€100" / "100 eur" / "100Є"
    re.compile(
        r"€\s*(\d{1,7}(?:[.,]\d{1,2})?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d{1,7}(?:[.,]\d{1,2})?)\s*(?:€|евро|eur|Є)",
        re.IGNORECASE,
    ),
    # USD: "$100" / "100 usd" / "100 долл"
    re.compile(
        r"\$\s*(\d{1,7}(?:[.,]\d{1,2})?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d{1,7}(?:[.,]\d{1,2})?)\s*(?:\$|usd|долл)",
        re.IGNORECASE,
    ),
    # "15к" / "15.5к" / "15 к"
    re.compile(
        r"(\d{1,4}(?:[.,]\d{1,2})?)\s*к\b",
        re.IGNORECASE,
    ),
    # "15 тыс" / "15тыс"
    re.compile(
        r"(\d{1,4}(?:[.,]\d{1,2})?)\s*тыс",
        re.IGNORECASE,
    ),
    # bare number 4-7 digits (fallback)
    re.compile(r"\b(\d{4,7})\b"),
]


def extract_price(text: str) -> int | None:
    """Return price as integer or None."""
    if not text:
        return None

    for pattern in _PRICE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(1).replace("\u00a0", "").replace(" ", "")

        # "15к" / "15 тыс" multiplier
        if pattern.pattern.endswith(r"к\b") or "тыс" in pattern.pattern:
            raw = raw.replace(",", ".")
            try:
                return int(float(raw) * 1000)
            except ValueError:
                continue

        try:
            return int(raw)
        except ValueError:
            continue

    return None
