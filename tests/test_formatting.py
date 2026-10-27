from datetime import datetime, timezone

from bot.utils.telegram import fmt_dt, post_link, split_html_lines, truncate


def test_post_link():
    assert post_link(-1001234567890, 55, 7) == "https://t.me/c/1234567890/7/55"
    assert post_link(-1001234567890, 55) == "https://t.me/c/1234567890/55"
    assert post_link(12345, 1) is None


def test_truncate():
    assert truncate("коротко", 50) == "коротко"
    out = truncate("слово " * 100, 40)
    assert len(out) <= 40 and out.endswith("…")


def test_split_respects_limit():
    text = "\n".join(["x" * 100] * 100)
    chunks = split_html_lines(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_fmt_dt_moscow():
    dt = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)
    assert fmt_dt(dt, "Europe/Moscow").startswith("27.10.2026 04:00")
