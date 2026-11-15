import pytest

from jarvis.laptop_signing import sign

SECRET = "test-secret-0123456789abcdef0123456789"
CMD_ID = "11111111-2222-3333-4444-555555555555"
ARGS = {"src": "a.txt", "dest": "Ünïcode/b.txt"}
# Same vector as laptop_agent/tests/test_signing.py: the two sides must agree.
VECTOR = "bbdeaf9a1870dc5d827da1f20079c551b7cac050b9af60d7cd26950dc58f7696"


def test_shared_vector():
    assert sign(SECRET, CMD_ID, "move_file", ARGS, 1900000000) == VECTOR


def test_arg_order_does_not_matter_but_values_do():
    reordered = {"dest": "Ünïcode/b.txt", "src": "a.txt"}
    assert sign(SECRET, CMD_ID, "move_file", reordered, 1900000000) == VECTOR
    assert sign(SECRET, CMD_ID, "move_file", {**ARGS, "dest": "b.txt"}, 1900000000) != VECTOR


def test_short_secret_refused():
    with pytest.raises(ValueError):
        sign("short", CMD_ID, "move_file", ARGS, 1900000000)
