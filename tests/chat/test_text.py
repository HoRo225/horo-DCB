from horo_dcb.chat import context_parts

CONTINUED = "（續）\n"


def test_short_context_is_one_part():
    assert context_parts("暗號是紫色長頸鹿") == ["暗號是紫色長頸鹿"]


def test_long_context_parts_fit_the_byte_limit_and_rejoin():
    text = "藍天" * 2000 + "ascii tail"
    parts = context_parts(text)
    assert len(parts) > 1
    assert all(len(part.encode()) <= 3800 for part in parts)
    assert all(part.startswith(CONTINUED) for part in parts[1:])
    assert parts[0] + "".join(part.removeprefix(CONTINUED) for part in parts[1:]) == text
