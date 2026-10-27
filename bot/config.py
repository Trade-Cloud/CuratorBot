"""Настройки бота. Всё берётся из env (или .env рядом с docker-compose)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    # --- Telegram ---
    bot_token: SecretStr
    # Свой Bot API сервер (telegram-bot-api) снимает лимит 20 МБ на скачивание файлов.
    telegram_api_url: str | None = None

    # Чат беты (supergroup с топиками), вида -100xxxxxxxxxx
    chat_id: int

    # ID топиков (message_thread_id). 0 = General. None = топик не настроен.
    # Узнать ID: написать /topic_id в нужном топике (команда для админов бота).
    topic_info_id: int | None = None
    topic_changelog_id: int | None = None
    topic_bugs_id: int | None = None
    topic_features_id: int | None = None
    topic_main_id: int | None = 0

    # Кто видит детали багов и может публиковать changelog / менять статусы.
    admin_ids_raw: str = Field("", alias="ADMIN_IDS")

    # Куда слать логи модерации и уведомления о новых репортах (необязательно).
    # Если не задано — уведомления уходят в ЛС админам из ADMIN_IDS.
    admin_log_chat_id: int | None = None
    admin_log_thread_id: int | None = None

    # --- Хранилища ---
    database_url: str = "postgresql+asyncpg://beta_bot:beta_bot@beta_bot_postgres:5432/beta_bot"
    # FSM (незаконченные анкеты). Пусто = в памяти, сбрасывается при рестарте.
    redis_url: str | None = None
    media_dir: Path = Path("/data/media")
    media_download_concurrency: int = 3
    # Bot API без своего сервера не отдаёт файлы > 20 МБ.
    media_max_bytes: int = 20 * 1024 * 1024

    timezone: str = "Europe/Moscow"
    log_level: str = "INFO"

    # --- Модерация Main ---
    moderation_enabled: bool = True
    # Топики, где работает модерация, через запятую. По умолчанию — Main.
    moderation_topics_raw: str = Field("", alias="MODERATION_TOPICS")
    # Теги участников (новая фича Telegram), которых модерация не трогает.
    exempt_tags_raw: str = Field("ADMIN,MODER", alias="EXEMPT_TAGS")
    # Не трогать создателя/админов чата, даже если у них нет тега.
    exempt_chat_admins: bool = True
    # Разрешённые домены и @упоминания (бот и свои ресурсы добавятся автоматически).
    allowed_domains_raw: str = Field("trade-cloud.org", alias="ALLOWED_DOMAINS")
    allowed_mentions_raw: str = Field("", alias="ALLOWED_MENTIONS")
    # Блокировать любые @username. В чате, где люди часто тегают друг друга, можно выключить.
    block_mentions: bool = True
    # Что делать: ignore | log | delete
    moderation_hard_action: str = "delete"
    moderation_soft_action: str = "log"
    # Мут после N жёстких нарушений за окно (0 = не мутить).
    moderation_mute_after: int = 3
    moderation_mute_window_hours: int = 24
    moderation_mute_minutes: int = 60
    moderation_warning_ttl: int = 20

    # Куда отправлять тех, кого нет в бета-чате (там запрашивают доступ к бете).
    access_bot_username: str = "trade_cloud_bot"

    # --- Репорты ---
    bug_attachments_required: bool = True
    require_membership: bool = True
    delete_group_commands: bool = True
    group_reply_ttl: int = 15

    # --- Веб-просмотр бекапа ---
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_user: str = "admin"
    # Пусто = без пароля (порт в compose открыт только на 127.0.0.1).
    web_password: SecretStr | None = None

    # --- Локальная LLM (llama-server, OpenAI-совместимый API) ---
    llm_enabled: bool = True
    # Явный адрес (http://host:port). Пусто = автоопределение, см. services/llm.py.
    llm_base_url: str | None = None
    # Сервер в LAN — для запуска с Мака.
    llm_server_host: str = "192.168.1.145"
    llm_port: int = 8301
    # llama-server отвечает той моделью, что загружена; имя нужно только если сервер их различает.
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_timeout: float = 20.0
    bug_summary_chars: int = 220

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
