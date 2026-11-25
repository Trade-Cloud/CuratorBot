from bot.utils.ids import ALPHABET, ID_LENGTH, new_public_id, normalize_public_id


def test_new_id_shape():
    for _ in range(200):
        rid = new_public_id()
        assert len(rid) == ID_LENGTH
        assert set(rid) <= set(ALPHABET)


def test_normalize():
    assert normalize_public_id("#abc234") == "ABC234"
    assert normalize_public_id(" ABC234 ") == "ABC234"
    assert normalize_public_id("ABC23") is None
    assert normalize_public_id("ABC230") is None  # 0 is not in the alphabet
