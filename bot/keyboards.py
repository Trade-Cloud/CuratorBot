from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import texts


class DetailsCb(CallbackData, prefix="det"):
    rid: str


class FlowCb(CallbackData, prefix="flow"):
    """Кнопки внутри анкет в ЛС. action: submit | restart | cancel | done | env_default | no_version"""

    action: str


def _kb(*buttons: tuple[str, str], width: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, action in buttons:
        kb.button(text=text, callback_data=FlowCb(action=action))
    kb.adjust(width)
    return kb.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    return _kb((texts.BTN_SUBMIT, "submit"), (texts.BTN_RESTART, "restart"), (texts.BTN_CANCEL, "cancel"))


def env_kb() -> InlineKeyboardMarkup:
    return _kb((f"🌐 {texts.ENV_DEFAULT} (по умолчанию)", "env_default"))


def done_kb() -> InlineKeyboardMarkup:
    return _kb((f"✅ {texts.BTN_DONE}", "done"))


def no_version_kb() -> InlineKeyboardMarkup:
    return _kb((texts.BTN_NO_VERSION, "no_version"))


def url_kb(text: str, url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, url=url)
    return kb.as_markup()
