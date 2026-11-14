from laptop_agent.signing import expires_epoch, sign, verify_row

SECRET = "test-secret-0123456789abcdef0123456789"
CMD_ID = "11111111-2222-3333-4444-555555555555"
ARGS = {"src": "a.txt", "dest": "Ünïcode/b.txt"}
# Same vector as server/tests/test_laptop_signing.py: the two sides must agree.
VECTOR = "bbdeaf9a1870dc5d827da1f20079c551b7cac050b9af60d7cd26950dc58f7696"


def row(**over):
    base = {
        "id": CMD_ID,
        "tool": "move_file",
        "args": dict(ARGS),
        "expires_at": "2030-03-17T17:46:40+00:00",  # 1900000000
        "signature": VECTOR,
    }
    base.update(over)
    return base


def test_shared_vector():
    assert sign(SECRET, CMD_ID, "move_file", ARGS, 1900000000) == VECTOR
    assert expires_epoch("2030-03-17T17:46:40+00:00") == 1900000000
    assert expires_epoch("2030-03-17T17:46:40.000000Z") == 1900000000


def test_valid_signature():
    assert verify_row(SECRET, row())


def test_tampered_fields_fail():
    assert not verify_row(SECRET, row(args={"src": "a.txt", "dest": "../Documents/b.txt"}))
    assert not verify_row(SECRET, row(tool="delete_file"))
    assert not verify_row(SECRET, row(id="99999999-2222-3333-4444-555555555555"))
    assert not verify_row(SECRET, row(expires_at="2030-03-17T17:46:41+00:00"))


def test_wrong_secret_fails():
    assert not verify_row("another-secret-0123456789abcdef01234", row())


def test_unsigned_or_malformed_fails():
    assert not verify_row(SECRET, row(signature=None))
    assert not verify_row(SECRET, row(signature=""))
    assert not verify_row(SECRET, row(args="not a dict"))
    assert not verify_row(SECRET, row(expires_at="2030-03-17T17:46:40"))  # no zone
    assert not verify_row(SECRET, {"signature": VECTOR})


def test_short_or_missing_secret_never_verifies():
    short = "short"
    assert not verify_row(short, row(signature=sign(short, CMD_ID, "move_file", ARGS, 1900000000)))
    assert not verify_row("", row())
