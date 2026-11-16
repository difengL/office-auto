"""密钥形态兼容性回归。

真实 data/secret.key 是旧版留下的 32 字节原始随机字节，不是合法 Fernet key。
若直接当 key 用会 ValueError；若另生成一把则旧凭据永久解不开。
"""
import base64
import hashlib
import os

import pytest
from cryptography.fernet import Fernet

from app.core import db


@pytest.fixture
def keydir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    return tmp_path


def test_generates_valid_key_when_absent(keydir):
    assert not db.KEY_PATH.exists()
    token = db.encrypt_dict({"password": "p"})
    assert db.decrypt_dict(token)["password"] == "p"
    assert db.KEY_PATH.exists()
    Fernet(db.KEY_PATH.read_bytes().strip())      # 必须是合法 key
    assert os.stat(db.KEY_PATH).st_mode & 0o777 == 0o600


def test_accepts_existing_valid_fernet_key(keydir):
    key = Fernet.generate_key()
    db.KEY_PATH.write_bytes(key)
    assert db._load_key() == key
    assert db.decrypt_dict(db.encrypt_dict({"a": 1})) == {"a": 1}


def test_legacy_raw_32_bytes_still_decrypts(keydir):
    raw = os.urandom(32)                          # 旧版写入的原始随机字节
    db.KEY_PATH.write_bytes(raw)
    legacy_key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    legacy_token = Fernet(legacy_key).encrypt(b'{"password": "old"}').decode()
    # 新代码必须能解开旧密文
    assert db.decrypt_dict(legacy_token)["password"] == "old"
    # 且新写入的也能读回
    assert db.decrypt_dict(db.encrypt_dict({"password": "new"}))["password"] == "new"


def test_credential_roundtrip_with_legacy_key(keydir):
    db.KEY_PATH.write_bytes(os.urandom(32))
    u = db.create_user("legacy", "HASH", "L")
    db.save_credential(u.id, "meican", {"accessToken": "AT"}, "active")
    assert db.load_credential(u.id, "meican").payload["accessToken"] == "AT"