"""Detecting contact sharing and solicitation in Main.

The detector is a set of analyzers with a shared interface. For now it is only rules (regex + Telegram
entities); later an analyzer backed by a local LLM (see services/llm.py) will sit here too, for the soft
cases that regexes miss ("message me on a well-known social network").

NOTE: the regex patterns below intentionally contain Russian words. They are detection DATA, not prose —
they match the Russian text people actually post in the chat. Do not translate or remove them without
dropping the ability to catch Russian contact sharing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any, Protocol
from urllib.parse import urlparse

from aiogram.types import Message


class Severity(IntEnum):
    SOFT = 1  # suspicious: logged to admins only, by default
    HARD = 2  # explicit contact/link: deleted


@dataclass(slots=True)
class Finding:
    kind: str
    severity: Severity
    match: str

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.name.lower()
        return data


@dataclass(slots=True)
class ModerationInput:
    """Everything the analyzers need. Built from a Message but independent of aiogram — easy to test."""

    text: str = ""
    entities: list[dict[str, Any]] = field(default_factory=list)
    is_contact: bool = False
    forward_type: str | None = None  # user | hidden_user | chat | channel
    via_bot: bool = False
    has_external_reply: bool = False


@dataclass(slots=True)
class ModerationRules:
    allowed_domains: set[str] = field(default_factory=set)
    allowed_mentions: set[str] = field(default_factory=set)
    block_mentions: bool = True


class Analyzer(Protocol):
    async def analyze(self, data: ModerationInput, rules: ModerationRules) -> list[Finding]: ...


# ---------------------------------------------------------------- rules

_TG_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/[\w+\-/]+|tg://\S+",
    re.IGNORECASE,
)
# Common obfuscations: "t . me / name", "т.ме/name", "t[.]me".
_TG_LINK_OBFUSCATED = re.compile(
    r"(?<![\w])[tт]\s*(?:\.|\[\.\]|\(\.\)|dot|точка)\s*(?:me|ме|mе|мe)\s*/\s*\w+",
    re.IGNORECASE,
)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_TLDS = (
    "com|ru|org|net|io|gg|me|xyz|shop|store|su|top|info|site|online|pro|link|ly|cc|app|dev|"
    "biz|club|live|fun|space|website|tk|ml|ga|cf|by|kz|ua|uz|рф"
)
_BARE_DOMAIN = re.compile(
    rf"(?<![@\w.])((?:[a-zа-я0-9](?:[a-zа-я0-9\-]{{0,61}}[a-zа-я0-9])?\.)+(?:{_TLDS}))(?![\w])(?:/\S*)?",
    re.IGNORECASE,
)
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})(?![\w])")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
_EMAIL_OBFUSCATED = re.compile(
    r"[\w.+\-]+\s*(?:\(at\)|\[at\]|\{at\}|собака)\s*[\w\-]+\s*(?:\.|\(dot\)|\[dot\]|точка)\s*\w{2,}",
    re.IGNORECASE,
)
_PHONE = re.compile(
    r"(?<![\d\w])(?:\+?7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"
    r"|(?<![\d\w])\+\d{1,3}[\s\-(]*\d{2,4}[\s\-)]*\d{2,4}[\s\-]*\d{2,4}[\s\-]*\d{0,4}(?!\d)"
)
_SOFT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "messenger",
        re.compile(
            # Long names matched by stem (дискорд/дискорде/дискордик); short ones only as whole words.
            r"\b(?:whats?app|ватс?ап|вотс?ап|вацап|viber|вайбер|discord|дискорд|skype|скайп|"
            r"wechat|вичат|instagram|инстаграм)\w*|\b(?:vk|вк|инст[ау]|signal)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "dm_invite",
        re.compile(
            r"\b(?:пиш(?:и|ите)|стучи(?:те)?|напиш(?:и|ите)|го|пиши)\s+(?:мне\s+)?(?:в\s+)?"
            r"(?:лс|личку|личные|личн\w*|тг|телег\w*|dm|pm)\b"
            r"|\b(?:dm|pm)\s+me\b|\bв\s+лс\b|\bв\s+личк[уе]\b",
            re.IGNORECASE,
        ),
    ),
    (
        "contact_handle",
        re.compile(r"\b(?:мой|мои|my)\s+(?:тг|телеграм\w*|ник|контакт\w*|tg|telegram|номер)\b", re.IGNORECASE),
    ),
)

_HARD_ENTITY_KINDS = {"phone_number": "phone", "email": "email"}


def _domain_allowed(url: str, allowed: set[str]) -> bool:
    if not allowed:
        return False
    raw = url if "://" in url else f"http://{url}"
    host = (urlparse(raw).hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowed)


def _entity_text(text: str, entity: dict[str, Any]) -> str:
    # offset/length are in UTF-16 code units
    encoded = text.encode("utf-16-le")
    start, length = entity.get("offset", 0) * 2, entity.get("length", 0) * 2
    return encoded[start : start + length].decode("utf-16-le", errors="ignore")


class RuleAnalyzer:
    async def analyze(self, data: ModerationInput, rules: ModerationRules) -> list[Finding]:
        return analyze_rules(data, rules)


def analyze_rules(data: ModerationInput, rules: ModerationRules) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, severity: Severity, match: str) -> None:
        key = (kind, match.lower())
        if key not in seen:
            seen.add(key)
            findings.append(Finding(kind, severity, match))

    text = data.text or ""

    if data.is_contact:
        add("contact_card", Severity.HARD, "contact")
    if data.forward_type == "channel":
        add("forward_channel", Severity.HARD, "forward")
    elif data.forward_type:
        add("forward", Severity.SOFT, data.forward_type)
    if data.via_bot:
        add("via_bot", Severity.SOFT, "via_bot")

    # Telegram already marked up links/phones/emails — the most reliable signal.
    for ent in data.entities:
        kind = ent.get("type")
        if kind == "text_link":
            url = ent.get("url", "")
            if not _domain_allowed(url, rules.allowed_domains):
                add("telegram_link" if _TG_LINK.search(url) else "link", Severity.HARD, url)
        elif kind == "url":
            url = _entity_text(text, ent)
            if not _domain_allowed(url, rules.allowed_domains):
                add("telegram_link" if _TG_LINK.search(url) else "link", Severity.HARD, url)
        elif kind == "mention" and rules.block_mentions:
            handle = _entity_text(text, ent).lstrip("@")
            if handle.lower() not in rules.allowed_mentions:
                add("mention", Severity.HARD, "@" + handle)
        elif kind in _HARD_ENTITY_KINDS:
            add(_HARD_ENTITY_KINDS[kind], Severity.HARD, _entity_text(text, ent))

    for m in _TG_LINK.finditer(text):
        add("telegram_link", Severity.HARD, m.group(0))
    for m in _TG_LINK_OBFUSCATED.finditer(text):
        add("telegram_link", Severity.HARD, m.group(0))
    for m in _URL.finditer(text):
        if not _domain_allowed(m.group(0), rules.allowed_domains):
            add("link", Severity.HARD, m.group(0))
    text_without_emails = _EMAIL.sub(" ", text)
    # Look for schemeless domains in text where full links are already stripped, to avoid double counting.
    bare_text = _URL.sub(" ", _TG_LINK.sub(" ", text_without_emails))
    for m in _BARE_DOMAIN.finditer(bare_text):
        if not _domain_allowed(m.group(0), rules.allowed_domains):
            add("link", Severity.HARD, m.group(0))
    for m in _EMAIL.finditer(text):
        add("email", Severity.HARD, m.group(0))
    for m in _EMAIL_OBFUSCATED.finditer(text):
        add("email", Severity.HARD, m.group(0))
    for m in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 15:
            add("phone", Severity.HARD, m.group(0).strip())
    if rules.block_mentions:
        for m in _MENTION.finditer(text_without_emails):
            if m.group(1).lower() not in rules.allowed_mentions:
                add("mention", Severity.HARD, m.group(0))

    for kind, pattern in _SOFT_PATTERNS:
        for m in pattern.finditer(text):
            add(kind, Severity.SOFT, m.group(0))

    return findings


def build_input(message: Message) -> ModerationInput:
    text = message.text if message.text is not None else (message.caption or "")
    entities = message.entities if message.text is not None else message.caption_entities
    origin = message.forward_origin
    return ModerationInput(
        text=text,
        entities=[e.model_dump(mode="json", exclude_none=True) for e in (entities or [])],
        is_contact=message.contact is not None,
        forward_type=getattr(origin, "type", None) if origin else None,
        via_bot=message.via_bot is not None,
        has_external_reply=message.external_reply is not None,
    )


async def run_analyzers(analyzers: list[Analyzer], data: ModerationInput, rules: ModerationRules) -> list[Finding]:
    findings: list[Finding] = []
    for analyzer in analyzers:
        findings.extend(await analyzer.analyze(data, rules))
    return findings


def max_severity(findings: list[Finding]) -> Severity | None:
    return max((f.severity for f in findings), default=None)
