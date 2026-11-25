"""One-off topic discovery without starting the bot or a DB — needs only BOT_TOKEN and CHAT_ID.

    python -m bot.scan_topics            # sweep message_thread_id up to 300
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
    parser = argparse.ArgumentParser(description="Find the beta chat's topics and post each one's id into it")
    parser.add_argument("--max", type=int, default=300, help="highest message_thread_id to sweep")
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    bot = make_bot(settings)
    try:
        found = await scan_topics(bot, settings.chat_id, max_thread_id=args.max)
    finally:
        await bot.session.close()
    # To the terminal — without HTML tags.
    print(re.sub(r"</?\w+>", "", report_text(found, args.max)).replace("&lt;", "<").replace("&gt;", ">"))


if __name__ == "__main__":
    asyncio.run(main())
