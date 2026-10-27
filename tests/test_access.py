from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.types import Message

from bot.middlewares.access import PrivateAccessMiddleware

TS = int(datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp())


class FakeTags:
    def __init__(self, members):
        self.members = members

    async def is_member(self, bot, chat_id, user_id):
        return user_id in self.members


class FakeMenu:
    def __init__(self):
        self.synced = {}

    async def sync(self, bot, user_id, is_member):
        self.synced[user_id] = is_member


def _settings():
    return SimpleNamespace(chat_id=-1001, access_bot_username="trade_cloud_bot", is_admin=lambda uid: uid == 1)


def _msg(user_id, chat_type="private"):
    chat = {"id": user_id if chat_type == "private" else -1001, "type": chat_type}
    return Message.model_validate(
        {"message_id": 1, "date": TS, "chat": chat, "from": {"id": user_id, "is_bot": False, "first_name": "x"}, "text": "/start"}
    )


async def _run(mw, event):
    called = []

    async def handler(e, d):
        called.append(e)
        return "ok"

    answered = []

    async def fake_answer(*args, **kwargs):
        answered.append(args[0] if args else kwargs.get("text"))

    object.__setattr__(event, "answer", fake_answer)
    await mw(handler, event, {"bot": SimpleNamespace()})
    return called, answered


async def test_member_passes_and_gets_menu():
    menu = FakeMenu()
    mw = PrivateAccessMiddleware(_settings(), FakeTags({42}), menu)
    called, answered = await _run(mw, _msg(42))
    assert called and not answered and menu.synced[42] is True


async def test_stranger_redirected():
    menu = FakeMenu()
    mw = PrivateAccessMiddleware(_settings(), FakeTags(set()), menu)
    called, answered = await _run(mw, _msg(77))
    assert not called
    assert "@trade_cloud_bot" in answered[0]
    assert menu.synced[77] is False


async def test_admin_passes_without_membership():
    mw = PrivateAccessMiddleware(_settings(), FakeTags(set()), FakeMenu())
    called, _ = await _run(mw, _msg(1))
    assert called


async def test_group_messages_untouched():
    mw = PrivateAccessMiddleware(_settings(), FakeTags(set()), FakeMenu())
    called, _ = await _run(mw, _msg(77, chat_type="supergroup"))
    assert called
