"""Точки подключения локальной LLM. Пока НЕ реализовано — только интерфейсы и заглушки.

Куда LLM встанет:
1. Модерация Main — ещё один Analyzer в списке (services/moderation.py). Например, отдавать модели
   только сообщения с SOFT-находками и просить вердикт "это зазыв/обмен контактами или нет".
2. Короткое описание бага для поста в Bug Reports — реализация Summarizer вместо обрезки текста.
3. Поиск дублей багов/идей по смыслу — отдельный сервис, интерфейс появится, когда дойдём.

Настройки уже есть в config: LLM_ENABLED, LLM_BASE_URL, LLM_MODEL (OpenAI-совместимый API,
у Ollama/LM Studio/vLLM он есть).
"""

from __future__ import annotations

from typing import Protocol

from ..utils.telegram import truncate


class Summarizer(Protocol):
    async def summarize(self, text: str, limit: int) -> str: ...


class TruncateSummarizer:
    """Текущее поведение: просто аккуратно обрезаем."""

    async def summarize(self, text: str, limit: int) -> str:
        return truncate(text, limit)


def build_summarizer(llm_enabled: bool) -> Summarizer:
    # TODO(llm): вернуть LLM-реализацию, когда подключим локальную модель.
    return TruncateSummarizer()
