"""Password hashing for dashboard administration.

Versioned scrypt records; never persist or log the plaintext password.
"""
import hashlib
import hmac
import os
import re

_FORMAT = re.compile(r"^scrypt\$16384\$8\$1\$([0-9a-f]{32})\$([0-9a-f]{128})$")


def validate_password(password):
    if type(password) is not str or not 12 <= len(password) <= 256:
        raise ValueError("Invalid management password")
    if len(password.encode("utf-8")) > 1024 or any(ord(ch) < 32 or ord(ch) == 127 for ch in password):
        raise ValueError("Invalid management password")
    return password


def hash_password(password):
    raw = validate_password(password).encode("utf-8")
    salt = os.urandom(16)
    digest = hashlib.scrypt(raw, salt=salt, n=16384, r=8, p=1, dklen=64)
    return "scrypt$16384$8$1$" + salt.hex() + "$" + digest.hex()


def verify_password(password, record):
    if type(password) is not str or type(record) is not str:
        return False
    match = _FORMAT.fullmatch(record)
    if match is None:
        return False
    try:
        raw = validate_password(password).encode("utf-8")
        digest = hashlib.scrypt(raw, salt=bytes.fromhex(match[1]), n=16384, r=8, p=1, dklen=64)
        return hmac.compare_digest(digest, bytes.fromhex(match[2]))
    except (ValueError, MemoryError):
        return False
