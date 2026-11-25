"""Member actions: the correct Bot API calls."""

import pytest

from bot.services import members


class FakeBot:
    def __init__(self):
        self.calls = []

    async def restrict_chat_member(self, chat_id, user_id, permissions=None, until_date=None):
        self.calls.append(("restrict", chat_id, user_id, until_date is not None))

    async def ban_chat_member(self, chat_id, user_id):
        self.calls.append(("ban", chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=None):
        self.calls.append(("unban", chat_id, user_id))


async def test_mute_with_timer():
    bot = FakeBot()
    assert await members.apply_action(bot, -100, 7, "mute", 60) == "muted"
    assert bot.calls == [("restrict", -100, 7, True)]


async def test_unmute_no_timer():
    bot = FakeBot()
    assert await members.apply_action(bot, -100, 7, "unmute") == "unmuted"
    assert bot.calls == [("restrict", -100, 7, False)]


async def test_kick_is_ban_then_unban():
    bot = FakeBot()
    assert await members.apply_action(bot, -100, 7, "kick") == "kicked"
    assert [c[0] for c in bot.calls] == ["ban", "unban"]


async def test_unknown_action_raises():
    with pytest.raises(ValueError):
        await members.apply_action(FakeBot(), -100, 7, "nope")


def test_destructive_set():
    assert members.DESTRUCTIVE == {"ban", "kick"}
