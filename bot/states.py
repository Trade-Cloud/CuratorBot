from aiogram.fsm.state import State, StatesGroup


class BugForm(StatesGroup):
    description = State()
    steps = State()
    actual = State()
    expected = State()
    environment = State()
    attachments = State()
    confirm = State()


class FeatureForm(StatesGroup):
    idea = State()
    problem = State()
    implementation = State()
    confirm = State()


class ChangelogForm(StatesGroup):
    version = State()
    text = State()
    confirm = State()
