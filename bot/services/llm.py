"""Local LLM (llama-server, OpenAI-compatible API).

Currently used for the short bug summary in the public Bug Reports post.
Possible future uses: a moderation Analyzer (services/moderation.py), duplicate detection.

Server address:
- LLM_BASE_URL set — use only that;
- otherwise try the candidates and take the first live one (via /health):
  in Docker — host.docker.internal:8301 (the server where the bot sits next to llm-qwen), then LAN;
  on Mac — straight to the server's LAN address (LLM_SERVER_HOST, default 192.168.1.145);
  on Linux outside Docker — 127.0.0.1:8301, then LAN.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from typing import Any, Protocol

import aiohttp

from .. import texts
from ..utils.telegram import truncate

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- prompt

BUG_SUMMARY_SYSTEM = texts.BUG_SUMMARY_SYSTEM
BUG_SUMMARY_USER = texts.BUG_SUMMARY_USER


def bug_prompt(fields: dict[str, Any], limit: int) -> str:
    logs = [str(x) for x in fields.get("logs") or []]
    logs_text = truncate("\n---\n".join(logs), 2000) if logs else "—"
    return BUG_SUMMARY_USER.format(
        description=truncate(str(fields.get("description") or "—"), 1000),
        steps=truncate(str(fields.get("steps") or "—"), 1500),
        actual=truncate(str(fields.get("actual") or "—"), 800),
        expected=truncate(str(fields.get("expected") or "—"), 800),
        environment=truncate(str(fields.get("environment") or "—"), 200),
        logs=logs_text,
        limit=limit,
    )


# ---------------------------------------------------------------- post-processing

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_REDACT = (
    re.compile(r"https?://\S+|www\.\S+|(?:t|telegram)\.me/\S+", re.IGNORECASE),
    re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+"),
    re.compile(r"(?<!\d)\+?\d[\d\s\-()]{8,}\d(?!\d)"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # long tokens/hashes/keys
    re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}"),
)


def redact(text: str) -> str:
    """A safety net on top of the prompt: strip anything resembling contacts, links and secrets."""
    for pattern in _REDACT:
        text = pattern.sub("…", text)
    return re.sub(r"(?:…\s*){2,}", "… ", text)


def clean_completion(text: str) -> str:
    text = _THINK.sub("", text)
    text = text.split("</think>")[-1]  # in case the opening tag was cut off
    text = text.strip().strip("\"'«»“”").strip()
    text = re.sub(r"^(?:summary|answer|annotation)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[*_`#]+", "", text)
    return " ".join(text.split())


# ---------------------------------------------------------------- client


def default_candidates(server_host: str, port: int) -> list[str]:
    lan = f"http://{server_host}:{port}"
    if os.path.exists("/.dockerenv"):
        return [f"http://host.docker.internal:{port}", lan]
    if sys.platform == "darwin":
        return [lan]
    return [f"http://127.0.0.1:{port}", lan]


class LLMClient:
    def __init__(
        self,
        candidates: list[str],
        model: str | None = None,
        timeout: float = 20.0,
        api_key: str | None = None,
    ):
        self.candidates = [c.rstrip("/").removesuffix("/v1") for c in candidates]
        self.model = model
        self.timeout = timeout
        self.api_key = api_key
        self._base: str | None = None
        self._resolved_at = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str | None:
        return self._base

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _alive(self, base: str) -> bool:
        try:
            async with self._http().get(f"{base}/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def resolve(self, force: bool = False) -> str | None:
        """First live address among the candidates. A failure is cached for a minute so every report isn't slowed."""
        async with self._lock:
            if not force and (self._base or time.monotonic() - self._resolved_at < 60):
                return self._base
            self._resolved_at = time.monotonic()
            self._base = None
            for base in self.candidates:
                if await self._alive(base):
                    self._base = base
                    log.info(texts.LOG["llm"]["using"], base)
                    break
            else:
                log.warning(texts.LOG["llm"]["unreachable"], self.candidates)
            return self._base

    async def models(self) -> list[str]:
        base = await self.resolve()
        if not base:
            return []
        async with self._http().get(f"{base}/v1/models", timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                raise LLMUnavailable(f"HTTP {resp.status}: {(await resp.text())[:300]}")
            data = await resp.json()
        return [m.get("id", "?") for m in data.get("data", [])]

    async def chat(self, messages: list[dict[str, str]], max_tokens: int = 200, temperature: float = 0.2) -> str:
        base = await self.resolve()
        if not base:
            raise LLMUnavailable("no reachable LLM server")
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "stream": False,
            # Qwen3: no thinking — we want a short answer, fast. Other models ignore this field.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self.model:
            payload["model"] = self.model
        try:
            async with self._http().post(
                f"{base}/v1/chat/completions", json=payload, timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as resp:
                if resp.status != 200:
                    raise LLMUnavailable(f"HTTP {resp.status}: {(await resp.text())[:300]}")
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._base = None  # re-resolve a live address next time
            raise LLMUnavailable(str(exc) or exc.__class__.__name__) from exc
        return data["choices"][0]["message"].get("content") or ""


class LLMUnavailable(Exception):
    pass


# ---------------------------------------------------------------- summarizer


class Summarizer(Protocol):
    async def summarize_bug(self, fields: dict[str, Any], limit: int) -> str: ...


class TruncateSummarizer:
    """No LLM: a neatly truncated description."""

    async def summarize_bug(self, fields: dict[str, Any], limit: int) -> str:
        return truncate(redact(str(fields.get("description", ""))), limit)


class LLMSummarizer:
    def __init__(self, client: LLMClient, fallback: Summarizer | None = None):
        self.client = client
        self.fallback = fallback or TruncateSummarizer()

    async def summarize_bug(self, fields: dict[str, Any], limit: int) -> str:
        prompt = bug_prompt(fields, limit)
        try:
            raw = await self.client.chat(
                [{"role": "system", "content": BUG_SUMMARY_SYSTEM}, {"role": "user", "content": prompt}],
                max_tokens=max(64, limit // 2),
            )
        except (LLMUnavailable, KeyError, ValueError) as exc:
            log.warning(texts.LOG["llm"]["summary_failed"], exc)
            return await self.fallback.summarize_bug(fields, limit)
        text = truncate(redact(clean_completion(raw)), limit)
        if len(text) < 10:
            return await self.fallback.summarize_bug(fields, limit)
        return text


def build_llm_client(settings) -> LLMClient:
    candidates = (
        [settings.llm_base_url]
        if settings.llm_base_url
        else default_candidates(settings.llm_server_host, settings.llm_port)
    )
    return LLMClient(
        candidates,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
        api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
    )


def build_summarizer(settings, client: LLMClient | None) -> Summarizer:
    if settings.llm_enabled and client is not None:
        return LLMSummarizer(client)
    return TruncateSummarizer()
