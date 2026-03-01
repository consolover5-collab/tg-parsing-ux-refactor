"""Seller deduplication: one DM per unique seller_id."""

from db.database import Database


class DedupChecker:
    def __init__(self, db: Database):
        self._db = db

    async def is_seen(self, seller_id: int, cooldown_hours: int = 25) -> bool:
        return await self._db.is_seller_seen(seller_id, cooldown_hours)

    async def register(
        self,
        seller_id: int,
        chat: str,
        msg_id: int,
        match_type: str,
        matched_value: str,
        price: int | None,
    ) -> bool:
        """Register new seller. Returns True if inserted, False if duplicate."""
        return await self._db.add_seller(
            seller_id, chat, msg_id, match_type, matched_value, price
        )

    async def record_match(
        self,
        seller_id: int,
        chat_id: int,
        message_id: int,
        match_type: str,
        matched_value: str,
        price: int | None,
        is_duplicate: bool = False,
    ):
        await self._db.add_match(
            seller_id, chat_id, message_id, match_type, matched_value, price, is_duplicate
        )

    async def mark_dm_sent(self, seller_id: int):
        await self._db.mark_dm_sent(seller_id)
