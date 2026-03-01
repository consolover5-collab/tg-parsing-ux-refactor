from pydantic import BaseModel, Field
from enum import Enum


class ForwardMode(str, Enum):
    FORWARD_RAW = "forward_raw"
    NOTIFY_WITH_META = "notify_with_meta"


class TelegramConfig(BaseModel):
    api_id: int
    api_hash: str
    phone: str
    session_name: str = "session/monitor"
    bot_token: str


class MonitoringConfig(BaseModel):
    chats: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    max_price: int = 0
    vision_prompt: str = "На фото телевизор, колонка или аудиосистема? Если да — ответь: ТИП: ..., ЦЕНА: ... Если нет — ответь: НЕТ"
    vision_require_listing_signal: bool = True  # only Vision-analyse photos that look like listings
    use_text_nlp: bool = False   # Groq text NLP for semantic matching
    text_nlp_per_minute: int = 3  # max Groq NLP calls per minute


class VisionConfig(BaseModel):
    provider: str = "groq"
    api_key: str = ""
    model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    base_url: str = "https://api.groq.com/openai/v1/chat/completions"


class PerChatOverride(BaseModel):
    auto_dm: bool | None = None
    forward_to_main_bot: bool | None = None
    dm_template: str | None = None


class RulesConfig(BaseModel):
    keyword_map: dict[str, str | list[str]] = Field(default_factory=dict)
    vision_enabled: bool = True
    per_chat_overrides: dict[str, PerChatOverride] = Field(default_factory=dict)
    opt_out_list: list[int] = Field(default_factory=list)


class ActionsConfig(BaseModel):
    notify_chat_id: str | int = "me"
    auto_dm: bool = True
    forward_to_main_bot: bool = False
    forward_mode: ForwardMode = ForwardMode.NOTIFY_WITH_META
    dm_template: str = "Привет! {type} ещё доступна? Цена: {price}. Ссылка: {link}"
    dry_run: bool = False
    dm_delay_min: int = 60
    dm_delay_max: int = 120
    dm_cooldown_hours: int = 25
    no_dedup_ids: list[int] = Field(default_factory=list)
    use_groq_dm: bool = False


class RateLimitConfig(BaseModel):
    dm_per_hour: int = 15
    vision_per_minute: int = 5


class DatabaseConfig(BaseModel):
    path: str = "data/dedup.db"


class Config(BaseModel):
    telegram: TelegramConfig
    monitoring: MonitoringConfig = MonitoringConfig()
    vision: VisionConfig = VisionConfig()
    actions: ActionsConfig = ActionsConfig()
    rules: RulesConfig = RulesConfig()
    rate_limits: RateLimitConfig = RateLimitConfig()
    database: DatabaseConfig = DatabaseConfig()
