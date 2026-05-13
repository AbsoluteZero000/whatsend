import pytest
from app.services.crypto import decrypt_token, encrypt_token


def test_encrypt_decrypt_roundtrip():
    original = "whapi-secret-token-abc123"
    encrypted = encrypt_token(original)
    assert encrypted != original, "encrypted value should differ from plaintext"
    decrypted = decrypt_token(encrypted)
    assert decrypted == original, "decrypted should match original"


def test_encryption_produces_different_outputs():
    token = "same-token"
    e1 = encrypt_token(token)
    e2 = encrypt_token(token)
    assert e1 != e2, "Fernet produces different ciphertext each time (IV)"


def test_decrypt_with_wrong_key_fails():
    from app.services.crypto import encrypt_token as enc
    from cryptography.fernet import Fernet, InvalidToken
    import hashlib, base64

    wrong = base64.urlsafe_b64encode(hashlib.sha256(b"wrong-key").digest())
    f = Fernet(wrong)
    encrypted = enc("test")
    with pytest.raises(InvalidToken):
        f.decrypt(encrypted.encode())


def test_empty_token():
    encrypted = encrypt_token("")
    decrypted = decrypt_token(encrypted)
    assert decrypted == ""


def test_long_token():
    original = "x" * 500
    encrypted = encrypt_token(original)
    decrypted = decrypt_token(encrypted)
    assert decrypted == original


def test_special_characters():
    original = "!@#$%^&*()_+-=[]{}|;':\",./<>?`~ WHAPI_TOKEN_123"
    encrypted = encrypt_token(original)
    decrypted = decrypt_token(encrypted)
    assert decrypted == original
