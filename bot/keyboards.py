from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import texts


class DetailsCb(CallbackData, prefix="det"):
    rid: str


class EditCb(CallbackData, prefix="edit"):
    """Author editing their own report. action: menu | field | att | addatt | delatt | done"""

    rid: str
    action: str
    arg: str = ""


class FlowCb(CallbackData, prefix="flow"):
    """Buttons inside private-chat forms. action: submit | restart | cancel | done | skip | no_version"""

    action: str


class QuickCb(CallbackData, prefix="quick"):
    """Form step quick-reply button: fill in a preset answer and move on."""

    step: str
    idx: int


def _kb(*buttons: tuple[str, str], width: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, action in buttons:
        kb.button(text=text, callback_data=FlowCb(action=action))
    kb.adjust(width)
    return kb.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    return _kb((texts.BTN_SUBMIT, "submit"), (texts.BTN_RESTART, "restart"), (texts.BTN_CANCEL, "cancel"))


def step_kb(buttons: list[dict[str, str]], step: str, *, skip: bool = True) -> InlineKeyboardMarkup | None:
    """Form step keyboard: quick-reply buttons plus "Skip". None if there is neither."""
    kb = InlineKeyboardBuilder()
    for i, b in enumerate(buttons):
        kb.button(text=b.get("label") or b.get("value") or "—", callback_data=QuickCb(step=step, idx=i))
    if skip:
        kb.button(text=texts.BTN_SKIP, callback_data=FlowCb(action="skip"))
    if not buttons and not skip:
        return None
    kb.adjust(1)
    return kb.as_markup()


def done_kb(skip: bool = False) -> InlineKeyboardMarkup:
    """"Done" button for the attachments step; adds "Skip" when attachments are optional."""
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ {texts.BTN_DONE}", callback_data=FlowCb(action="done"))
    if skip:
        kb.button(text=texts.BTN_SKIP, callback_data=FlowCb(action="skip"))
    kb.adjust(1)
    return kb.as_markup()


def no_version_kb() -> InlineKeyboardMarkup:
    return _kb((texts.BTN_NO_VERSION, "no_version"))


def url_kb(text: str, url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, url=url)
    return kb.as_markup()
