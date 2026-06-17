"""Optional database-encryption hook and key-derivation helpers.

Encryption is deliberately kept **off the critical path** (see MS6.3 and
``07-Local-Single-User-Optimization.md`` Section 7.2). The unencrypted core
ships first; encryption is an opt-in layer that requires a SQLCipher-enabled
SQLite driver which is fragile to build on Windows.

This module provides:
    * :func:`derive_key`            -- PBKDF2-HMAC-SHA256 -> 32-byte raw key.
    * :func:`is_encryption_available` -- whether a SQLCipher driver is importable.
    * :func:`connect_encrypted`     -- open an encrypted connection, or raise a
      clear, catchable error with guidance if SQLCipher is absent.

Security notes:
    * The raw passphrase is **never** interpolated into a ``PRAGMA`` statement.
      ``PRAGMA`` does not accept SQL bind parameters, so we derive a 32-byte key
      and pass it as a hex literal ``x'<64-hex-chars>'`` -- a string the user
      cannot influence and which contains no quotable characters.
    * Keys should come from the OS keystore (e.g. ``keyring``), never the source.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging

logger = logging.getLogger(__name__)

# PBKDF2 parameters. 200k iterations is a reasonable 2020s baseline for
# SHA-256; the cost is paid once at connection time, off the hot path.
_PBKDF2_ITERATIONS = 200_000
_KEY_LENGTH = 32  # bytes -> 256-bit key, as required by SQLCipher.

# Candidate SQLCipher-enabled DBAPI modules, most-preferred first. The
# maintained `sqlcipher3-binary` (coleifer) wheel exposes `sqlcipher3`.
_SQLCIPHER_MODULES = ("sqlcipher3", "pysqlcipher3")


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a deterministic 32-byte key from a passphrase via PBKDF2.

    Uses PBKDF2-HMAC-SHA256. The output is deterministic for a given
    ``(passphrase, salt)`` pair, so the same inputs always unlock the same
    database.

    Args:
        passphrase: The human-memorable secret. Encoded as UTF-8.
        salt: A non-secret random value (store it alongside the DB). Should be
            at least 16 bytes for real use; not enforced here.

    Returns:
        A 32-byte raw key suitable for the SQLCipher hex-literal PRAGMA.
    """
    if not isinstance(passphrase, str):
        raise TypeError("passphrase must be a str")
    if not isinstance(salt, (bytes, bytearray)):
        raise TypeError("salt must be bytes")

    key = hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        bytes(salt),
        _PBKDF2_ITERATIONS,
        dklen=_KEY_LENGTH,
    )
    return key


def is_encryption_available() -> bool:
    """Return ``True`` if a SQLCipher-enabled DBAPI driver can be imported.

    This only checks importability; it does not verify that the build was
    compiled with loadable-extension support (required for ``sqlite-vec``).
    """
    for name in _SQLCIPHER_MODULES:
        if importlib.util.find_spec(name) is not None:
            return True
    return False


def _import_sqlcipher():
    """Import the first available SQLCipher DBAPI module, or raise RuntimeError."""
    for name in _SQLCIPHER_MODULES:
        if importlib.util.find_spec(name) is None:
            continue
        module = importlib.import_module(name)
        # `sqlcipher3` exposes the DBAPI under `.dbapi2`; pysqlcipher3 too.
        return getattr(module, "dbapi2", module)
    raise RuntimeError(
        "Database encryption requested but no SQLCipher driver is installed. "
        "Install the maintained 'sqlcipher3-binary' package "
        "(pip install sqlcipher3-binary) and verify that "
        "conn.enable_load_extension(True) + sqlite_vec.load(conn) succeed "
        "against an encrypted connection. Encryption is optional and off the "
        "critical path; the unencrypted core works without it."
    )


def connect_encrypted(db_path: str, key_bytes: bytes):
    """Open a SQLCipher-encrypted SQLite connection.

    The 32-byte ``key_bytes`` is applied as a **raw hex literal** so SQLCipher
    consumes it directly (skipping its own key derivation) and so no
    user-controlled string is ever interpolated into a ``PRAGMA``::

        conn.execute("PRAGMA key = \\"x'<64 hex chars>'\\"")

    Args:
        db_path: Filesystem path to the (encrypted) database.
        key_bytes: Exactly 32 bytes, e.g. from :func:`derive_key` or the OS
            keystore.

    Returns:
        An open, keyed DBAPI connection.

    Raises:
        ValueError: If ``key_bytes`` is not exactly 32 bytes.
        RuntimeError: If no SQLCipher driver is installed. The message
            includes installation guidance. This is intentionally catchable so
            callers can fall back to the unencrypted path.
    """
    if not isinstance(key_bytes, (bytes, bytearray)) or len(key_bytes) != _KEY_LENGTH:
        raise ValueError("expected a 32-byte raw key")

    sqlcipher = _import_sqlcipher()  # raises RuntimeError-with-guidance if absent

    conn = sqlcipher.connect(db_path)
    # Hex literal: built only from key_bytes.hex() (0-9a-f), never from raw
    # user input -> no SQL-injection / quote-breakage surface. PRAGMA cannot
    # take a bind parameter, hence the literal. The C-API sqlite3_key() is the
    # most robust alternative for fully untrusted input.
    hex_key = bytes(key_bytes).hex()
    conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
    logger.debug("Opened encrypted connection to %s", db_path)
    return conn
