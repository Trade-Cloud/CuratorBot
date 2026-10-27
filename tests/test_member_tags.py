from types import SimpleNamespace

import pytest

from bot.services.member_tags import MemberInfo, MemberTagResolver, tag_matches

EXEMPT = {"admin", "moder"}


def test_tag_matches_case_insensitive():
    assert tag_matches("ADMIN", EXEMPT)
    assert tag_matches(" Moder ", EXEMPT)
    assert not tag_matches("VIP", EXEMPT)
    assert not tag_matches(None, EXEMPT)


def _settings(**kw):
    base = dict(exempt_tags=EXEMPT, exempt_chat_admins=True, is_admin=lambda uid: False)
    base.update(kw)
    return SimpleNamespace(**base)


def _message(sender_tag=None, user_id=10, sender_chat=None):
    return SimpleNamespace(
        sender_chat=sender_chat,
        is_automatic_forward=False,
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        sender_tag=sender_tag,
        chat=SimpleNamespace(id=-100),
        bot=SimpleNamespace(id=999),
    )


class FakeResolver(MemberTagResolver):
    def __init__(self, info):
        super().__init__()
        self.info = info

    async def get(self, bot, chat_id, user_id):
        return self.info


@pytest.mark.asyncio
async def test_sender_tag_exempts():
    resolver = FakeResolver(None)
    assert await resolver.is_exempt(_message(sender_tag="MODER"), _settings())


@pytest.mark.asyncio
async def test_member_tag_from_api_exempts():
    resolver = FakeResolver(MemberInfo(status="member", tag="ADMIN", custom_title=None))
    assert await resolver.is_exempt(_message(), _settings())


@pytest.mark.asyncio
async def test_regular_member_not_exempt():
    resolver = FakeResolver(MemberInfo(status="member", tag="VIP", custom_title=None))
    assert not await resolver.is_exempt(_message(), _settings())


@pytest.mark.asyncio
async def test_chat_admin_exempt_by_setting():
    resolver = FakeResolver(MemberInfo(status="administrator", tag=None, custom_title=None))
    assert await resolver.is_exempt(_message(), _settings())
    assert not await resolver.is_exempt(_message(), _settings(exempt_chat_admins=False))


@pytest.mark.asyncio
async def test_anonymous_admin_exempt():
    resolver = FakeResolver(None)
    msg = _message(sender_chat=SimpleNamespace(id=-100))
    assert await resolver.is_exempt(msg, _settings())
