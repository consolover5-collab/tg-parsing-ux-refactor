import aiosqlite
import os
import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_sellers (
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

CREATE TABLE IF NOT EXISTS matches (
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

CREATE TABLE IF NOT EXISTS stats (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id          TEXT PRIMARY KEY,
    external    TEXT UNIQUE NOT NULL,
    title       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pools (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pool_chats (
    pool_id     TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    PRIMARY KEY (pool_id, chat_id),
    FOREIGN KEY (pool_id) REFERENCES pools(id),
    FOREIGN KEY (chat_id) REFERENCES chats(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    source_chat TEXT NOT NULL,
    author      TEXT,
    text        TEXT,
    ts          TEXT NOT NULL,
    meta        TEXT,
    FOREIGN KEY (chat_id) REFERENCES chats(id)
);

CREATE TABLE IF NOT EXISTS actions_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT,
    action_type TEXT NOT NULL,
    result      TEXT NOT NULL,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    details     TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # Migrations (safe on existing DBs)
        for stmt in [
            "ALTER TABLE seen_sellers ADD COLUMN dm_sent_at TEXT",
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        ]:
            try:
                await self._db.execute(stmt)
                await self._db.commit()
            except Exception:
                pass

    async def close(self):
        if self._db:
            await self._db.close()

    async def get_setting(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def set_setting(self, key: str, value: str):
        await self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self._db.commit()

    async def is_seller_seen(self, seller_id: int, cooldown_hours: int = 25) -> bool:
        """Return True if seller was DM'd within cooldown_hours (0 = forever)."""
        if cooldown_hours == 0:
            async with self._db.execute(
                "SELECT 1 FROM seen_sellers WHERE seller_id = ? AND dm_sent = 1", (seller_id,)
            ) as cur:
                return await cur.fetchone() is not None
        async with self._db.execute(
            "SELECT dm_sent_at FROM seen_sellers WHERE seller_id = ? AND dm_sent = 1",
            (seller_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["dm_sent_at"] is None:
            return False
        async with self._db.execute(
            "SELECT 1 WHERE datetime(?) > datetime('now', ?)",
            (row["dm_sent_at"], f"-{cooldown_hours} hours"),
        ) as cur:
            return await cur.fetchone() is not None

    async def add_seller(
        self, seller_id: int, chat: str, msg_id: int,
        match_type: str, matched_value: str, price: int | None
    ) -> bool:
        try:
            await self._db.execute(
                "INSERT INTO seen_sellers (seller_id, first_chat, first_msg_id, match_type, matched_value, price) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (seller_id, chat, msg_id, match_type, matched_value, price),
            )
            await self._db.commit()
            return True
        except aiosqlite.IntegrityError:
            # Seller already in table — update record for new listing
            await self._db.execute(
                "UPDATE seen_sellers SET first_chat=?, first_msg_id=?, match_type=?, "
                "matched_value=?, price=?, dm_sent=0, dm_sent_at=NULL WHERE seller_id=?",
                (chat, msg_id, match_type, matched_value, price, seller_id),
            )
            await self._db.commit()
            return True

    async def mark_dm_sent(self, seller_id: int):
        await self._db.execute(
            "UPDATE seen_sellers SET dm_sent = 1, dm_sent_at = datetime('now') WHERE seller_id = ?",
            (seller_id,),
        )
        await self._db.commit()

    async def add_match(
        self, seller_id: int, chat_id: int, message_id: int,
        match_type: str, matched_value: str, price: int | None,
        is_duplicate: bool = False
    ):
        await self._db.execute(
            "INSERT INTO matches (seller_id, chat_id, message_id, match_type, matched_value, price, is_duplicate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seller_id, chat_id, message_id, match_type, matched_value, price, int(is_duplicate)),
        )
        await self._db.commit()

    async def get_recent_matches(self, limit: int = 10) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM matches ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_stats(self) -> dict:
        total_matches = 0
        total_dms = 0
        total_sellers = 0

        async with self._db.execute("SELECT COUNT(*) FROM matches") as cur:
            row = await cur.fetchone()
            total_matches = row[0] if row else 0

        async with self._db.execute("SELECT COUNT(*) FROM seen_sellers WHERE dm_sent = 1") as cur:
            row = await cur.fetchone()
            total_dms = row[0] if row else 0

        async with self._db.execute("SELECT COUNT(*) FROM seen_sellers") as cur:
            row = await cur.fetchone()
            total_sellers = row[0] if row else 0

        return {
            "total_matches": total_matches,
            "total_dms": total_dms,
            "total_sellers": total_sellers,
        }

    # ── New methods for extended schema ────────────────────────────────

    async def get_or_create_chat(self, external: str, title: str = None) -> str:
        """Get or create chat by external ID, return internal UUID."""
        import uuid

        async with self._db.execute(
            "SELECT id FROM chats WHERE external = ?", (external,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]

        chat_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO chats (id, external, title) VALUES (?, ?, ?)",
            (chat_id, external, title)
        )
        await self._db.commit()
        return chat_id

    async def add_message(
        self, msg_id: str, chat_id: str, source_chat: str,
        author: str, text: str, ts: str, meta: dict = None
    ):
        """Store processed message with metadata."""
        meta_json = json.dumps(meta) if meta else None
        await self._db.execute(
            "INSERT INTO messages (id, chat_id, source_chat, author, text, ts, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, chat_id, source_chat, author, text, ts, meta_json)
        )
        await self._db.commit()

    async def log_action(
        self, message_id: str, action_type: str, result: str, details: dict = None
    ):
        """Log an action (forward/dm) attempt."""
        details_json = json.dumps(details) if details else None
        await self._db.execute(
            "INSERT INTO actions_log (message_id, action_type, result, details) "
            "VALUES (?, ?, ?, ?)",
            (message_id, action_type, result, details_json)
        )
        await self._db.commit()

    async def get_actions_log(self, limit: int = 50) -> list[dict]:
        """Get recent action logs."""
        async with self._db.execute(
            "SELECT * FROM actions_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("details"):
                    try:
                        d["details"] = json.loads(d["details"])
                    except:
                        pass
                result.append(d)
            return result

    async def create_pool(self, pool_id: str, title: str):
        """Create a pool."""
        await self._db.execute(
            "INSERT INTO pools (id, title) VALUES (?, ?)",
            (pool_id, title)
        )
        await self._db.commit()

    async def add_chat_to_pool(self, pool_id: str, chat_id: str):
        """Add chat to pool."""
        await self._db.execute(
            "INSERT OR IGNORE INTO pool_chats (pool_id, chat_id) VALUES (?, ?)",
            (pool_id, chat_id)
        )
        await self._db.commit()

    async def get_pool_chats(self, pool_id: str) -> list[str]:
        """Get chats in a pool."""
        async with self._db.execute(
            "SELECT chat_id FROM pool_chats WHERE pool_id = ?", (pool_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

