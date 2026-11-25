"""Form step keyboards: quick buttons + "Skip"."""

from bot import keyboards


def _texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_step_kb_with_quick_and_skip():
    markup = keyboards.step_kb([{"label": "🌐 Browser", "value": "Browser"}], "environment", skip=True)
    assert _texts(markup) == ["🌐 Browser", keyboards.texts.BTN_SKIP]
    assert _datas(markup) == ["quick:environment:0", "flow:skip"]


def test_step_kb_only_skip():
    markup = keyboards.step_kb([], "steps", skip=True)
    assert _datas(markup) == ["flow:skip"]


def test_step_kb_empty_is_none():
    assert keyboards.step_kb([], "steps", skip=False) is None


def test_done_kb_skip_variant():
    assert "flow:skip" in _datas(keyboards.done_kb(skip=True))
    assert "flow:skip" not in _datas(keyboards.done_kb(skip=False))
    assert "flow:done" in _datas(keyboards.done_kb(skip=False))
