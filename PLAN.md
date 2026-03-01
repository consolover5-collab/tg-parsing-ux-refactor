# Plan: Telegram Monitor Bot (tg-parsing)

## Context

Бот-мониторщик Telegram-барахолок. Два компонента:

1. **Telethon userbot** — мониторит чаты в реальном времени, отправляет DM продавцам
2. **Telegram Bot** (aiogram) — UI для управления: статус, добавление чатов/ключевых слов, настройки

Целевые платформы: Oracle Cloud MicroFlex (1GB RAM ARM) и Synology NAS.
Мульти-инстанс: каждый пользователь разворачивает свою копию со своими credentials.

### Почему НЕ берём готовые репо

- `unnohwn/telegram-scraper` — batch-скрапер с polling 60с, монолитный файл 965 строк, нет event-driven мониторинга. Потребуется переписать 80% кода
- `khmuhtadin/TeleGpt` — не существует (private/удалён)
- Проще и чище написать с нуля под наши требования (~600 строк)

### Vision API: Groq (бесплатно)

- Модель: `meta-llama/llama-4-scout-17b-16e-instruct` (бесплатно, без карты)
- OpenAI-совместимый API → легко переключить на другой провайдер
- Fallback: Cloudflare Workers AI (100k req/day free)

---

## Структура проекта

```
tg-parsing/
├── main.py                  # Запуск обоих клиентов (userbot + bot)
├── config.example.json      # Шаблон конфига (коммитится)
├── config.json              # Реальный конфиг (gitignored)
├── requirements.txt
├── .gitignore
│
├── bot/
│   ├── __init__.py
│   ├── models.py            # Pydantic-модели конфига
│   ├── userbot.py           # Telethon: мониторинг чатов + отправка DM
│   ├── control.py           # aiogram: Telegram-бот с кнопками (UI)
│   ├── vision.py            # Groq vision API (OpenAI-совместимый)
│   ├── dedup.py             # Хэш + SQLite dedup (по seller_id глобально)
│   ├── keywords.py          # Regex keyword matcher
│   ├── price.py             # Извлечение цены из текста
│   └── ratelimit.py         # Token-bucket rate limiter
│
├── db/
│   ├── __init__.py
│   └── database.py          # aiosqlite: schema + CRUD
│
├── deploy/
│   ├── tg-parsing.service   # systemd (Oracle Cloud)
│   └── docker-compose.yml   # Docker (Synology NAS)
│
└── Dockerfile               # Для Synology / любого Docker-хоста
```

---

## config.json — схема

```json
{
  "telegram": {
    "api_id": 12345678,
    "api_hash": "abc...",
    "phone": "+79001234567",
    "session_name": "session/monitor",
    "bot_token": "123456:ABC-DEF..."
  },
  "monitoring": {
    "chats": ["@cg_baraholka"],
    "keywords": ["телевизор", "колонка", "jbl"],
    "max_price": 30000,
    "use_vision": true,
    "vision_prompt": "На фото телевизор, колонка или аудиосистема? Если да — ответь: ТИП: ..., ЦЕНА: ... Если нет — ответь: НЕТ"
  },
  "vision": {
    "provider": "groq",
    "api_key": "gsk_...",
    "model": "meta-llama/llama-4-scout-17b-16e-instruct",
    "base_url": "https://api.groq.com/openai/v1/chat/completions"
  },
  "actions": {
    "dm_message": "Привет, ещё доступно?",
    "notify_chat_id": "me"
  },
  "rate_limits": {
    "dm_per_hour": 15,
    "vision_per_minute": 5
  },
  "database": {
    "path": "data/dedup.db"
  }
}
```

---

## Telegram Bot UI (aiogram) — `bot/control.py`

### Главное меню (при /start или открытии бота)

```
📊 Статус: Активен | Мониторинг: 3 чата | Найдено: 47 | DM: 23

[📡 Чаты]  [🔑 Ключевые слова]
[💰 Макс. цена]  [🧪 Тест]
[📋 Последние находки]  [⚙️ Настройки]
[⏸ Пауза / ▶️ Запуск]
```

### Функции кнопок

- **📡 Чаты** → список текущих чатов + кнопки [Добавить] [Удалить]
- **🔑 Ключевые слова** → список + [Добавить] [Удалить]
- **💰 Макс. цена** → текущая цена + ввод новой
- **🧪 Тест** → отправить тестовое фото/текст для проверки pipeline
- **📋 Последние находки** → список последних 10 матчей (время, чат, тип, ссылка)
- **⚙️ Настройки** → кому отправлять DM (мне / другому user_id), вкл/выкл vision
- **⏸ Пауза** → приостановить мониторинг без остановки процесса

### Уведомления в бот

Когда найдено совпадение → бот присылает:

```
🔔 Новое совпадение!
📍 Чат: @cg_baraholka
🏷 Тип: keyword (телевизор)
💰 Цена: 15 000 ₽
✉️ DM отправлен продавцу
🔗 [Ссылка на сообщение]
```

При повторе:

```
🔄 Повтор от того же продавца
📍 Чат: @another_chat
ℹ️ DM уже отправлялся ранее
```

---

## Database (SQLite) — `db/database.py`

```sql
CREATE TABLE seen_sellers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id   INTEGER NOT NULL UNIQUE,
    first_chat  TEXT NOT NULL,
    first_msg_id INTEGER NOT NULL,
    match_type  TEXT NOT NULL,
    matched_value TEXT,
    price       INTEGER,
    dm_sent     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id   INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    match_type  TEXT NOT NULL,
    matched_value TEXT,
    price       INTEGER,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (seller_id) REFERENCES seen_sellers(seller_id)
);

CREATE TABLE stats (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

**Ключевое**: dedup по `seller_id` (UNIQUE), а не по хэшу сообщения. Один продавец = один DM, даже если постит в разных чатах.

---

## Pipeline обработки сообщений — `bot/userbot.py`

```
Новое сообщение в мониторимом чате
    │
    ├─ Есть текст? ──→ Keyword match ──→ Совпадение?
    │                                       ├─ Да → extract_price()
    │                                       └─ Нет → ─┐
    │                                                   │
    ├─ Есть фото + vision включен? ←────────────────────┘
    │   └─ Да → rate_limit_check → Groq Vision API
    │           └─ Совпадение? → extract price from AI
    │
    ├─ Цена > max_price? → Пропустить
    │
    ├─ seller_id уже в seen_sellers?
    │   ├─ Да (повтор) → записать в matches(is_duplicate=1)
    │   │                → уведомить в бот "🔄 Повтор"
    │   │
    │   └─ Нет (новый) → INSERT в seen_sellers
    │                   → rate_limit_check
    │                   → send DM "Привет, ещё доступно?"
    │                   → записать в matches
    │                   → уведомить в бот "🔔 Новое!"
```

---

## Порядок реализации (10 шагов)

### 1. Скелет проекта
- `.gitignore`, `requirements.txt`, `config.example.json`
- `bot/models.py` — Pydantic-модели конфига

### 2. Database
- `db/database.py` — DDL + async CRUD (aiosqlite)

### 3. Keywords + Price
- `bot/keywords.py` — compiled regex matcher
- `bot/price.py` — извлечение цены ("15к", "15000р", "15 тыс")

### 4. Dedup
- `bot/dedup.py` — проверка/запись seller_id в seen_sellers

### 5. Vision (Groq)
- `bot/vision.py` — OpenAI-совместимый вызов Groq API, парсинг ответа

### 6. Rate limiter
- `bot/ratelimit.py` — sliding window для DM (15/час) и vision (5/мин)

### 7. Userbot (ядро мониторинга)
- `bot/userbot.py` — Telethon event handler, album debounce, pipeline, отправка DM

### 8. Control bot (UI)
- `bot/control.py` — aiogram бот: меню, кнопки, CRUD чатов/keywords, уведомления

### 9. Main + graceful shutdown
- `main.py` — запуск обоих клиентов в одном event loop, signal handlers

### 10. Deployment
- `deploy/tg-parsing.service` — systemd (MemoryMax=512M)
- `Dockerfile` + `docker-compose.yml` — для Synology NAS
- `deploy/install.sh` — скрипт установки

---

## Зависимости (requirements.txt)

```
telethon==1.37.0          # Userbot: мониторинг + DM
aiogram==3.15.0           # Telegram Bot UI
aiosqlite==0.20.0         # Async SQLite
aiohttp==3.10.11          # HTTP клиент (для Groq API)
pydantic==2.10.3          # Валидация конфига
cryptg==0.4.0             # Быстрое шифрование для Telethon на ARM
```

**~70 MB RAM** в steady state.

---

## Deployment

### Oracle Cloud (systemd)

```ini
[Service]
MemoryMax=512M
Restart=always
RestartSec=10
```

### Synology NAS (Docker)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-u", "main.py"]
```

```yaml
# docker-compose.yml
services:
  tg-parsing:
    build: .
    restart: always
    volumes:
      - ./config.json:/app/config.json
      - ./data:/app/data
      - ./session:/app/session
    mem_limit: 512m
```

---

## Верификация

1. `python main.py` — интерактивная авторизация Telethon + бот стартует
2. Открыть бота → /start → проверить меню с кнопками
3. Через бот добавить тестовый чат и ключевое слово
4. Отправить тестовое сообщение с ключевым словом → бот уведомляет + DM отправлен
5. Отправить повторно → бот уведомляет "🔄 Повтор", DM НЕ отправлен
6. Кнопка "Тест" → проверка vision pipeline
7. `sudo systemctl status tg-parsing` или `docker logs tg-parsing`
