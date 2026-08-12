"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_data_encryption.py
@DateTime: 2026-08-13 12:55
@Docs: 共享数据库秘密加解密模块测试。
"""

import pytest
from cryptography.fernet import Fernet

from app.core.cmdb_credential import (
    decrypt_credential_password,
    encrypt_credential_password,
)
from app.core.config import settings
from app.core.data_encryption import (
    DataDecryptError,
    DataEncryptionKeyMissingError,
    decrypt_secret,
    encrypt_secret,
)


def test_shared_data_encryption_round_trip_without_plaintext_in_ciphertext() -> None:
    ciphertext = encrypt_secret("sk-sensitive-value")
    assert "sk-sensitive-value" not in ciphertext
    assert decrypt_secret(ciphertext) == "sk-sensitive-value"


def test_cmdb_wrapper_and_generic_crypto_use_the_same_key() -> None:
    cmdb_ciphertext = encrypt_credential_password("device-password")
    generic_ciphertext = encrypt_secret("llm-api-key")

    assert decrypt_secret(cmdb_ciphertext) == "device-password"
    assert decrypt_credential_password(generic_ciphertext) == "llm-api-key"


def test_encrypt_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", None)

    with pytest.raises(DataEncryptionKeyMissingError, match="CMDB_CREDENTIAL_KEY"):
        encrypt_secret("whatever")


def test_decrypt_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", None)

    with pytest.raises(DataEncryptionKeyMissingError, match="CMDB_CREDENTIAL_KEY"):
        decrypt_secret("gAAAAA-anything")


def test_decrypt_raises_on_ciphertext_from_a_different_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(
        settings,
        "CMDB_CREDENTIAL_KEY",
        SecretStr(Fernet.generate_key().decode()),
    )
    ciphertext = encrypt_secret("sk-sensitive-value")

    monkeypatch.setattr(
        settings,
        "CMDB_CREDENTIAL_KEY",
        SecretStr(Fernet.generate_key().decode()),
    )

    with pytest.raises(DataDecryptError, match="无法解密"):
        decrypt_secret(ciphertext)
