import secrets

from ..config import CONFIG

# No look-alike characters (0/O, 1/I/L), so an id is easy to dictate and retype.
ALPHABET = CONFIG["ids"]["alphabet"]
ID_LENGTH = CONFIG["ids"]["length"]


def new_public_id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(ID_LENGTH))


def normalize_public_id(raw: str) -> str | None:
    value = raw.strip().lstrip("#").upper()
    if len(value) != ID_LENGTH or any(ch not in ALPHABET for ch in value):
        return None
    return value
