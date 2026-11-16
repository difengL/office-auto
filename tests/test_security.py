from app.core import security


def test_hash_is_not_plaintext():
    h = security.hash_password("secret123")
    assert h != "secret123"
    assert h.startswith("pbkdf2_sha256$")


def test_verify_accepts_correct_password():
    h = security.hash_password("secret123")
    assert security.verify_password("secret123", h) is True


def test_verify_rejects_wrong_password():
    h = security.hash_password("secret123")
    assert security.verify_password("wrong", h) is False


def test_same_password_gets_different_hashes():
    a = security.hash_password("secret123")
    b = security.hash_password("secret123")
    assert a != b          # salt 不同
    assert security.verify_password("secret123", a)
    assert security.verify_password("secret123", b)


def test_verify_handles_malformed_hash():
    assert security.verify_password("x", "garbage") is False
    assert security.verify_password("x", "") is False