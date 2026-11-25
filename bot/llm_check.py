"""Check connectivity to the local LLM and the bug summary prompt.

    python -m bot.llm_check
    python -m bot.llm_check "After the SBP redirect the payment page hangs on a white screen, token abc123..."
"""

from __future__ import annotations

import asyncio
import sys
import time

from .config import get_settings
from .services.llm import LLMSummarizer, LLMUnavailable, build_llm_client

SAMPLE = {
    "description": (
        "Can't withdraw money: in the dashboard, on the Wallet page I tap \"Withdraw\", "
        "enter 1500 to card 2200 1234 5678 9012, and get an error. My account ivan.petrov@mail.com, id 48211."
    ),
    "actual": "Server response 500 {\"detail\":\"payout_failed\",\"trace\":\"a8f3e1c9d2b74e56a1f0c3d9e8b7a6f5\"}",
    "expected": "The withdrawal request is created and shows up in history",
}


async def main() -> None:
    settings = get_settings()
    client = build_llm_client(settings)
    try:
        print("Candidates:", ", ".join(client.candidates))
        base = await client.resolve()
        print("Selected:", base or "— none respond to /health")
        if not base:
            return
        try:
            print("Models:", ", ".join(await client.models()) or "?")
        except LLMUnavailable as exc:
            print("Models: error —", exc)
            if "401" in str(exc):
                print("The server requires a key: set LLM_API_KEY in .env (the same one as --api-key for llm-qwen).")
            return
        fields = {"description": " ".join(sys.argv[1:])} if len(sys.argv) > 1 else SAMPLE
        print("\nInput:", fields["description"])
        started = time.monotonic()
        summary = await LLMSummarizer(client).summarize_bug(fields, settings.bug_summary_chars)
        print(f"\nSummary ({time.monotonic() - started:.1f} s, {len(summary)} chars):\n{summary}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
