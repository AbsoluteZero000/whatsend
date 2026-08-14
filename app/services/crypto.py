import base64
import hashlib

from cryptography.fernet import Fernet, MultiFernet

from app.config import settings


def _derive_key(value: str) -> bytes:
    raw = hashlib.sha256(value.encode()).digest()
    return base64.urlsafe_b64encode(raw)


_primary_secret = settings.token_encryption_key or settings.secret_key
_fernets = [Fernet(_derive_key(_primary_secret))]
if settings.token_encryption_key and settings.token_encryption_key != settings.secret_key:
    _fernets.append(Fernet(_derive_key(settings.secret_key)))
_fernet = MultiFernet(_fernets)


def encrypt_token(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_token(cipher: str) -> str:
    return _fernet.decrypt(cipher.encode()).decode()
