import os

# Settings aren't read in tests, but set these just in case so importing config doesn't fail.
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("CHAT_ID", "-1001")
