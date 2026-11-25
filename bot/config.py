"""Settings loaded from the environment (or a .env file next to docker-compose).

Non-secret, non-instance tunables live in config.json at the project root and are used as the
defaults below; the environment (.env) still overrides any of them. Private values — tokens,
database/Redis URLs, chat and admin ids, passwords, API keys — stay in .env only.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
CONFIG: dict = json.loads(CONFIG_PATH.read_text("utf-8"))


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _join(values: list[str]) -> str:
    return ",".join(values)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    # --- Telegram ---
    bot_token: SecretStr
    # A self-hosted Bot API server (telegram-bot-api) lifts the 20 MB download limit.
    telegram_api_url: str | None = None

    # Beta chat (supergroup with topics), in the form -100xxxxxxxxxx.
    chat_id: int

    # Topic ids (message_thread_id). 0 = General. None = topic not configured.
    topic_info_id: int | None = None
    topic_changelog_id: int | None = None
    topic_bugs_id: int | None = None
    topic_features_id: int | None = None
    topic_main_id: int | None = 0
    # Topic for short bot logs (events without details). None = do not post.
    topic_logs_id: int | None = None

    # Who sees bug details and may publish changelogs / change statuses.
    admin_ids_raw: str = Field("", alias="ADMIN_IDS")

    # Where to send moderation logs and new-report notifications (optional).
    # If unset, notifications go to the ADMIN_IDS private chats.
    admin_log_chat_id: int | None = None
    admin_log_thread_id: int | None = None

    # --- Storage ---
    database_url: str = "postgresql+asyncpg://beta_bot:beta_bot@beta_bot_postgres:5432/beta_bot"
    # FSM (unfinished forms). Empty = in memory, lost on restart.
    redis_url: str | None = None
    media_dir: Path = Path("/data/media")
    media_download_concurrency: int = CONFIG["media"]["download_concurrency"]
    # Without a self-hosted server the Bot API will not serve files > 20 MB.
    media_max_bytes: int = CONFIG["media"]["max_bytes"]

    timezone: str = CONFIG["runtime"]["timezone"]
    log_level: str = CONFIG["runtime"]["log_level"]

    # --- Main-topic moderation ---
    moderation_enabled: bool = CONFIG["moderation"]["enabled"]
    # Topics where moderation runs, comma-separated. Defaults to Main.
    moderation_topics_raw: str = Field("", alias="MODERATION_TOPICS")
    # Member tags that moderation leaves alone.
    exempt_tags_raw: str = Field(_join(CONFIG["moderation"]["exempt_tags"]), alias="EXEMPT_TAGS")
    # Leave the chat owner/admins alone even without a tag.
    exempt_chat_admins: bool = CONFIG["moderation"]["exempt_chat_admins"]
    # Allowed domains and @mentions (the bot and its own resources are added automatically).
    allowed_domains_raw: str = Field(_join(CONFIG["moderation"]["allowed_domains"]), alias="ALLOWED_DOMAINS")
    allowed_mentions_raw: str = Field(_join(CONFIG["moderation"]["allowed_mentions"]), alias="ALLOWED_MENTIONS")
    # Block any @username. Can be disabled in chats where people tag each other often.
    block_mentions: bool = CONFIG["moderation"]["block_mentions"]
    # Action: ignore | log | delete
    moderation_hard_action: str = CONFIG["moderation"]["hard_action"]
    moderation_soft_action: str = CONFIG["moderation"]["soft_action"]
    # Mute after N hard violations within the window (0 = never mute).
    moderation_mute_after: int = CONFIG["moderation"]["mute_after"]
    moderation_mute_window_hours: int = CONFIG["moderation"]["mute_window_hours"]
    moderation_mute_minutes: int = CONFIG["moderation"]["mute_minutes"]
    moderation_warning_ttl: int = CONFIG["moderation"]["warning_ttl"]

    # Where to send people who are not in the beta chat (to request beta access).
    access_bot_username: str = CONFIG["access"]["bot_username"]

    # --- Reports ---
    bug_attachments_required: bool = CONFIG["reports"]["attachments_required"]
    require_membership: bool = CONFIG["reports"]["require_membership"]
    delete_group_commands: bool = CONFIG["group"]["delete_commands"]
    group_reply_ttl: int = CONFIG["group"]["reply_ttl"]

    # --- Backup web viewer ---
    web_enabled: bool = CONFIG["web"]["enabled"]
    web_host: str = CONFIG["web"]["host"]
    web_port: int = CONFIG["web"]["port"]
    web_user: str = CONFIG["web"]["user"]
    # Empty = no password (the compose port is bound to 127.0.0.1 only).
    web_password: SecretStr | None = None

    # --- Local LLM (llama-server, OpenAI-compatible API) ---
    llm_enabled: bool = CONFIG["llm"]["enabled"]
    # Explicit address (http://host:port). Empty = autodetect, see services/llm.py.
    llm_base_url: str | None = None
    # LAN server, used when running from a Mac.
    llm_server_host: str = CONFIG["llm"]["server_host"]
    llm_port: int = CONFIG["llm"]["port"]
    # llama-server replies with whatever model is loaded; a name is only needed if it distinguishes them.
    llm_model: str | None = CONFIG["llm"]["model"]
    llm_api_key: SecretStr | None = None
    llm_timeout: float = CONFIG["llm"]["timeout"]
    bug_summary_chars: int = CONFIG["reports"]["bug_summary_chars"]

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in _csv(self.admin_ids_raw)}

    @property
    def moderation_topics(self) -> set[int]:
        raw = _csv(self.moderation_topics_raw)
        if raw:
            return {int(x) for x in raw}
        return {self.topic_main_id} if self.topic_main_id is not None else set()

    @property
    def exempt_tags(self) -> set[str]:
        return {x.casefold() for x in _csv(self.exempt_tags_raw)}

    @property
    def allowed_domains(self) -> set[str]:
        return {x.lower().lstrip(".") for x in _csv(self.allowed_domains_raw)}

    @property
    def allowed_mentions(self) -> set[str]:
        return {x.lower().lstrip("@") for x in _csv(self.allowed_mentions_raw)}

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
