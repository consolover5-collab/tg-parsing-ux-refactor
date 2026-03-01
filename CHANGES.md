# Изменения: Управление действиями бота

## Обзор

Эта версия добавляет продвинутую систему управления действиями бота через UI, позволяя гибко настраивать поведение без изменения кода.

## Основные возможности

### 1. 🎯 Управление действиями (Actions Management)

Новое меню "Управление действиями" в боте позволяет:

- **Авто-DM**: Включить/выключить автоматическую отправку DM продавцам
- **Пересылка боту**: Включить/выключить пересылку сообщений основному боту
- **Режим пересылки**: Выбор между `forward_raw` (прямая пересылка) и `notify_with_meta` (уведомление с метаданными)
- **Dry-run режим**: Тестирование без реальных отправок (только логирование)

### 2. 📝 Шаблоны DM с плейсхолдерами

Теперь можно создавать динамические шаблоны DM сообщений:

**Поддерживаемые плейсхолдеры:**
- `{type}` - Тип товара (из keyword_map или vision)
- `{price}` - Цена товара
- `{link}` - Ссылка на сообщение
- `{author}` - ID автора сообщения
- `{chat_title}` - Название чата
- `{message_snippet}` - Отрывок из сообщения (до 200 символов)
- `{source_chat}` - ID чата-источника

**Пример шаблона:**
```
Привет! {type} ещё доступна?
Цена: {price} руб.
Ссылка: {link}
```

### 3. ⚙️ Настройки для отдельных чатов

Можно переопределить настройки для конкретных чатов через `rules.per_chat_overrides` в config.json:

```json
"rules": {
  "per_chat_overrides": {
    "@special_chat": {
      "auto_dm": false,
      "forward_to_main_bot": true,
      "dm_template": "Специальный шаблон для этого чата"
    }
  }
}
```

### 4. 🚫 Opt-out список

Список user_id, которым НЕ нужно отправлять DM:

- Добавление/удаление через UI
- Автоматическая проверка перед отправкой
- Сохраняется в `rules.opt_out_list`

### 5. 📜 Лог действий

Отслеживание всех действий бота:

- ✉️ Отправка DM (успешно/неудачно)
- 📤 Пересылка сообщений (успешно/неудачно)
- 🔄 Пропущенные повторы
- Детали в базе данных (таблица `actions_log`)

### 6. 🔧 Dry-run режим

Тестирование конфигурации без реальных отправок:

- Включается через UI одной кнопкой
- Логирует что было бы отправлено
- Идеально для отладки шаблонов и правил

## Структура базы данных

### Новые таблицы:

**chats** - Внутренние ID чатов
```sql
CREATE TABLE chats (
    id TEXT PRIMARY KEY,          -- UUID
    external TEXT UNIQUE,         -- Telegram chat_id
    title TEXT,                   -- Название чата
    created_at TEXT
);
```

**messages** - Обработанные сообщения
```sql
CREATE TABLE messages (
    id TEXT PRIMARY KEY,          -- UUID
    chat_id TEXT,                 -- FK to chats
    source_chat TEXT,             -- Telegram chat_id
    author TEXT,                  -- User ID
    text TEXT,                    -- Текст сообщения
    ts TEXT,                      -- Timestamp
    meta TEXT                     -- JSON с метаданными
);
```

**actions_log** - Лог действий
```sql
CREATE TABLE actions_log (
    id INTEGER PRIMARY KEY,
    message_id TEXT,              -- FK to messages
    action_type TEXT,             -- "dm", "forward", "duplicate"
    result TEXT,                  -- "success", "failed", "skipped"
    timestamp TEXT,
    details TEXT                  -- JSON с дополнительной информацией
);
```

**pools** и **pool_chats** - Группировка чатов (для будущего использования)

## Изменения в config.json

### Новые поля в `actions`:

```json
"actions": {
  "dm_message": "Привет, ещё доступно?",        // Устаревшее, используется как fallback
  "notify_chat_id": "me",
  "auto_dm": true,                              // NEW: Включить автоматические DM
  "forward_to_main_bot": false,                 // NEW: Пересылать боту
  "forward_mode": "notify_with_meta",           // NEW: Режим пересылки
  "dm_template": "Привет! {type}...",           // NEW: Шаблон с плейсхолдерами
  "dry_run": false                              // NEW: Тестовый режим
}
```

### Новая секция `rules`:

```json
"rules": {
  "keyword_map": {                              // NEW: Маппинг ключевых слов на типы
    "телевизор": "TV",
    "колонка": "Speaker"
  },
  "vision_enabled": true,                       // NEW: Включить vision
  "vision_precedence": "keywords",              // NEW: Приоритет vision или keywords
  "per_chat_overrides": {},                     // NEW: Переопределения для чатов
  "opt_out_list": []                            // NEW: Список исключений
}
```

## Структура кода

### Новые модули:

**bot/processor.py** - Обработчик сообщений
- `MessageProcessor.render_template()` - Рендеринг шаблонов
- `MessageProcessor.get_effective_config()` - Получение эффективной конфигурации с учетом переопределений
- `MessageProcessor.decide_actions()` - Решение каких действий выполнять
- `MessageProcessor.store_message()` - Сохранение сообщения в БД
- `MessageProcessor.format_notification()` - Форматирование уведомлений

### Расширенные модули:

**bot/models.py** - Новые Pydantic модели
- `ForwardMode` - Enum для режимов пересылки
- `VisionPrecedence` - Enum для приоритета vision
- `PerChatOverride` - Модель переопределений для чата
- `RulesConfig` - Конфигурация правил
- Расширенные `ActionsConfig`

**bot/userbot.py** - Обновленная логика обработки
- Интеграция `MessageProcessor`
- Новый метод `_send_dm_with_template()` для отправки с шаблонами
- Новый метод `_forward_message()` для пересылки
- Поддержка dry_run режима
- Логирование действий в БД

**bot/control.py** - Новые UI меню
- Меню "Управление действиями"
- Обработчики для всех новых настроек
- Редактор шаблонов
- Управление opt-out списком
- Просмотр логов действий

**db/database.py** - Новые методы
- `get_or_create_chat()` - Управление внутренними ID чатов
- `add_message()` - Сохранение сообщений
- `log_action()` - Логирование действий
- `get_actions_log()` - Получение логов
- `create_pool()`, `add_chat_to_pool()`, `get_pool_chats()` - Управление пулами

## Использование

### Через UI бота:

1. Откройте бота в Telegram
2. Нажмите "🎯 Управление действиями"
3. Настройте желаемые параметры
4. Включите dry_run для тестирования
5. Проверьте лог действий

### Через config.json:

```json
{
  "actions": {
    "auto_dm": true,
    "forward_to_main_bot": false,
    "forward_mode": "notify_with_meta",
    "dm_template": "Привет! {type} ещё доступна? Цена: {price}",
    "dry_run": false
  },
  "rules": {
    "keyword_map": {
      "телевизор": "TV",
      "колонка": "Speaker"
    },
    "opt_out_list": [123456789],
    "per_chat_overrides": {
      "@special_chat": {
        "auto_dm": false,
        "forward_to_main_bot": true
      }
    }
  }
}
```

## Миграция с предыдущей версии

Существующие конфигурации будут работать со значениями по умолчанию:
- `auto_dm`: true (включено)
- `forward_to_main_bot`: false (выключено)
- `forward_mode`: "notify_with_meta"
- `dry_run`: false
- `dm_template`: использует старое `dm_message`

База данных автоматически создаст новые таблицы при первом запуске.

## Примеры сценариев

### Сценарий 1: Только логирование без действий
```json
"actions": {
  "auto_dm": false,
  "forward_to_main_bot": false,
  "dry_run": false
}
```

### Сценарий 2: Только пересылка, без DM
```json
"actions": {
  "auto_dm": false,
  "forward_to_main_bot": true,
  "forward_mode": "forward_raw"
}
```

### Сценарий 3: И DM, и пересылка
```json
"actions": {
  "auto_dm": true,
  "forward_to_main_bot": true,
  "forward_mode": "notify_with_meta",
  "dm_template": "Привет! {type} за {price} руб."
}
```

### Сценарий 4: Тестирование конфигурации
```json
"actions": {
  "auto_dm": true,
  "forward_to_main_bot": true,
  "dry_run": true  // Ничего не отправляется, только логи
}
```

## Известные ограничения

1. Изменение списка мониторимых чатов требует перезапуска бота
2. Per-chat overrides применяются только к новым сообщениям
3. Pools пока не используются в логике (заготовка для будущего)

## Техническая информация

- Python 3.11+
- Новые зависимости: нет (используются существующие)
- Обратная совместимость: да (со значениями по умолчанию)
- Требуется миграция БД: нет (автоматическая)
