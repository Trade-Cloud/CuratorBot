"""Разовый поиск топиков без запуска бота и без БД — нужны только BOT_TOKEN и CHAT_ID.

    python -m bot.scan_topics            # перебор message_thread_id до 300
    python -m bot.scan_topics --max 1000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys

from .__main__ import make_bot
from .config import get_settings
from .services.topic_scan import report_text, scan_topics


async def main() -> None:
    parser = argparse.ArgumentParser(description="Найти топики чата беты и запостить в каждый его ID")
    parser.add_argument("--max", type=int, default=300, help="до какого message_thread_id перебирать")
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    bot = make_bot(settings)
    try:
        found = await scan_topics(bot, settings.chat_id, max_thread_id=args.max)
    finally:
        await bot.session.close()
    # В терминал — без HTML-тегов.
    print(re.sub(r"</?\w+>", "", report_text(found, args.max)).replace("&lt;", "<").replace("&gt;", ">"))


if __name__ == "__main__":
    asyncio.run(main())
