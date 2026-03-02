"""Telethon userbot: monitors chats in real-time, sends DMs to sellers."""

import asyncio
import logging
import random
import time

from telethon import TelegramClient, events
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import Message

from bot.models import Config, ForwardMode
from bot.keywords import KeywordMatcher
from bot.price import extract_price
from bot.dedup import DedupChecker
from bot.ratelimit import RateLimiter
from bot.vision import analyse_image, parse_vision_response
from bot.nlp import analyse_text, generate_dm, looks_like_listing
from bot.processor import MessageProcessor
from db.database import Database

logger = logging.getLogger(__name__)


class Userbot:
    def __init__(
        self,
        config: Config,
        dedup: DedupChecker,
        dm_limiter: RateLimiter,
        vision_limiter: RateLimiter,
        db: Database,
        notify_callback=None,
    ):
        self.config = config
        self.dedup = dedup
        self.dm_limiter = dm_limiter
        self.vision_limiter = vision_limiter
        self.db = db
        self.notify = notify_callback  # async fn(text: str)
        self.matcher = KeywordMatcher(
            config.monitoring.keywords,
            keyword_map=config.rules.keyword_map,
        )
        self.nlp_limiter = RateLimiter(
            max_tokens=config.monitoring.text_nlp_per_minute,
            period_seconds=60.0,
        )
        self.paused = False
        self.processor = MessageProcessor(config, db)

        self.client = TelegramClient(
            config.telegram.session_name,
            config.telegram.api_id,
            config.telegram.api_hash,
        )

        # album debounce: group_id -> list[Message]
        self._album_buf: dict[int, list[Message]] = {}
        self._album_tasks: dict[int, asyncio.Task] = {}
        self._started = False
        self._pending_qr_login = None
        self._phone_code_hash: str | None = None
        self._phone_code_requested_at: float | None = None
        self._last_code_delivery: str | None = None
        self._last_code_timeout: int | None = None

    async def start(self) -> bool:
        if self._started:
            return True
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logger.error(
                "Userbot is not authorized yet. "
                "Use control bot: Settings -> Authorize userbot."
            )
            return False
        self.client.session.save()
        logger.info("Userbot authorised as %s", (await self.client.get_me()).first_name)

        chats = self.config.monitoring.chats
        if not chats:
            logger.warning("No chats to monitor")
            self._started = True
            return True

        # Pre-resolve chat entities to numeric IDs for reliable event filtering
        resolved_ids = []
        for chat in chats:
            try:
                entity = await self.client.get_input_entity(chat)
                resolved_ids.append(entity)
                logger.debug("Resolved chat %s -> %s", chat, entity)
            except Exception as e:
                logger.warning("Cannot resolve chat %s: %s", chat, e)
        if not resolved_ids:
            logger.error("No chats could be resolved, monitoring disabled")
            self._started = True
            return True

        @self.client.on(events.NewMessage(chats=resolved_ids, incoming=True))
        async def on_message(event: events.NewMessage.Event):
            try:
                if self.paused:
                    return
                msg: Message = event.message

                # Album grouping
                if msg.grouped_id:
                    await self._handle_album(msg)
                else:
                    await self._process_message(msg)
            except Exception:
                logger.exception("on_message error")

        logger.info("Monitoring %d chats: %s", len(chats), chats)
        self._started = True
        return True

    async def create_qr_login_link(self) -> str | None:
        await self.client.connect()
        if await self.client.is_user_authorized():
            return None
        self._pending_qr_login = await self.client.qr_login()
        return self._pending_qr_login.url

    async def wait_qr_login(self, timeout: int = 120) -> str:
        if not self._pending_qr_login:
            return "no_qr"
        try:
            await self._pending_qr_login.wait(timeout=timeout)
            self._pending_qr_login = None
            self.client.session.save()
            return "ok" if await self.client.is_user_authorized() else "failed"
        except asyncio.TimeoutError:
            self._pending_qr_login = None
            return "timeout"
        except SessionPasswordNeededError:
            self._pending_qr_login = None
            return "need_2fa"

    async def request_login_code(self, force_sms: bool = False) -> str:
        await self.client.connect()
        if await self.client.is_user_authorized():
            return "already_authorized"

        now = time.time()
        if self._phone_code_hash and self._phone_code_requested_at and now - self._phone_code_requested_at > 180:
            logger.info("request_login_code: dropping stale hash")
            self._phone_code_hash = None

        # Reuse pending request unless user explicitly asks for SMS fallback.
        if self._phone_code_hash and not force_sms:
            logger.info("request_login_code: reusing pending hash")
            return "already_sent"

        try:
            sent = await self.client.send_code_request(self.config.telegram.phone, force_sms=force_sms)
            if getattr(sent, "phone_code_hash", None):
                self._phone_code_hash = sent.phone_code_hash
            self._phone_code_requested_at = now
            self._last_code_delivery = type(getattr(sent, "type", None)).__name__
            timeout = getattr(sent, "timeout", None)
            self._last_code_timeout = int(timeout) if isinstance(timeout, (int, float)) else None
            logger.info(
                "request_login_code: type=%s force_sms=%s timeout=%s",
                self._last_code_delivery,
                force_sms,
                self._last_code_timeout,
            )
            return "sms_sent" if force_sms else "sent"
        except Exception as e:
            msg = str(e).lower()
            if "resendcoderequest" in msg or "all available options for this type of number were already used" in msg:
                logger.warning("request_login_code: resend limited by Telegram")
                return "already_sent" if self._phone_code_hash else "resend_limited"
            logger.error("request_login_code failed: %s", e)
            return "error"

    async def sign_in_with_code(self, code: str) -> str:
        code = "".join(code.split())  # strip spaces (user types "1 2 3 4 5")
        await self.client.connect()
        if not self._phone_code_hash:
            logger.warning("sign_in_with_code: no hash set")
            return "no_code_request"
        try:
            logger.info(f"sign_in_with_code: attempting with hash={self._phone_code_hash[:10]}...")
            await self.client.sign_in(
                self.config.telegram.phone,
                code,
                phone_code_hash=self._phone_code_hash,
            )
            self._phone_code_hash = None
            self._phone_code_requested_at = None
            self._last_code_delivery = None
            self._last_code_timeout = None
            self.client.session.save()
            return "ok" if await self.client.is_user_authorized() else "failed"
        except PhoneCodeInvalidError:
            return "invalid_code"
        except PhoneCodeExpiredError:
            self._phone_code_hash = None
            self._phone_code_requested_at = None
            self._last_code_delivery = None
            self._last_code_timeout = None
            return "expired_code"
        except SessionPasswordNeededError:
            return "need_2fa"
        except Exception as e:
            logger.error("Code sign-in failed: %s", e)
            return "error"

    def get_code_delivery_hint(self) -> str:
        t = self._last_code_delivery or ""
        if "Sms" in t:
            base = "Код отправлен по SMS."
        elif "App" in t:
            base = "Код отправлен в Telegram (сервисный чат 777000)."
        elif "Call" in t:
            base = "Код будет продиктован звонком Telegram."
        elif t:
            base = f"Тип доставки кода: {t}."
        else:
            base = ""
        if self._last_code_timeout:
            base = (base + f" Повторный запрос обычно доступен через ~{self._last_code_timeout}с.").strip()
        return base

    async def sign_in_with_password(self, password: str) -> str:
        try:
            await self.client.sign_in(password=password)
            self.client.session.save()
            return "ok" if await self.client.is_user_authorized() else "failed"
        except PasswordHashInvalidError:
            return "invalid_2fa"
        except Exception as e:
            logger.error("2FA sign-in failed: %s", e)
            return "error"

    async def stop(self):
        # Cancel pending album tasks
        for task in self._album_tasks.values():
            task.cancel()
        self._album_tasks.clear()
        self._album_buf.clear()
        await self.client.disconnect()
        self._started = False
        logger.info("Userbot disconnected")

    # ── Album debounce ─────────────────────────────────────────────

    async def _handle_album(self, msg: Message):
        gid = msg.grouped_id
        if gid not in self._album_buf:
            self._album_buf[gid] = []
        self._album_buf[gid].append(msg)

        # Reset debounce timer
        if gid in self._album_tasks:
            self._album_tasks[gid].cancel()
        self._album_tasks[gid] = asyncio.create_task(self._flush_album(gid))

    async def _flush_album(self, gid: int):
        await asyncio.sleep(1.5)  # wait for all parts
        messages = self._album_buf.pop(gid, [])
        self._album_tasks.pop(gid, None)
        if messages:
            # Use first message with text, process photos from first message
            primary = messages[0]
            for m in messages:
                if m.text:
                    primary = m
                    break
            try:
                await self._process_message(primary)
            except Exception:
                logger.exception("_flush_album error (group_id=%s)", gid)

    # ── Main pipeline ──────────────────────────────────────────────

    def _msg_link(self, msg: Message) -> str:
        """Build a clickable Telegram message link."""
        try:
            if msg.chat.username:
                return f"https://t.me/{msg.chat.username}/{msg.id}"
        except AttributeError:
            pass
        cid = abs(msg.chat_id)
        if cid > 1_000_000_000:
            cid = int(str(cid)[3:])  # strip -100 prefix for t.me/c/ format
        return f"https://t.me/c/{cid}/{msg.id}"

    async def _process_message(self, msg: Message):
        seller_id = msg.sender_id
        if not seller_id:
            return
        seller_username = getattr(msg.sender, 'username', None) if msg.sender else None

        chat = await self._chat_title(msg)
        chat_external = str(msg.chat_id)
        match_type = None
        matched_value = None
        price = None
        nlp_dm_text: str | None = None

        # Step 1: keyword/synonym match on text
        if msg.text:
            kw = self.matcher.match(msg.text)
            if kw:
                match_type = "keyword"
                # Resolve synonym stem back to category key (e.g. "акустик" → "колонка")
                resolved = self.matcher.resolve_key(kw)
                if resolved:
                    matched_value = resolved
                else:
                    kmap_val = self.config.rules.keyword_map.get(kw)
                    matched_value = kmap_val if isinstance(kmap_val, str) else kw
                price = extract_price(msg.text)

        # Step 1b: Groq text NLP fallback (when keywords miss + looks like a listing)
        if not match_type and msg.text and self.config.monitoring.use_text_nlp:
            if looks_like_listing(msg.text):
                targets = self.config.monitoring.keywords or list(self.config.rules.keyword_map.keys())
                nlp = await analyse_text(msg.text, targets, self.config, self.nlp_limiter)
                if nlp and nlp["match"]:
                    match_type = "nlp"
                    matched_value = nlp["type"] or targets[0] if targets else "товар"
                    price = price or nlp["price"]
                    nlp_dm_text = nlp["dm"] or None

        # Step 2: vision (if no match yet and photo present)
        if not match_type and msg.photo and self.config.rules.vision_enabled:
            vision_result = await self._try_vision(msg)
            if vision_result:
                match_type = "vision"
                matched_value = vision_result.get("type", "")
                price = vision_result.get("price")

        if not match_type:
            return  # no match

        # Step 3: price filter
        max_price = self.config.monitoring.max_price
        if max_price and price and price > max_price:
            logger.debug("Price %d > max %d, skipping", price, max_price)
            return

        # Prepare metadata
        link = self._msg_link(msg)
        meta = {
            "type": matched_value,
            "price": price,
            "link": link,
            "author": f"@{seller_username}" if seller_username else str(seller_id),
            "chat_title": chat,
            "source_chat": chat_external,
            "message_snippet": (msg.text or "")[:200],
            "match_type": match_type,
            "matched_value": matched_value,
        }

        # Store message
        msg_uuid = await self.processor.store_message(
            chat_external=chat_external,
            chat_title=chat,
            message_id=msg.id,
            author_id=seller_id,
            text=msg.text or "",
            meta=meta
        )

        # Step 4: dedup check (bypass for no_dedup_ids)
        _no_dedup_ids = self.config.actions.no_dedup_ids or []
        no_dedup = seller_id in _no_dedup_ids
        if not no_dedup and seller_username:
            _un = seller_username.lower()
            no_dedup = any(
                isinstance(e, str) and e.lstrip('@').lower() == _un
                for e in _no_dedup_ids
            )
        is_dup = False if no_dedup else await self.dedup.is_seen(
            seller_id, cooldown_hours=self.config.actions.dm_cooldown_hours
        )

        if is_dup:
            await self.dedup.record_match(
                seller_id, msg.chat_id, msg.id,
                match_type, matched_value, price, is_duplicate=True,
            )
            await self._notify_duplicate(chat, seller_id)
            await self.db.log_action(msg_uuid, "duplicate", "skipped", {"reason": "seller in cooldown"})
        else:
            if not no_dedup:
                await self.dedup.register(
                    seller_id, chat, msg.id, match_type, matched_value, price,
                )
            await self.dedup.record_match(
                seller_id, msg.chat_id, msg.id,
                match_type, matched_value, price, is_duplicate=False,
            )

            # Decide actions
            actions = self.processor.decide_actions(chat_external, seller_id, meta, username=seller_username)

            # Override DM text with Groq-generated version if applicable
            if actions.get("should_dm") and self.config.actions.use_groq_dm:
                groq_text = nlp_dm_text  # already generated during NLP step
                if not groq_text:
                    groq_text = await generate_dm(
                        msg.text or matched_value, matched_value, self.config, price=price
                    )
                if groq_text:
                    actions = dict(actions, dm_text=groq_text)

            dm_sent = False
            forward_sent = False

            if actions["should_forward"]:
                forward_sent = await self._forward_message(msg, meta)
                await self.db.log_action(
                    msg_uuid, "forward",
                    "success" if forward_sent else "failed",
                    {"mode": self.config.actions.forward_mode.value, "dry_run": self.config.actions.dry_run}
                )

            if actions["should_dm"]:
                dm_sent = await self._send_dm_with_template(seller_id, actions["dm_text"])
                await self.db.log_action(
                    msg_uuid, "dm",
                    "success" if dm_sent else "failed",
                    {"template_used": True, "groq_dm": self.config.actions.use_groq_dm,
                     "dry_run": self.config.actions.dry_run}
                )

            await self._notify_new(chat, match_type, matched_value, price, dm_sent, msg, forward_sent)

    # ── Vision ─────────────────────────────────────────────────────

    async def _try_vision(self, msg: Message) -> dict | None:
        if not self.config.vision.api_key:
            return None

        # Caption pre-filter: skip photos that don't look like listings
        if self.config.monitoring.vision_require_listing_signal:
            caption = (msg.text or msg.message or "").strip()
            if not looks_like_listing(caption):
                logger.debug("Vision skipped: caption has no listing signal")
                return None

        if not self.vision_limiter.consume():
            logger.debug("Vision rate limit reached, skipping")
            return None

        # Auto-generate vision prompt from current keywords
        kw_list = ", ".join(self.config.monitoring.keywords[:15]) or "товар"
        prompt = (
            f"На фото один из товаров: {kw_list}? "
            "Если да — ответь: ТИП: ..., ЦЕНА: ... Если нет — ответь: НЕТ"
        )

        try:
            photo_bytes = await self.client.download_media(msg, bytes)
            if not photo_bytes:
                return None
            reply = await analyse_image(
                photo_bytes,
                prompt,
                self.config.vision,
            )
            return parse_vision_response(reply) if reply else None
        except Exception as e:
            logger.error("Vision processing error: %s", e)
            return None


    # ── DM ─────────────────────────────────────────────────────────

    async def _send_dm_with_template(self, seller_id: int, text: str) -> bool:
        """Send DM with rendered template, with human-like delay."""
        if not self.dm_limiter.consume():
            logger.warning("DM rate limit reached for seller %d", seller_id)
            return False

        # Human-like delay
        delay_min = self.config.actions.dm_delay_min
        delay_max = max(delay_min, self.config.actions.dm_delay_max)
        delay = random.uniform(delay_min, delay_max)

        if self.config.actions.dry_run:
            logger.info(
                "[DRY RUN] Would send DM to %d in %.0fs: %s", seller_id, delay, text[:100]
            )
            return True

        logger.info("Waiting %.0fs before DM to seller %d (human-like delay)", delay, seller_id)
        await asyncio.sleep(delay)

        try:
            await self.client.send_message(seller_id, text)
            await self.dedup.mark_dm_sent(seller_id)
            logger.info("DM sent to seller %d", seller_id)
            return True
        except Exception as e:
            logger.error("Failed to send DM to %d: %s", seller_id, e)
            return False

    async def _forward_message(self, msg: Message, meta: dict) -> bool:
        """Forward message to main bot or send notification."""
        notify_chat_id = self.config.actions.notify_chat_id

        if self.config.actions.dry_run:
            logger.info("[DRY RUN] Would forward message to %s (mode=%s)", notify_chat_id, self.config.actions.forward_mode)
            return True

        try:
            if self.config.actions.forward_mode == ForwardMode.FORWARD_RAW:
                # Forward the actual message
                await self.client.forward_messages(notify_chat_id, msg)
                logger.info("Message forwarded to %s", notify_chat_id)
                return True
            else:
                # Send notification with metadata
                notification_text = self.processor.format_notification(meta, self.config.actions.forward_mode)
                if notification_text and self.notify:
                    await self.notify(notification_text)
                    logger.info("Notification sent")
                    return True
                return False
        except Exception as e:
            logger.error("Failed to forward message: %s", e)
            return False

    # ── Notifications ──────────────────────────────────────────────

    async def _notify_new(self, chat, match_type, matched_value, price, dm_sent, msg, forward_sent=False):
        # In notify_with_meta mode, _forward_message already sent a full notification
        if forward_sent and self.config.actions.forward_mode == ForwardMode.NOTIFY_WITH_META:
            return
        price_str = f"{price:,} ₽".replace(",", " ") if price else "—"
        dm_str = "✉️ DM отправлен продавцу" if dm_sent else "⚠️ DM не отправлен (лимит или выключен)"
        forward_str = "📤 Переслано" if forward_sent else ""
        link = self._msg_link(msg)

        parts = [
            f"🔔 Новое совпадение!",
            f"📍 Чат: {chat}",
            f"🏷 Тип: {match_type} ({matched_value})",
            f"💰 Цена: {price_str}",
        ]

        if dm_sent:
            parts.append(dm_str)
        if forward_sent:
            parts.append(forward_str)

        parts.append(f"🔗 {link}")

        text = "\n".join(parts)

        if self.notify:
            await self.notify(text)

    async def _notify_duplicate(self, chat, seller_id):
        text = (
            f"🔄 Повтор от того же продавца\n"
            f"📍 Чат: {chat}\n"
            f"ℹ️ DM уже отправлялся ранее"
        )
        if self.notify:
            await self.notify(text)

    # ── Helpers ─────────────────────────────────────────────────────

    async def _chat_title(self, msg: Message) -> str:
        try:
            chat = await self.client.get_entity(msg.chat_id)
            return getattr(chat, "title", None) or getattr(chat, "username", str(msg.chat_id))
        except Exception:
            return str(msg.chat_id)
