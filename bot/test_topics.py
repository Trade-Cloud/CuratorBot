"""Test post "Topic <id> - <name>" into every topic from .env. Needs only BOT_TOKEN, CHAT_ID and TOPIC_*.

    python -m bot.test_topics
"""

from __future__ import annotations

import asyncio
import re

from .__main__ import make_bot
from .config import get_settings
from .services.topic_scan import test_configured_topics


async def main() -> None:
    settings = get_settings()
    bot = make_bot(settings)
    try:
        lines = await test_configured_topics(bot, settings)
    finally:
        await bot.session.close()
    for line in lines:
        print(re.sub(r"</?\w+>", "", line).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


if __name__ == "__main__":
    asyncio.run(main())
