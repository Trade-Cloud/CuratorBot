import pytest

from bot.services.moderation import ModerationInput, ModerationRules, Severity, analyze_rules, max_severity

RULES = ModerationRules(
    allowed_domains={"trade-cloud.org"},
    allowed_mentions={"tradecloud_beta_bot"},
    block_mentions=True,
)


def kinds(text: str, **kw) -> set[str]:
    return {f.kind for f in analyze_rules(ModerationInput(text=text, **kw), RULES)}


@pytest.mark.parametrize(
    "text,kind",
    [
        ("го ко мне t.me/some_channel", "telegram_link"),
        ("t . me / joinchat", "telegram_link"),
        ("https://example.com/promo", "link"),
        ("заходи на example.ru", "link"),
        ("пиши @vasya_pupkin", "mention"),
        ("мой номер +7 (912) 345-67-89", "phone"),
        ("звони 8 912 345 67 89", "phone"),
        ("почта test@gmail.com", "email"),
        ("ivan (at) mail точка ru", "email"),
    ],
)
def test_hard_findings(text, kind):
    findings = analyze_rules(ModerationInput(text=text), RULES)
    assert kind in {f.kind for f in findings}
    assert max_severity(findings) == Severity.HARD


@pytest.mark.parametrize("text", ["пиши в лс", "есть в дискорде?", "мой тг скину"])
def test_soft_findings(text):
    findings = analyze_rules(ModerationInput(text=text), RULES)
    assert findings
    assert max_severity(findings) == Severity.SOFT


@pytest.mark.parametrize(
    "text",
    [
        "спасибо, всё работает",
        "доки на trade-cloud.org/docs",
        "в версии 1.2.3 баг. Me too",
        "заказ 1234567890 завис",
        "@tradecloud_beta_bot помоги",
    ],
)
def test_clean(text):
    assert analyze_rules(ModerationInput(text=text), RULES) == []


def test_entities_are_used():
    text = "ссылка тут"
    entities = [{"type": "text_link", "offset": 0, "length": 6, "url": "https://evil.example/x"}]
    assert "link" in kinds(text, entities=entities)


def test_allowed_text_link():
    entities = [{"type": "text_link", "offset": 0, "length": 4, "url": "https://app.trade-cloud.org"}]
    assert kinds("доки", entities=entities) == set()


def test_mentions_can_be_allowed():
    rules = ModerationRules(block_mentions=False)
    assert analyze_rules(ModerationInput(text="@vasya_pupkin глянь"), rules) == []


def test_contact_card_and_channel_forward():
    assert "contact_card" in kinds("", is_contact=True)
    assert "forward_channel" in kinds("", forward_type="channel")
