"""crypto.py — Symmetric credential decryption helper.

The API Gateway encrypts camera passwords (cameras.password_encrypted) with a
Python `cryptography` Fernet cipher (AES-128-CBC + HMAC-SHA256, urlsafe-base64;
IV/timestamp/HMAC embedded in the token). We share the same urlsafe-base64
32-byte key — read from CAMERAS_ENCRYPTION_KEY — to decrypt them here.

``make_decrypt`` returns a callable so the (possibly invalid) Fernet cipher is
constructed once, lazily, and never eagerly with a blank key — a blank key is
the expected "fill it in later" state and must not crash boot.
"""

from typing import Callable, Optional

from cryptography.fernet import Fernet, InvalidToken


def make_decrypt(key: Optional[str]) -> Callable[[Optional[str]], Optional[str]]:
    """Return a ``decrypt(token) -> str | None`` callable.

    With a blank/unset key, returns a no-op that yields ``None`` (the
    "blank-for-now" case) instead of constructing Fernet eagerly — `Fernet(key)`
    raises ``ValueError`` on a malformed key, which we only want to surface when
    a real key was actually provided. Empty or undecryptable tokens yield
    ``None`` so callers can fall back to another password source.
    """
    if not key:
        return lambda token: None

    cipher = Fernet(key.encode())  # raises ValueError on a malformed key

    def decrypt(token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        try:
            return cipher.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

    return decrypt
