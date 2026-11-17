"""Field/file encryption helpers.

Keys (set as environment variables online; created automatically as files on a local computer):
  FIELD_ENCRYPTION_KEY  one Fernet key, or several separated by commas (newest first) for key rotation
  ID_HASH_KEY           secret used for the searchable fingerprint of an ID number
LOSING THE KEYS MEANS LOSING THE DATA. Keep a copy somewhere safe, away from the backups.
"""
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

FIELD_PREFIX = "enc1:"
FILE_MAGIC = b"ASENC1\n"
_cache = {}


def _load(env_name, file_name, make):
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    if not getattr(settings, "ALLOW_LOCAL_KEYS", False):
        raise ImproperlyConfigured(f"{env_name} must be set when running online.")
    f = Path(settings.BASE_DIR) / file_name
    if f.exists():
        return f.read_text().strip()
    value = make()
    f.write_text(value)
    return value


def _fernet():
    if "fernet" not in _cache:
        raw = _load("FIELD_ENCRYPTION_KEY", ".field_key", lambda: Fernet.generate_key().decode())
        _cache["fernet"] = MultiFernet([Fernet(k.strip().encode()) for k in raw.split(",") if k.strip()])
    return _cache["fernet"]


def _hash_key():
    if "hash" not in _cache:
        _cache["hash"] = _load("ID_HASH_KEY", ".hash_key", lambda: Fernet.generate_key().decode()).encode()
    return _cache["hash"]


def reset_cache():
    _cache.clear()


def encrypt_text(value: str) -> str:
    if value is None or value == "":
        return value
    return FIELD_PREFIX + _fernet().encrypt(value.encode("utf-8")).decode()


def decrypt_text(value: str) -> str:
    """Tolerates legacy plaintext (before migration) by returning it unchanged."""
    if not value or not value.startswith(FIELD_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(FIELD_PREFIX):].encode()).decode("utf-8")
    except InvalidToken:
        raise ImproperlyConfigured("Cannot decrypt stored data: FIELD_ENCRYPTION_KEY does not match.")


def nid_hash(national_id: str) -> str:
    """Deterministic fingerprint for equality lookups and the unique-per-workspace rule."""
    digits = "".join(ch for ch in (national_id or "") if ch.isdigit())
    if not digits:
        return ""
    return hmac.new(_hash_key(), digits.zfill(9)[-9:].encode(), hashlib.sha256).hexdigest()


def encrypt_bytes(data: bytes) -> bytes:
    return FILE_MAGIC + _fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    if not data.startswith(FILE_MAGIC):
        return data  # legacy, not yet encrypted
    try:
        return _fernet().decrypt(data[len(FILE_MAGIC):])
    except InvalidToken:
        raise ImproperlyConfigured("Cannot decrypt stored file: FIELD_ENCRYPTION_KEY does not match.")
