"""Шина событий для живого просмотра: бекап публикует, SSE-подписчики веб-просмотрщика читают."""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Вкладка не успевает читать — пропускаем, она перезагрузит ленту при переподключении.
                pass


bus = EventBus()
