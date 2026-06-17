"""Key derivation and graceful degradation when SQLCipher is absent."""

from __future__ import annotations

import pytest

from nexus_memory.core import security


def test_derive_key_length_is_32_bytes():
    key = security.derive_key("correct horse battery staple", b"0123456789abcdef")
    assert isinstance(key, bytes)
    assert len(key) == 32


def test_derive_key_deterministic_for_same_salt():
    salt = b"0123456789abcdef"
    k1 = security.derive_key("passphrase", salt)
    k2 = security.derive_key("passphrase", salt)
    assert k1 == k2


def test_derive_key_differs_with_salt():
    k1 = security.derive_key("passphrase", b"saltsaltsaltsalt")
    k2 = security.derive_key("passphrase", b"pepperpepperpepp")
    assert k1 != k2


def test_derive_key_differs_with_passphrase():
    salt = b"0123456789abcdef"
    assert security.derive_key("a", salt) != security.derive_key("b", salt)


def test_is_encryption_available_returns_bool():
    assert isinstance(security.is_encryption_available(), bool)


def test_connect_encrypted_degrades_gracefully():
    """Without a SQLCipher driver, a clear catchable error is raised."""
    key = security.derive_key("pw", b"0123456789abcdef")
    if security.is_encryption_available():
        pytest.skip("SQLCipher driver is installed; degradation path not exercised")
    with pytest.raises(RuntimeError) as exc:
        security.connect_encrypted("ignored.db", key)
    # Message must guide the user toward a fix.
    assert "sqlcipher" in str(exc.value).lower()


def test_connect_encrypted_rejects_wrong_key_length():
    with pytest.raises(ValueError):
        security.connect_encrypted("ignored.db", b"too-short")
