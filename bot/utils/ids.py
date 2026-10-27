import secrets

# Без похожих символов (0/O, 1/I/L), чтобы ID легко было продиктовать и переписать.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ID_LENGTH = 6


def new_public_id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(ID_LENGTH))


def normalize_public_id(raw: str) -> str | None:
    value = raw.strip().lstrip("#").upper()
    if len(value) != ID_LENGTH or any(ch not in ALPHABET for ch in value):
        return None
    return value
