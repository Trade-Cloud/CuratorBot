import pytest

from bot.services.llm import LLMSummarizer, LLMUnavailable, TruncateSummarizer, bug_prompt, clean_completion, redact


def test_clean_completion_strips_think_and_quotes():
    raw = "<think>\nрассуждаю\n</think>\n«Аннотация: Не создаётся заявка на вывод в разделе «Кошелёк».»"
    assert clean_completion(raw) == "Не создаётся заявка на вывод в разделе «Кошелёк»."


def test_clean_completion_markdown():
    assert clean_completion("**Падает** вход через `Telegram`") == "Падает вход через Telegram"


@pytest.mark.parametrize(
    "text",
    [
        "пишите на ivan@mail.ru",
        "звоните +7 912 345 67 89",
        "смотри https://evil.example/x",
        "сервер 10.0.0.12 не отвечает",
        "токен a8f3e1c9d2b74e56a1f0c3d9e8b7a6f5",
        "ник @vasya_pupkin",
    ],
)
def test_redact(text):
    out = redact(text)
    assert "…" in out


def test_redact_keeps_normal_text():
    text = "После нажатия «Вывести» появляется ошибка 500"
    assert redact(text) == text


class FakeClient:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error

    async def chat(self, messages, max_tokens=200, temperature=0.2):
        if self.error:
            raise self.error
        return self.answer


async def test_llm_summarizer_uses_model_answer():
    s = LLMSummarizer(FakeClient("Не создаётся заявка на вывод средств из кошелька."))
    out = await s.summarize_bug({"description": "длинный текст"}, 220)
    assert out == "Не создаётся заявка на вывод средств из кошелька."


async def test_llm_summarizer_falls_back():
    s = LLMSummarizer(FakeClient(error=LLMUnavailable("down")))
    out = await s.summarize_bug({"description": "Падает вход"}, 220)
    assert out == "Падает вход"


async def test_truncate_summarizer_redacts():
    out = await TruncateSummarizer().summarize_bug({"description": "не работает, мой email a@b.ru"}, 220)
    assert "a@b.ru" not in out


def test_bug_prompt_contains_all_fields():
    fields = {
        "description": "D", "steps": "1. S", "actual": "A", "expected": "E",
        "environment": "Chrome", "logs": ["L1", "L2"],
    }
    prompt = bug_prompt(fields, 220)
    for value in ("D", "1. S", "A", "E", "Chrome", "L1", "L2", "220"):
        assert value in prompt
