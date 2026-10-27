"""Проверка связи с локальной LLM и промпта для аннотации бага.

    python -m bot.llm_check
    python -m bot.llm_check "При оплате через СБП после редиректа висит белый экран, токен abc123..."
"""

from __future__ import annotations

import asyncio
import sys
import time

from .config import get_settings
from .services.llm import LLMSummarizer, LLMUnavailable, build_llm_client

SAMPLE = {
    "description": (
        "Не могу вывести деньги: в кабинете на странице Кошелёк нажимаю «Вывести», "
        "ввожу 1500 ₽ на карту 2200 1234 5678 9012, а мне пишет ошибку. Мой аккаунт ivan.petrov@mail.ru, id 48211."
    ),
    "actual": "Ответ сервера 500 {\"detail\":\"payout_failed\",\"trace\":\"a8f3e1c9d2b74e56a1f0c3d9e8b7a6f5\"}",
    "expected": "Заявка на вывод создаётся и появляется в истории",
}


async def main() -> None:
    settings = get_settings()
    client = build_llm_client(settings)
    try:
        print("Кандидаты:", ", ".join(client.candidates))
        base = await client.resolve()
        print("Выбран:", base or "— ни один не отвечает на /health")
        if not base:
            return
        try:
            print("Модели:", ", ".join(await client.models()) or "?")
        except LLMUnavailable as exc:
            print("Модели: ошибка —", exc)
            if "401" in str(exc):
                print("Сервер требует ключ: задай LLM_API_KEY в .env (тот же, что в --api-key у llm-qwen).")
            return
        fields = {"description": " ".join(sys.argv[1:])} if len(sys.argv) > 1 else SAMPLE
        print("\nВход:", fields["description"])
        started = time.monotonic()
        summary = await LLMSummarizer(client).summarize_bug(fields, settings.bug_summary_chars)
        print(f"\nАннотация ({time.monotonic() - started:.1f} с, {len(summary)} симв.):\n{summary}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
