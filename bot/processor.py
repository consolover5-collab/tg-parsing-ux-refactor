"""Message processor: handles pipeline, template rendering, and action decisions."""

import logging
import uuid
from datetime import datetime

from bot.models import Config, ForwardMode
from db.database import Database

logger = logging.getLogger(__name__)


class MessageProcessor:
    """Processes messages according to configured rules and actions."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def render_template(self, template: str, meta: dict) -> str:
        """Render template with placeholders.

        Supports: {type}, {price}, {link}, {author}, {chat_title}, {message_snippet}, {source_chat}
        """
        safe_meta = {
            "type": meta.get("type", ""),
            "price": str(meta.get("price", "")) if meta.get("price") else "",
            "link": meta.get("link", ""),
            "author": meta.get("author", ""),
            "chat_title": meta.get("chat_title", ""),
            "message_snippet": meta.get("message_snippet", "")[:100],
            "source_chat": meta.get("source_chat", ""),
        }

        try:
            # Use format_map for {key} style placeholders
            return template.format_map(safe_meta)
        except KeyError as e:
            logger.warning("Missing template key: %s", e)
            # Return template with available substitutions
            for key, value in safe_meta.items():
                template = template.replace(f"{{{key}}}", value)
            return template
        except Exception as e:
            logger.error("Template rendering error: %s", e)
            return template

    def get_effective_config(self, chat_external: str) -> dict:
        """Get effective configuration considering per-chat overrides."""
        base = {
            "auto_dm": self.config.actions.auto_dm,
            "forward_to_main_bot": self.config.actions.forward_to_main_bot,
            "dm_template": self.config.actions.dm_template,
        }

        # Apply per-chat overrides
        override = self.config.rules.per_chat_overrides.get(chat_external)
        if override:
            if override.auto_dm is not None:
                base["auto_dm"] = override.auto_dm
            if override.forward_to_main_bot is not None:
                base["forward_to_main_bot"] = override.forward_to_main_bot
            if override.dm_template is not None:
                base["dm_template"] = override.dm_template

        return base

    def should_process_user(self, user_id: int) -> bool:
        """Check if user is not in opt-out list."""
        return user_id not in self.config.rules.opt_out_list

    def decide_actions(
        self, chat_external: str, user_id: int, meta: dict
    ) -> dict:
        """Decide which actions to take based on config and meta.

        Returns:
            {
                "should_dm": bool,
                "should_forward": bool,
                "dm_text": str or None,
                "reason": str (for logging)
            }
        """
        result = {
            "should_dm": False,
            "should_forward": False,
            "dm_text": None,
            "reason": ""
        }

        # Check opt-out
        if not self.should_process_user(user_id):
            result["reason"] = "user in opt-out list"
            return result

        # Get effective config
        eff_config = self.get_effective_config(chat_external)

        # Check if we should forward
        if eff_config["forward_to_main_bot"]:
            result["should_forward"] = True

        # Check if we should DM
        if eff_config["auto_dm"]:
            result["should_dm"] = True
            result["dm_text"] = self.render_template(
                eff_config["dm_template"], meta
            )

        reasons = []
        if result["should_dm"]:
            reasons.append("auto_dm=true")
        if result["should_forward"]:
            reasons.append("forward=true")
        if not reasons:
            reasons.append("no actions configured")

        result["reason"] = ", ".join(reasons)
        return result

    async def store_message(
        self, chat_external: str, chat_title: str, message_id: int,
        author_id: int, text: str, meta: dict
    ) -> str:
        """Store message in database and return UUID."""
        # Get or create internal chat ID
        internal_chat_id = await self.db.get_or_create_chat(chat_external, chat_title)

        # Generate message UUID
        msg_uuid = str(uuid.uuid4())

        # Store message
        ts = datetime.utcnow().isoformat()
        await self.db.add_message(
            msg_id=msg_uuid,
            chat_id=internal_chat_id,
            source_chat=chat_external,
            author=str(author_id),
            text=text or "",
            ts=ts,
            meta=meta
        )

        return msg_uuid

    def format_notification(self, meta: dict, mode: ForwardMode) -> str:
        """Format notification message for forward_mode=notify_with_meta."""
        if mode == ForwardMode.FORWARD_RAW:
            return None

        price_str = f"{meta.get('price', 0):,} ₽".replace(",", " ") if meta.get("price") else "—"

        text = (
            f"🔔 Новое совпадение!\n"
            f"📍 Чат: {meta.get('chat_title', meta.get('source_chat', ''))}\n"
            f"🏷 Тип: {meta.get('match_type', '')} ({meta.get('matched_value', '')})\n"
            f"💰 Цена: {price_str}\n"
            f"👤 Автор: {meta.get('author', '')}\n"
            f"🔗 {meta.get('link', '')}"
        )

        if meta.get("message_snippet"):
            text += f"\n💬 {meta['message_snippet'][:200]}"

        return text
