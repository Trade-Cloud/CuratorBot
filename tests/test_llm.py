import pytest

from bot.services.llm import LLMSummarizer, LLMUnavailable, TruncateSummarizer, bug_prompt, clean_completion, redact


def test_clean_completion_strips_think_and_quotes():
    raw = "<think>\nreasoning\n</think>\n\"Summary: The withdrawal request is not created in the Wallet section.\""
    assert clean_completion(raw) == "The withdrawal request is not created in the Wallet section."


def test_clean_completion_markdown():
    assert clean_completion("**Login** fails via `Telegram`") == "Login fails via Telegram"


@pytest.mark.parametrize(
    "text",
    [
        "write to ivan@mail.com",
        "call +7 912 345 67 89",
        "see https://evil.example/x",
        "server 10.0.0.12 is down",
        "token a8f3e1c9d2b74e56a1f0c3d9e8b7a6f5",
        "handle @vasya_pupkin",
    ],
)
def test_redact(text):
    out = redact(text)
    assert "…" in out


def test_redact_keeps_normal_text():
    text = "After clicking Withdraw an error 500 appears"
    assert redact(text) == text


class FakeClient:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error

    async def chat(self, messages, max_tokens=200, temperature=0.2):
        if self.error:
            raise self.error
        return self.answer


async def test_llm_summarizer_uses_model_answer():
    s = LLMSummarizer(FakeClient("The withdrawal request is not created from the wallet."))
    out = await s.summarize_bug({"description": "a long text"}, 220)
    assert out == "The withdrawal request is not created from the wallet."


async def test_llm_summarizer_falls_back():
    s = LLMSummarizer(FakeClient(error=LLMUnavailable("down")))
    out = await s.summarize_bug({"description": "Login fails"}, 220)
    assert out == "Login fails"


async def test_truncate_summarizer_redacts():
    out = await TruncateSummarizer().summarize_bug({"description": "not working, my email a@b.com"}, 220)
    assert "a@b.com" not in out


def test_bug_prompt_contains_all_fields():
    fields = {
        "description": "D", "steps": "1. S", "actual": "A", "expected": "E",
        "environment": "Chrome", "logs": ["L1", "L2"],
    }
    prompt = bug_prompt(fields, 220)
    for value in ("D", "1. S", "A", "E", "Chrome", "L1", "L2", "220"):
        assert value in prompt
