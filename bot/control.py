"""aiogram Telegram bot — control panel UI for tg-parsing."""

import json
import logging
import io
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.models import Config
from db.database import Database

logger = logging.getLogger(__name__)
router = Router()

# Runtime references (set in ControlBot.setup)
_bot_instance: "ControlBot | None" = None


def _cfg() -> Config:
    return _bot_instance.config


def _db() -> Database:
    return _bot_instance.db


def _extract_chat_ref_from_message(message: Message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    if origin:
        chat = getattr(origin, "chat", None)
        if chat:
            username = getattr(chat, "username", None)
            return f"@{username}" if username else str(chat.id)

    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat:
        username = getattr(fwd_chat, "username", None)
        return f"@{username}" if username else str(fwd_chat.id)

    return None


def _normalize_chat_ref_input(text: str) -> str | None:
    value = (text or "").strip()
    if not value:
        return None
    if value.startswith("@"):
        username = value[1:]
        if re.fullmatch(r"[A-Za-z0-9_]{5,64}", username):
            return f"@{username}"
        return None
    if re.fullmatch(r"-?\d+", value):
        return value
    return None


async def _handle_chat_add(message: Message, fallback_text: str = "") -> bool:
    chat_ref = _extract_chat_ref_from_message(message)
    if not chat_ref:
        chat_ref = _normalize_chat_ref_input(fallback_text)
    if not chat_ref:
        await message.answer(
            "❌ Не удалось определить чат из пересылки.\n"
            "Если Telegram скрывает источник, введите @username или ID чата вручную."
        )
        return False
    if chat_ref in _cfg().monitoring.chats:
        await message.answer(f"ℹ️ Чат {chat_ref} уже в списке.")
        return True

    _cfg().monitoring.chats.append(chat_ref)
    _save_config()
    if _bot_instance.userbot:
        await message.answer(
            f"✅ Чат {chat_ref} добавлен.\n"
            "⚠️ Для применения перезапустите сервис на хосте kc:\n"
            "<code>sudo systemctl restart tg-parsing</code>",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"✅ Чат {chat_ref} добавлен.")
    return True


async def _send_qr_image(message: Message, link: str) -> bool:
    try:
        import qrcode

        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        await message.answer_photo(
            BufferedInputFile(buf.getvalue(), filename="userbot-login-qr.png"),
            caption="📷 QR для авторизации userbot (действует ~2 минуты).",
        )
        return True
    except Exception as e:
        logger.warning("Failed to build/send QR image: %s", e)
        try:
            import qrcode

            qr = qrcode.QRCode(border=1)
            qr.add_data(link)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            ascii_qr = "\n".join(
                "".join("██" if cell else "  " for cell in row)
                for row in matrix
            )
            await message.answer(
                "📷 PNG-QR не отправился, отправляю текстовый QR:\n"
                f"<pre>{ascii_qr}</pre>",
                parse_mode="HTML",
            )
            return True
        except Exception as e2:
            logger.warning("Failed to send ASCII QR fallback: %s", e2)
            return False


# ── Keyboards ──────────────────────────────────────────────────────

def _has_groq() -> bool:
    return bool(_cfg().vision.api_key)


def main_menu_kb() -> InlineKeyboardMarkup:
    paused = _bot_instance and _bot_instance.userbot and _bot_instance.userbot.paused
    pause_text = "▶️ Запуск" if paused else "⏸ Пауза"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📡 Мониторинг", callback_data="monitoring"),
            InlineKeyboardButton(text="✉️ Авто-DM", callback_data="autodm"),
        ],
        [
            InlineKeyboardButton(text="📋 История", callback_data="history"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        ],
        [
            InlineKeyboardButton(text=pause_text, callback_data="toggle_pause"),
            InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
        ],
    ])


def back_kb(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=target)],
    ])


# ── Dashboard helper ───────────────────────────────────────────────

def _dashboard_text() -> str:
    chats_count = len(_cfg().monitoring.chats)
    kw_count = len(_cfg().monitoring.keywords)
    price = _cfg().monitoring.max_price
    price_str = f"💰 до {price:,}₽".replace(",", " ") if price else ""
    paused = _bot_instance and _bot_instance.userbot and _bot_instance.userbot.paused
    status = "⏸ Пауза" if paused else "✅ Активен"
    return status, chats_count, kw_count, price_str


# ── /start ─────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    if _bot_instance.owner_id is None:
        _bot_instance.owner_id = message.from_user.id
        await _db().set_setting("owner_id", str(message.from_user.id))
    stats = await _db().get_stats()
    status, chats_count, kw_count, price_str = _dashboard_text()
    text = (
        f"{status} | 📡 {chats_count} чат(ов) | 🔑 {kw_count} слов\n"
        f"🔍 {stats['total_matches']} находок | ✉️ {stats['total_dms']} DM"
    )
    if price_str:
        text += f" | {price_str}"
    await message.answer(text, reply_markup=main_menu_kb())


# ── Menu callback ──────────────────────────────────────────────────

@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    stats = await _db().get_stats()
    status, chats_count, kw_count, price_str = _dashboard_text()
    text = (
        f"{status} | 📡 {chats_count} чат(ов) | 🔑 {kw_count} слов\n"
        f"🔍 {stats['total_matches']} находок | ✉️ {stats['total_dms']} DM"
    )
    if price_str:
        text += f" | {price_str}"
    await callback.message.edit_text(text, reply_markup=main_menu_kb())
    await callback.answer()


# ── 📡 Мониторинг (chats + keywords + synonyms + price) ──────────

def _format_keyword_with_synonyms(kw: str) -> str:
    """Format keyword with its synonyms from keyword_map."""
    kmap = _cfg().rules.keyword_map
    val = kmap.get(kw)
    if isinstance(val, list) and val:
        syns = ", ".join(val)
        return f"  • {kw} → {syns}"
    return f"  • {kw}"


@router.callback_query(F.data == "monitoring")
async def cb_monitoring(callback: CallbackQuery):
    cfg = _cfg()
    chats = cfg.monitoring.chats
    keywords = cfg.monitoring.keywords
    price = cfg.monitoring.max_price

    parts = ["<b>📡 Мониторинг</b>\n"]

    if chats:
        parts.append("📡 <b>Чаты:</b>")
        for i, c in enumerate(chats):
            parts.append(f"  {i+1}. {c}")
    else:
        parts.append("📡 Нет чатов")

    parts.append("")
    if keywords:
        parts.append("🔑 <b>Слова + синонимы:</b>")
        for kw in keywords:
            parts.append(_format_keyword_with_synonyms(kw))
    else:
        parts.append("🔑 Нет ключевых слов")

    price_str = f"{price:,}₽".replace(",", " ") if price else "без ограничений"
    parts.append(f"\n💰 Макс. цена: {price_str}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Чат", callback_data="chat_add"),
            InlineKeyboardButton(text="➖ Чат", callback_data="chat_del"),
        ],
        [
            InlineKeyboardButton(text="➕ Слово", callback_data="kw_add"),
            InlineKeyboardButton(text="➖ Слово", callback_data="kw_del"),
        ],
        [
            InlineKeyboardButton(text="🏷 Синонимы", callback_data="synonyms"),
            InlineKeyboardButton(text="💰 Цена", callback_data="max_price"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu")],
    ])
    await callback.message.edit_text("\n".join(parts), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ── Chats (sub of Мониторинг) ──────────────────────────────────────

@router.callback_query(F.data == "chats")
async def cb_chats(callback: CallbackQuery):
    await cb_monitoring(callback)


@router.callback_query(F.data == "chat_add")
async def cb_chat_add(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "chat_add"
    await callback.message.edit_text(
        "Введите @username/ID чата или перешлите сообщение из нужного чата:",
        reply_markup=back_kb("monitoring"),
    )
    await callback.answer()


@router.callback_query(F.data == "chat_del")
async def cb_chat_del(callback: CallbackQuery):
    chats = _cfg().monitoring.chats
    if not chats:
        await callback.answer("Список пуст", show_alert=True)
        return
    _bot_instance.awaiting[callback.from_user.id] = "chat_del"
    lines = [f"  {i+1}. {c}" for i, c in enumerate(chats)]
    await callback.message.edit_text(
        "Введите номер чата для удаления:\n" + "\n".join(lines),
        reply_markup=back_kb("monitoring"),
    )
    await callback.answer()


# ── Keywords (sub of Мониторинг) ───────────────────────────────────

@router.callback_query(F.data == "keywords")
async def cb_keywords(callback: CallbackQuery):
    await cb_monitoring(callback)


@router.callback_query(F.data == "kw_add")
async def cb_kw_add(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "kw_add"
    await callback.message.edit_text(
        "Введите ключевое слово для добавления:",
        reply_markup=back_kb("monitoring"),
    )
    await callback.answer()


@router.callback_query(F.data == "kw_del")
async def cb_kw_del(callback: CallbackQuery):
    kws = _cfg().monitoring.keywords
    if not kws:
        await callback.answer("Список пуст", show_alert=True)
        return
    _bot_instance.awaiting[callback.from_user.id] = "kw_del"
    lines = [f"  {i+1}. {k}" for i, k in enumerate(kws)]
    await callback.message.edit_text(
        "Введите номер слова для удаления:\n" + "\n".join(lines),
        reply_markup=back_kb("monitoring"),
    )
    await callback.answer()


# ── Synonyms ──────────────────────────────────────────────────────

@router.callback_query(F.data == "synonyms")
async def cb_synonyms(callback: CallbackQuery):
    kws = _cfg().monitoring.keywords
    if not kws:
        await callback.answer("Сначала добавьте ключевые слова", show_alert=True)
        return

    buttons = []
    for kw in kws:
        kmap = _cfg().rules.keyword_map
        val = kmap.get(kw)
        count = len(val) if isinstance(val, list) else 0
        buttons.append([InlineKeyboardButton(
            text=f"🏷 {kw} ({count} синонимов)",
            callback_data=f"syn_show:{kw}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="monitoring")])

    await callback.message.edit_text(
        "🏷 <b>Синонимы</b>\nВыберите слово для управления синонимами:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("syn_show:"))
async def cb_syn_show(callback: CallbackQuery):
    kw = callback.data.split(":", 1)[1]
    kmap = _cfg().rules.keyword_map
    val = kmap.get(kw)
    syns = val if isinstance(val, list) else []

    if syns:
        lines = [f"  {i+1}. {s}" for i, s in enumerate(syns)]
        text = f"🏷 <b>Синонимы для «{kw}»:</b>\n" + "\n".join(lines)
    else:
        text = f"🏷 У слова «{kw}» нет синонимов."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"syn_add:{kw}"),
            InlineKeyboardButton(text="➖ Удалить", callback_data=f"syn_del:{kw}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="synonyms")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("syn_add:"))
async def cb_syn_add(callback: CallbackQuery):
    kw = callback.data.split(":", 1)[1]
    _bot_instance.awaiting[callback.from_user.id] = f"syn_add:{kw}"
    await callback.message.edit_text(
        f"Введите синоним для «{kw}» (через запятую для нескольких):",
        reply_markup=back_kb("synonyms"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("syn_del:"))
async def cb_syn_del(callback: CallbackQuery):
    kw = callback.data.split(":", 1)[1]
    kmap = _cfg().rules.keyword_map
    val = kmap.get(kw)
    syns = val if isinstance(val, list) else []
    if not syns:
        await callback.answer("Список пуст", show_alert=True)
        return
    _bot_instance.awaiting[callback.from_user.id] = f"syn_del:{kw}"
    lines = [f"  {i+1}. {s}" for i, s in enumerate(syns)]
    await callback.message.edit_text(
        f"Введите номер для удаления:\n" + "\n".join(lines),
        reply_markup=back_kb("synonyms"),
    )
    await callback.answer()


# ── Max price (sub of Мониторинг) ──────────────────────────────────

@router.callback_query(F.data == "max_price")
async def cb_max_price(callback: CallbackQuery):
    price = _cfg().monitoring.max_price
    price_str = f"{price:,}".replace(",", " ") if price else "не задана"
    _bot_instance.awaiting[callback.from_user.id] = "max_price"
    await callback.message.edit_text(
        f"💰 Макс. цена: {price_str} ₽\nВведите новую максимальную цену (0 = без ограничений):",
        reply_markup=back_kb("monitoring"),
    )
    await callback.answer()


# ── 📋 История ────────────────────────────────────────────────────

@router.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery):
    rows = await _db().get_recent_matches(5)
    if rows:
        lines = []
        for r in rows:
            dup = "🔄" if r["is_duplicate"] else "🔔"
            price_str = f"{r['price']:,}₽".replace(",", " ") if r.get("price") else "—"
            lines.append(
                f"{dup} {r['match_type']}({r.get('matched_value','')}) "
                f"| {price_str} | {r['created_at'][-8:]}"
            )
        text = "<b>📋 История</b>\n\n" + "\n".join(lines)
    else:
        text = "<b>📋 История</b>\n\nПока нет совпадений."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Все находки", callback_data="recent"),
            InlineKeyboardButton(text="📜 Лог действий", callback_data="actions_log"),
        ],
        [InlineKeyboardButton(text="🧪 Тест pipeline", callback_data="test")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "test")
async def cb_test(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "test"
    await callback.message.edit_text(
        "🧪 Отправьте текст или фото для тестирования pipeline:",
        reply_markup=back_kb("history"),
    )
    await callback.answer()


# ── Recent matches ─────────────────────────────────────────────────

@router.callback_query(F.data == "recent")
async def cb_recent(callback: CallbackQuery):
    rows = await _db().get_recent_matches(10)
    if not rows:
        text = "📋 Пока нет совпадений."
    else:
        lines = []
        for r in rows:
            dup = "🔄" if r["is_duplicate"] else "🔔"
            price_str = f"{r['price']:,}₽".replace(",", " ") if r.get("price") else "—"
            lines.append(
                f"{dup} {r['match_type']}({r.get('matched_value','')}) "
                f"| {price_str} | {r['created_at']}"
            )
        text = "📋 Последние находки:\n" + "\n".join(lines)

    await callback.message.edit_text(text, reply_markup=back_kb("history"))
    await callback.answer()


# ── ⚙️ Настройки (adaptive: hides AI buttons without Groq key) ────

@router.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    cfg = _cfg()
    dry_run_status = "вкл" if cfg.actions.dry_run else "выкл"
    notify = cfg.actions.notify_chat_id

    buttons = []

    # AI buttons — only if Groq key is set
    if _has_groq():
        vision_status = "вкл" if cfg.rules.vision_enabled else "выкл"
        nlp_status = "вкл" if cfg.monitoring.use_text_nlp else "выкл"
        groq_dm_status = "вкл" if cfg.actions.use_groq_dm else "выкл"
        buttons.append([
            InlineKeyboardButton(text=f"👁 Vision: {vision_status}", callback_data="toggle_vision"),
            InlineKeyboardButton(text=f"🧬 NLP: {nlp_status}", callback_data="toggle_text_nlp"),
        ])
        buttons.append([
            InlineKeyboardButton(text=f"🤖 Groq DM: {groq_dm_status}", callback_data="toggle_groq_dm"),
            InlineKeyboardButton(text=f"🧪 Dry-run: {dry_run_status}", callback_data="toggle_dry_run"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text=f"🧪 Dry-run: {dry_run_status}", callback_data="toggle_dry_run"),
        ])

    buttons.extend([
        [
            InlineKeyboardButton(text="📊 Лимиты", callback_data="limits"),
            InlineKeyboardButton(text="👥 Списки", callback_data="lists"),
        ],
        [
            InlineKeyboardButton(text="🔔 Уведомления", callback_data="set_notify"),
            InlineKeyboardButton(text="🔐 Авторизация", callback_data="auth_userbot"),
        ],
        [
            InlineKeyboardButton(text="🔄 Перезапуск", callback_data="restart_bot"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu")],
    ])

    parts = [f"⚙️ <b>Настройки</b>\n"]
    if _has_groq():
        parts.append(f"👁 Vision: {vision_status} | 🧬 NLP: {nlp_status} | 🤖 Groq DM: {groq_dm_status}")
    parts.append(f"🧪 Dry-run: {dry_run_status} | 🔔 Уведомления: {notify}")

    await callback.message.edit_text(
        "\n".join(parts),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "toggle_vision")
async def cb_toggle_vision(callback: CallbackQuery):
    _cfg().rules.vision_enabled = not _cfg().rules.vision_enabled
    _save_config()
    status = "вкл" if _cfg().rules.vision_enabled else "выкл"
    await callback.answer(f"Vision: {status}", show_alert=True)
    await cb_settings(callback)


@router.callback_query(F.data == "set_notify")
async def cb_set_notify(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "set_notify"
    await callback.message.edit_text(
        "Введите chat_id для уведомлений (или 'me' для себя):",
        reply_markup=back_kb("settings"),
    )
    await callback.answer()


@router.callback_query(F.data == "restart_bot")
async def cb_restart_bot(callback: CallbackQuery):
    import os, signal
    await callback.answer("Перезапускаю…", show_alert=False)
    await callback.message.edit_text("🔄 Перезапускаю бота, подождите ~10 секунд и откройте /start…")
    os.kill(os.getpid(), signal.SIGTERM)


@router.callback_query(F.data == "auth_userbot")
async def cb_auth_userbot(callback: CallbackQuery):
    if not _bot_instance.userbot:
        await callback.answer("Userbot недоступен", show_alert=True)
        return

    await callback.answer()

    try:
        link = await _bot_instance.userbot.create_qr_login_link()
    except Exception as e:
        logger.error("Failed to generate userbot auth link: %s", e)
        await callback.message.edit_text(
            "❌ Не удалось подготовить авторизацию userbot.",
            reply_markup=back_kb("settings"),
        )
        return

    if not link:
        started = await _bot_instance.userbot.start()
        text = "✅ Userbot уже авторизован и запущен." if started else "✅ Userbot уже авторизован."
        await callback.message.edit_text(text, reply_markup=back_kb("settings"))
        return

    await callback.message.edit_text(
        "🔐 Подтвердите вход по QR или по ссылке ниже.\n"
        "QR/ссылка одноразовые и действуют ~2 минуты.\n\n"
        "⚠️ <b>Важно:</b> если у вас включена двухфакторная авторизация (2FA) в Telegram, "
        "временно отключите её перед авторизацией, затем включите обратно.",
        reply_markup=back_kb("settings"),
        parse_mode="HTML",
    )
    sent_qr = await _send_qr_image(callback.message, link)
    if not sent_qr:
        await callback.message.answer("⚠️ Не удалось отправить QR-код изображением, использую ссылку.")
    await callback.message.answer(f"🔗 {link}")
    await callback.message.answer("⏳ Жду подтверждения авторизации...")

    result = await _bot_instance.userbot.wait_qr_login(timeout=180)
    if result == "ok":
        started = await _bot_instance.userbot.start()
        if started:
            await callback.message.answer("✅ Авторизация успешна, мониторинг запущен.")
        else:
            await callback.message.answer("✅ Авторизация успешна, но запуск userbot не удался.")
    elif result == "need_2fa":
        await callback.message.answer(
            "🔐 Для входа требуется пароль 2FA.\n\n"
            "<b>Инструкция:</b>\n"
            "1. Откройте Telegram → Настройки → Конфиденциальность → Двухэтапная аутентификация\n"
            "2. Отключите пароль 2FA\n"
            "3. Нажмите «🔐 Авторизация» ещё раз\n"
            "4. После успешной авторизации включите 2FA обратно\n\n"
            "Это безопасно — сессия userbot сохраняется, 2FA можно вернуть сразу.",
            parse_mode="HTML",
        )
    elif result == "timeout":
        try:
            status = await _bot_instance.userbot.request_login_code()
        except Exception:
            status = "error"
        if status == "already_authorized":
            started = await _bot_instance.userbot.start()
            msg = "✅ Userbot уже авторизован и запущен." if started else "✅ Userbot уже авторизован."
            await callback.message.answer(msg)
        elif status == "sent":
            _bot_instance.awaiting[callback.from_user.id] = "auth_code"
            await callback.message.answer(
                "⌛ Ссылка не подтверждена вовремя. Переключаю на ввод кода вручную.\n"
                "Введите код из сообщения от аккаунта Telegram:"
            )
        else:
            await callback.message.answer("⌛ Время ожидания истекло. Нажмите «🔐 Авторизация» ещё раз.")
    else:
        await callback.message.answer("❌ Авторизация не завершена.")


@router.callback_query(F.data == "auth_userbot_code")
async def cb_auth_userbot_code(callback: CallbackQuery):
    if not _bot_instance.userbot:
        await callback.answer("Userbot недоступен", show_alert=True)
        return

    await callback.answer()
    try:
        status = await _bot_instance.userbot.request_login_code()
    except Exception as e:
        logger.error("Failed to request login code: %s", e)
        await callback.message.edit_text("❌ Не удалось запросить код авторизации.", reply_markup=back_kb("settings"))
        return

    if status == "already_authorized":
        started = await _bot_instance.userbot.start()
        text = "✅ Userbot уже авторизован и запущен." if started else "✅ Userbot уже авторизован."
        await callback.message.edit_text(text, reply_markup=back_kb("settings"))
        return

    _bot_instance.awaiting[callback.from_user.id] = "auth_code"
    await callback.message.edit_text(
        "🔢 Введите код из сообщения от аккаунта Telegram.\n"
        "Код приходит в официальном Telegram (обычно не SMS).\n"
        "Ваше сообщение с кодом будет удалено после обработки.\n\n"
        "⚠️ Если у вас включена 2FA — временно отключите её перед авторизацией.",
        reply_markup=back_kb("settings"),
    )


# ── Pause / Resume ─────────────────────────────────────────────────

@router.callback_query(F.data == "toggle_pause")
async def cb_toggle_pause(callback: CallbackQuery):
    if _bot_instance.userbot:
        _bot_instance.userbot.paused = not _bot_instance.userbot.paused
        status = "⏸ Приостановлен" if _bot_instance.userbot.paused else "▶️ Запущен"
        await callback.answer(status, show_alert=True)
    await cb_menu(callback)


# ── ✉️ Авто-DM ────────────────────────────────────────────────────

@router.callback_query(F.data == "autodm")
async def cb_autodm(callback: CallbackQuery):
    cfg = _cfg()
    auto_dm = "вкл" if cfg.actions.auto_dm else "выкл"
    delay_range = f"{cfg.actions.dm_delay_min}–{cfg.actions.dm_delay_max}с"
    cooldown = cfg.actions.dm_cooldown_hours
    tpl = cfg.actions.dm_template[:60] + "…" if len(cfg.actions.dm_template) > 60 else cfg.actions.dm_template

    text = (
        f"✉️ <b>Авто-DM</b>\n\n"
        f"Статус: {auto_dm}\n"
        f"📝 «{tpl}»\n"
        f"⏱ Задержка: {delay_range}\n"
        f"🔁 Повтор через: {cooldown} ч"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✉️ Авто-DM: {auto_dm}", callback_data="toggle_auto_dm")],
        [InlineKeyboardButton(text="📝 Шаблон", callback_data="edit_dm_template")],
        [
            InlineKeyboardButton(text=f"⏱ Задержка: {delay_range}", callback_data="set_dm_delay"),
            InlineKeyboardButton(text=f"🔁 Повтор: {cooldown}ч", callback_data="set_dm_cooldown"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

# redirect old actions_menu to autodm
@router.callback_query(F.data == "actions_menu")
async def cb_actions_menu(callback: CallbackQuery):
    await cb_autodm(callback)


@router.callback_query(F.data == "toggle_auto_dm")
async def cb_toggle_auto_dm(callback: CallbackQuery):
    _cfg().actions.auto_dm = not _cfg().actions.auto_dm
    _save_config()
    status = "вкл" if _cfg().actions.auto_dm else "выкл"
    await callback.answer(f"Авто-DM: {status}", show_alert=True)
    await cb_autodm(callback)


@router.callback_query(F.data == "toggle_dry_run")
async def cb_toggle_dry_run(callback: CallbackQuery):
    _cfg().actions.dry_run = not _cfg().actions.dry_run
    _save_config()
    status = "вкл" if _cfg().actions.dry_run else "выкл"
    await callback.answer(f"Dry-run: {status}", show_alert=True)
    await cb_settings(callback)


@router.callback_query(F.data == "toggle_text_nlp")
async def cb_toggle_text_nlp(callback: CallbackQuery):
    _cfg().monitoring.use_text_nlp = not _cfg().monitoring.use_text_nlp
    _save_config()
    status = "вкл" if _cfg().monitoring.use_text_nlp else "выкл"
    await callback.answer(f"NLP: {status}", show_alert=True)
    await cb_settings(callback)


@router.callback_query(F.data == "toggle_groq_dm")
async def cb_toggle_groq_dm(callback: CallbackQuery):
    _cfg().actions.use_groq_dm = not _cfg().actions.use_groq_dm
    _save_config()
    status = "вкл" if _cfg().actions.use_groq_dm else "выкл"
    await callback.answer(f"Groq DM: {status}", show_alert=True)
    await cb_settings(callback)


@router.callback_query(F.data == "set_dm_delay")
async def cb_set_dm_delay(callback: CallbackQuery):
    cfg = _cfg()
    _bot_instance.awaiting[callback.from_user.id] = "set_dm_delay"
    await callback.message.edit_text(
        f"⏱ Текущая задержка: {cfg.actions.dm_delay_min}–{cfg.actions.dm_delay_max} с\n\n"
        "Введите диапазон в формате <code>МИН МАКС</code> (в секундах), например:\n"
        "<code>60 120</code> — от 1 до 2 минут\n"
        "<code>30 60</code> — от 30 секунд до 1 минуты\n"
        "<code>0 0</code> — без задержки",
        reply_markup=back_kb("autodm"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "set_dm_cooldown")
async def cb_set_dm_cooldown(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "set_dm_cooldown"
    await callback.message.edit_text(
        f"🔁 Текущий период повтора DM: {_cfg().actions.dm_cooldown_hours} ч\n\n"
        "Введите количество часов (0 = никогда не повторять):",
        reply_markup=back_kb("autodm"),
    )
    await callback.answer()


@router.callback_query(F.data == "edit_dm_template")
async def cb_edit_dm_template(callback: CallbackQuery):
    current = _cfg().actions.dm_template
    _bot_instance.awaiting[callback.from_user.id] = "edit_dm_template"
    await callback.message.edit_text(
        f"📝 Текущий шаблон DM:\n<code>{current}</code>\n\n"
        "Плейсхолдеры: {type}, {price}, {link}, {author}, {chat_title}, {message_snippet}\n\n"
        "Введите новый шаблон:",
        reply_markup=back_kb("autodm"),
        parse_mode="HTML",
    )
    await callback.answer()


# ── 👥 Списки ─────────────────────────────────────────────────────

@router.callback_query(F.data == "lists")
async def cb_lists(callback: CallbackQuery):
    opt_out = _cfg().rules.opt_out_list or []
    no_dedup = _cfg().actions.no_dedup_ids or []

    parts = ["👥 <b>Списки</b>\n"]
    if opt_out:
        lines = [f"  {i+1}. {uid}" for i, uid in enumerate(opt_out)]
        parts.append("🚫 <b>Чёрный список</b> (не писать DM):\n" + "\n".join(lines))
    else:
        parts.append("🚫 Чёрный список: пуст")
    if no_dedup:
        lines = [f"  {i+1}. {uid}" for i, uid in enumerate(no_dedup)]
        parts.append("\n✅ <b>Белый список</b> (игнор cooldown):\n" + "\n".join(lines))
    else:
        parts.append("\n✅ Белый список: пуст")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚫 ➕", callback_data="opt_out_add"),
            InlineKeyboardButton(text="🚫 ➖", callback_data="opt_out_del"),
        ],
        [
            InlineKeyboardButton(text="✅ ➕", callback_data="no_dedup_add"),
            InlineKeyboardButton(text="✅ ➖", callback_data="no_dedup_del"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")],
    ])
    await callback.message.edit_text("\n".join(parts), reply_markup=kb, parse_mode="HTML")
    await callback.answer()

# redirect old callbacks
@router.callback_query(F.data == "opt_out_list")
async def cb_opt_out_list(callback: CallbackQuery):
    await cb_lists(callback)

@router.callback_query(F.data == "no_dedup_list")
async def cb_no_dedup_list(callback: CallbackQuery):
    await cb_lists(callback)


@router.callback_query(F.data == "opt_out_add")
async def cb_opt_out_add(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "opt_out_add"
    await callback.message.edit_text(
        "Введите user_id для добавления в чёрный список:",
        reply_markup=back_kb("lists"),
    )
    await callback.answer()


@router.callback_query(F.data == "opt_out_del")
async def cb_opt_out_del(callback: CallbackQuery):
    opt_out = _cfg().rules.opt_out_list
    if not opt_out:
        await callback.answer("Список пуст", show_alert=True)
        return
    _bot_instance.awaiting[callback.from_user.id] = "opt_out_del"
    lines = [f"  {i+1}. {uid}" for i, uid in enumerate(opt_out)]
    await callback.message.edit_text(
        "Введите номер для удаления:\n" + "\n".join(lines),
        reply_markup=back_kb("lists"),
    )
    await callback.answer()


@router.callback_query(F.data == "no_dedup_add")
async def cb_no_dedup_add(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "no_dedup_add"
    await callback.message.edit_text(
        "Введите user_id для добавления в белый список:",
        reply_markup=back_kb("lists"),
    )
    await callback.answer()


@router.callback_query(F.data == "no_dedup_del")
async def cb_no_dedup_del(callback: CallbackQuery):
    ids = _cfg().actions.no_dedup_ids or []
    if not ids:
        await callback.answer("Список пуст", show_alert=True)
        return
    _bot_instance.awaiting[callback.from_user.id] = "no_dedup_del"
    lines = [f"  {i+1}. {uid}" for i, uid in enumerate(ids)]
    await callback.message.edit_text(
        "Введите номер для удаления:\n" + "\n".join(lines),
        reply_markup=back_kb("lists"),
    )
    await callback.answer()


# ── Actions Log ────────────────────────────────────────────────

@router.callback_query(F.data == "actions_log")
async def cb_actions_log(callback: CallbackQuery):
    logs = await _db().get_actions_log(limit=20)
    if not logs:
        text = "📜 Лог действий пуст."
    else:
        lines = []
        for log in logs:
            icon = "✉️" if log["action_type"] == "dm" else "📤" if log["action_type"] == "forward" else "🔄"
            result_icon = "✅" if log["result"] == "success" else "❌"
            lines.append(
                f"{icon} {result_icon} {log['action_type']} | {log['timestamp'][:16]}"
            )
        text = "📜 Последние действия:\n" + "\n".join(lines)

    await callback.message.edit_text(text, reply_markup=back_kb("history"), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    text = (
        "❓ <b>Справка</b>\n\n"
        "Бот мониторит Telegram-чаты через userbot (Telethon). "
        "При совпадении ключевого слова, синонима или распознавании товара на фото — "
        "отправляет DM продавцу и уведомляет вас.\n\n"

        "<b>📡 Мониторинг</b>\n"
        "Чаты, ключевые слова, синонимы и макс. цена — всё в одном разделе.\n"
        "Чат можно добавить через <code>@username</code>, ID или пересылку сообщения.\n"
        "Синонимы расширяют поиск: «колонка» → акустика, speaker, JBL.\n"
        "Совпадение по основе слова (стемминг): «колонку» = «колонка» = «колонки».\n\n"

        "<b>✉️ Авто-DM</b>\n"
        "Шаблон сообщения с плейсхолдерами: "
        "<code>{type}</code> <code>{price}</code> <code>{link}</code>\n"
        "Задержка 60–120с имитирует живого человека.\n"
        "Повтор: через N часов одному продавцу можно написать снова.\n\n"

        "<b>📋 История</b>\n"
        "Последние совпадения, лог DM, тест pipeline (текст или фото).\n\n"

        "<b>⚙️ Настройки</b>\n"
    )
    if _has_groq():
        text += (
            "👁 Vision — распознавание товаров на фото (Groq API)\n"
            "🧬 NLP — семантический анализ текста\n"
            "🤖 Groq DM — генерация персонализированных сообщений\n"
        )
    text += (
        "🧪 Dry-run — тестовый режим без реальных DM\n"
        "📊 Лимиты — квоты DM/Vision/NLP\n"
        "👥 Списки — чёрный (не писать) и белый (игнор cooldown)\n"
        "🔐 Авторизация — вход по QR-коду или SMS-коду\n"
        "🔄 Перезапуск — мягкий SIGTERM, systemd перезапустит сервис"
    )
    await callback.message.edit_text(text, reply_markup=back_kb("menu"), parse_mode="HTML")
    await callback.answer()


# ── Limits ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "limits")
async def cb_limits(callback: CallbackQuery):
    from bot.vision import _groq_rate_info

    cfg = _cfg()
    dm = _bot_instance.dm_limiter
    vis = _bot_instance.vision_limiter

    dm_line = (
        f"✉️ DM: {dm.remaining}/{dm.max_tokens} осталось в час"
        if dm else "✉️ DM: недоступно"
    )
    vis_retry = f" (сброс через {vis.retry_after:.0f}с)" if vis and vis.retry_after > 0 else ""
    vis_line = (
        f"👁 Vision: {vis.remaining}/{vis.max_tokens} осталось в мин{vis_retry}"
        if vis else "👁 Vision: недоступно"
    )

    if _groq_rate_info:
        rem_req = _groq_rate_info.get("remaining_requests", "?")
        lim_req = _groq_rate_info.get("limit_requests", "?")
        rem_tok = _groq_rate_info.get("remaining_tokens", "?")
        lim_tok = _groq_rate_info.get("limit_tokens", "?")
        reset = _groq_rate_info.get("reset_requests", "?")
        groq_lines = (
            f"\n🌐 <b>Groq API</b> (данные из последнего ответа):\n"
            f"  Запросы: {rem_req}/{lim_req}\n"
            f"  Токены:  {rem_tok}/{lim_tok}\n"
            f"  Сброс:   {reset}"
        )
    else:
        groq_lines = "\n🌐 <b>Groq API</b>: нет данных (Vision ещё не вызывался)"

    text = f"📊 <b>Лимиты</b>\n\n{dm_line}"

    buttons = [
        [InlineKeyboardButton(
            text=f"✉️ DM/час: {cfg.rate_limits.dm_per_hour}",
            callback_data="edit_dm_limit",
        )],
    ]

    if _has_groq():
        text += f"\n{vis_line}{groq_lines}"
        buttons.append(
            [InlineKeyboardButton(
                text=f"👁 Vision/мин: {cfg.rate_limits.vision_per_minute}",
                callback_data="edit_vision_limit",
            )]
        )
        buttons.append(
            [InlineKeyboardButton(
                text=f"🧬 NLP/мин: {cfg.monitoring.text_nlp_per_minute}",
                callback_data="edit_nlp_limit",
            )]
        )

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "edit_dm_limit")
async def cb_edit_dm_limit(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "edit_dm_limit"
    await callback.message.edit_text(
        f"✉️ Текущий лимит DM: {_cfg().rate_limits.dm_per_hour}/час\n"
        "Введите новое значение:",
        reply_markup=back_kb("limits"),
    )
    await callback.answer()


@router.callback_query(F.data == "edit_vision_limit")
async def cb_edit_vision_limit(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "edit_vision_limit"
    await callback.message.edit_text(
        f"👁 Текущий лимит Vision: {_cfg().rate_limits.vision_per_minute}/мин\n"
        "Введите новое значение:",
        reply_markup=back_kb("limits"),
    )
    await callback.answer()


@router.callback_query(F.data == "edit_nlp_limit")
async def cb_edit_nlp_limit(callback: CallbackQuery):
    _bot_instance.awaiting[callback.from_user.id] = "edit_nlp_limit"
    await callback.message.edit_text(
        f"🧬 Текущий лимит NLP: {_cfg().monitoring.text_nlp_per_minute}/мин\n"
        "Введите новое значение:",
        reply_markup=back_kb("limits"),
    )
    await callback.answer()


# ── Text input handler ─────────────────────────────────────────────

@router.message(F.forward_origin | F.forward_from_chat)
async def handle_forwarded_input(message: Message):
    action = _bot_instance.awaiting.get(message.from_user.id)
    if action != "chat_add":
        return
    _bot_instance.awaiting.pop(message.from_user.id, None)
    text = (message.text or message.caption or "").strip()
    ok = await _handle_chat_add(message, fallback_text=text)
    if not ok:
        _bot_instance.awaiting[message.from_user.id] = "chat_add"
        return
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_input(message: Message):
    action = _bot_instance.awaiting.pop(message.from_user.id, None)
    if not action:
        return

    text = (message.text or message.caption or "").strip()
    if action in {"auth_code", "auth_2fa"}:
        try:
            await message.delete()
        except Exception:
            pass

    if action == "chat_add":
        ok = await _handle_chat_add(message, fallback_text=text)
        if not ok:
            _bot_instance.awaiting[message.from_user.id] = "chat_add"
            return

    elif action == "chat_del":
        try:
            idx = int(text) - 1
            removed = _cfg().monitoring.chats.pop(idx)
            _save_config()
            await message.answer(f"✅ Чат {removed} удалён.")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Перезапустить", callback_data="restart_bot")],
            ])
            await message.answer("⚠️ Для применения перезапустите бота:", reply_markup=kb)
        except (ValueError, IndexError):
            await message.answer("❌ Неверный номер.")

    elif action == "kw_add":
        _cfg().monitoring.keywords.append(text)
        _save_config()
        if _bot_instance.userbot:
            _bot_instance.userbot.matcher.update(
                _cfg().monitoring.keywords,
                keyword_map=_cfg().rules.keyword_map,
            )
        await message.answer(f"✅ Ключевое слово «{text}» добавлено.")

    elif action == "kw_del":
        try:
            idx = int(text) - 1
            removed = _cfg().monitoring.keywords.pop(idx)
            _save_config()
            if _bot_instance.userbot:
                _bot_instance.userbot.matcher.update(
                    _cfg().monitoring.keywords,
                    keyword_map=_cfg().rules.keyword_map,
                )
            await message.answer(f"✅ Слово «{removed}» удалено.")
        except (ValueError, IndexError):
            await message.answer("❌ Неверный номер.")

    elif action == "max_price":
        try:
            new_price = int(text.replace(" ", "").replace("₽", "").replace("р", ""))
            _cfg().monitoring.max_price = new_price
            _save_config()
            await message.answer(f"✅ Макс. цена: {new_price:,} ₽".replace(",", " "))
        except ValueError:
            await message.answer("❌ Введите число.")

    elif action == "set_notify":
        if text.lower() == "me":
            _cfg().actions.notify_chat_id = "me"
        else:
            try:
                _cfg().actions.notify_chat_id = int(text)
            except ValueError:
                _cfg().actions.notify_chat_id = text
        _save_config()
        await message.answer(f"✅ Уведомления: {_cfg().actions.notify_chat_id}")

    elif action == "auth_code":
        if not _bot_instance.userbot:
            await message.answer("❌ Userbot недоступен.")
        else:
            result = await _bot_instance.userbot.sign_in_with_code(text)
            if result == "ok":
                started = await _bot_instance.userbot.start()
                if started:
                    await message.answer("✅ Авторизация успешна, мониторинг запущен.")
                else:
                    await message.answer("✅ Авторизация успешна, но userbot не запущен.")
            elif result == "need_2fa":
                _bot_instance.awaiting[message.from_user.id] = "auth_2fa"
                await message.answer("🔐 Введите пароль 2FA:")
                return
            elif result == "invalid_code":
                _bot_instance.awaiting[message.from_user.id] = "auth_code"
                await message.answer("❌ Неверный код. Попробуйте ещё раз:")
                return
            elif result == "expired_code":
                try:
                    await _bot_instance.userbot.request_login_code()
                    _bot_instance.awaiting[message.from_user.id] = "auth_code"
                    await message.answer("⌛ Код истёк. Отправил новый, введите его:")
                    return
                except Exception:
                    await message.answer("❌ Код истёк и не удалось запросить новый.")
            else:
                await message.answer("❌ Не удалось авторизоваться по коду.")

    elif action == "auth_2fa":
        # 2FA password input is no longer accepted for security
        await message.answer(
            "🔐 Ввод пароля 2FA через бота отключён по соображениям безопасности.\n\n"
            "<b>Инструкция:</b>\n"
            "1. Откройте Telegram → Настройки → Конфиденциальность → Двухэтапная аутентификация\n"
            "2. Отключите пароль 2FA\n"
            "3. Нажмите «🔐 Авторизация» ещё раз\n"
            "4. После успешной авторизации включите 2FA обратно",
            parse_mode="HTML",
        )

    elif action == "test":
        from bot.keywords import KeywordMatcher
        from bot.price import extract_price as ep
        matcher = KeywordMatcher(
            _cfg().monitoring.keywords,
            keyword_map=_cfg().rules.keyword_map,
        )
        kw = matcher.match(text)
        price = ep(text)
        if kw:
            resolved = matcher.resolve_key(kw) or kw
            await message.answer(f"🧪 Результат:\n🏷 Keyword: {resolved}\n💰 Цена: {price or '—'}")
        else:
            await message.answer("🧪 Результат: совпадений по тексту нет.")

    elif action == "edit_dm_template":
        _cfg().actions.dm_template = text
        _save_config()
        await message.answer(f"✅ Шаблон DM обновлён:\n{text}")

    elif action == "opt_out_add":
        try:
            user_id = int(text)
            if user_id not in _cfg().rules.opt_out_list:
                _cfg().rules.opt_out_list.append(user_id)
                _save_config()
                await message.answer(f"✅ User {user_id} добавлен в opt-out")
            else:
                await message.answer(f"⚠️ User {user_id} уже в списке")
        except ValueError:
            await message.answer("❌ Введите число (user_id)")

    elif action == "opt_out_del":
        try:
            idx = int(text) - 1
            removed = _cfg().rules.opt_out_list.pop(idx)
            _save_config()
            await message.answer(f"✅ User {removed} удалён из opt-out")
        except (ValueError, IndexError):
            await message.answer("❌ Неверный номер.")

    elif action == "set_dm_delay":
        parts = text.split()
        try:
            mn, mx = int(parts[0]), int(parts[1]) if len(parts) > 1 else int(parts[0])
            if mn < 0 or mx < mn:
                raise ValueError
            _cfg().actions.dm_delay_min = mn
            _cfg().actions.dm_delay_max = mx
            _save_config()
            await message.answer(f"✅ Задержка: {mn}–{mx} с")
        except (ValueError, IndexError):
            await message.answer("❌ Формат: МИН МАКС (числа, например: 60 120)")

    elif action == "set_dm_cooldown":
        try:
            hours = int(text)
            if hours < 0:
                raise ValueError
            _cfg().actions.dm_cooldown_hours = hours
            _save_config()
            label = f"{hours} ч" if hours > 0 else "никогда не повторять"
            await message.answer(f"✅ Повтор DM через: {label}")
        except ValueError:
            await message.answer("❌ Введите целое число часов (0 = никогда)")

    elif action == "no_dedup_add":
        try:
            user_id = int(text)
            ids = _cfg().actions.no_dedup_ids
            if user_id not in ids:
                ids.append(user_id)
                _save_config()
                await message.answer(f"✅ User {user_id} добавлен (без дедупа)")
            else:
                await message.answer(f"⚠️ User {user_id} уже в списке")
        except ValueError:
            await message.answer("❌ Введите число (user_id)")

    elif action == "no_dedup_del":
        try:
            idx = int(text) - 1
            removed = _cfg().actions.no_dedup_ids.pop(idx)
            _save_config()
            await message.answer(f"✅ User {removed} удалён из белого списка")
        except (ValueError, IndexError):
            await message.answer("❌ Неверный номер.")

    elif action.startswith("syn_add:"):
        kw = action.split(":", 1)[1]
        kmap = _cfg().rules.keyword_map
        if kw not in kmap:
            kmap[kw] = []
        new_syns = [s.strip() for s in text.split(",") if s.strip()]
        for s in new_syns:
            if s not in kmap[kw]:
                kmap[kw].append(s)
        _save_config()
        if _bot_instance.userbot:
            _bot_instance.userbot.matcher.update(
                _cfg().monitoring.keywords,
                keyword_map=_cfg().rules.keyword_map,
            )
        await message.answer(f"✅ Добавлено {len(new_syns)} синонимов для «{kw}»")

    elif action.startswith("syn_del:"):
        kw = action.split(":", 1)[1]
        kmap = _cfg().rules.keyword_map
        syns = kmap.get(kw, [])
        try:
            idx = int(text) - 1
            removed = syns.pop(idx)
            _save_config()
            if _bot_instance.userbot:
                _bot_instance.userbot.matcher.update(
                    _cfg().monitoring.keywords,
                    keyword_map=_cfg().rules.keyword_map,
                )
            await message.answer(f"✅ Синоним «{removed}» удалён из «{kw}»")
        except (ValueError, IndexError):
            await message.answer("❌ Неверный номер.")

    elif action == "edit_dm_limit":
        try:
            val = int(text)
            if val < 1:
                raise ValueError
            _cfg().rate_limits.dm_per_hour = val
            _save_config()
            await message.answer(f"✅ Лимит DM: {val}/час")
        except ValueError:
            await message.answer("❌ Введите положительное число")

    elif action == "edit_vision_limit":
        try:
            val = int(text)
            if val < 1:
                raise ValueError
            _cfg().rate_limits.vision_per_minute = val
            _save_config()
            await message.answer(f"✅ Лимит Vision: {val}/мин")
        except ValueError:
            await message.answer("❌ Введите положительное число")

    elif action == "edit_nlp_limit":
        try:
            val = int(text)
            if val < 1:
                raise ValueError
            _cfg().monitoring.text_nlp_per_minute = val
            _save_config()
            await message.answer(f"✅ Лимит NLP: {val}/мин")
        except ValueError:
            await message.answer("❌ Введите положительное число")

    await message.answer("Главное меню:", reply_markup=main_menu_kb())


# ── Photo test handler ─────────────────────────────────────────────

@router.message(F.photo)
async def handle_photo_input(message: Message):
    action = _bot_instance.awaiting.pop(message.from_user.id, None)
    if action == "chat_add":
        text = (message.caption or "").strip()
        ok = await _handle_chat_add(message, fallback_text=text)
        if not ok:
            _bot_instance.awaiting[message.from_user.id] = "chat_add"
            return
        await message.answer("Главное меню:", reply_markup=main_menu_kb())
        return

    if action != "test":
        if action:
            _bot_instance.awaiting[message.from_user.id] = action
        return

    if not _cfg().rules.vision_enabled or not _cfg().vision.api_key:
        await message.answer("🧪 Vision выключен или нет API key.")
        return

    await message.answer("🧪 Анализирую фото...")
    photo = message.photo[-1]
    file = await message.bot.download(photo)
    image_bytes = file.read()

    from bot.vision import analyse_image, parse_vision_response
    reply = await analyse_image(image_bytes, _cfg().monitoring.vision_prompt, _cfg().vision)
    if reply:
        result = parse_vision_response(reply)
        if result:
            await message.answer(f"🧪 Vision результат:\n🏷 Тип: {result['type']}\n💰 Цена: {result.get('price', '—')}")
        else:
            await message.answer(f"🧪 Vision ответ: НЕТ (не совпадение)\nRaw: {reply[:200]}")
    else:
        await message.answer("🧪 Vision API не ответил.")

    await message.answer("Главное меню:", reply_markup=main_menu_kb())


# ── Config persistence ─────────────────────────────────────────────

def _save_config():
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(_cfg().model_dump(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed to save config: %s", e)


# ── ControlBot class ───────────────────────────────────────────────

class ControlBot:
    def __init__(self, config: Config, db: Database, dm_limiter=None, vision_limiter=None):
        self.config = config
        self.db = db
        self.userbot = None  # set externally after init
        self.awaiting: dict[int, str] = {}  # user_id -> action
        self.owner_id: int | None = None   # set on first /start
        self.dm_limiter = dm_limiter
        self.vision_limiter = vision_limiter

        self.bot = Bot(token=config.telegram.bot_token)
        self.dp = Dispatcher()
        self.dp.include_router(router)

        global _bot_instance
        _bot_instance = self

    async def send_notification(self, text: str):
        """Send notification to the configured chat (or owner for 'me')."""
        chat_id = self.config.actions.notify_chat_id
        if chat_id == "me":
            if self.owner_id:
                try:
                    await self.bot.send_message(chat_id=self.owner_id, text=text)
                except Exception as e:
                    logger.error("Failed to send notification to owner: %s", e)
            else:
                logger.info("Notification (owner unknown, send /start first): %s", text[:80])
            return
        try:
            await self.bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

    async def start(self):
        logger.info("Control bot starting...")
        saved = await self.db.get_setting("owner_id")
        if saved:
            self.owner_id = int(saved)
            logger.info("Loaded owner_id=%s from DB", saved)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def stop(self):
        await self.dp.stop_polling()
        await self.bot.session.close()
        logger.info("Control bot stopped")
