"""Field-level encryption for sensitive columns (phone, national id, MFA secrets).

Uses Fernet (AES-128-CBC + HMAC-SHA256, authenticated). ``MultiFernet`` lets us rotate keys:
put the new key first, keep old keys after it, and re-encrypt lazily.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CryptoError(Exception):
    """Raised when a value cannot be encrypted or authenticated/decrypted."""


class FieldCipher:
    def __init__(self, keys: str) -> None:
        parts = [key.strip() for key in keys.split(",") if key.strip()]
        if not parts:
            raise CryptoError("at least one encryption key is required")
        try:
            self._fernet = MultiFernet([Fernet(key.encode()) for key in parts])
        except ValueError as exc:
            raise CryptoError("invalid encryption key format") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise CryptoError("ciphertext failed authentication") from exc
