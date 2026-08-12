"""对称加解密 CMDB 静态设备密码。"""

import pytest
from cryptography.fernet import Fernet

from app.core import cmdb_credential  # noqa: F401
from app.core.cmdb_credential import (
    CmdbCredentialDecryptError,
    CmdbCredentialKeyMissingError,
    decrypt_credential_password,
    encrypt_credential_password,
)
from app.core.config import settings


def test_encrypt_then_decrypt_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))

    ciphertext = encrypt_credential_password("Sup3rSecret!")

    assert ciphertext != "Sup3rSecret!"
    assert decrypt_credential_password(ciphertext) == "Sup3rSecret!"


def test_encrypt_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", None)

    with pytest.raises(CmdbCredentialKeyMissingError):
        encrypt_credential_password("whatever")


def test_encrypt_raises_when_key_is_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(""))

    with pytest.raises(CmdbCredentialKeyMissingError):
        encrypt_credential_password("whatever")


def test_decrypt_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", None)

    with pytest.raises(CmdbCredentialKeyMissingError):
        decrypt_credential_password("gAAAAA-anything")


def test_decrypt_raises_on_ciphertext_from_a_different_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    ciphertext = encrypt_credential_password("Sup3rSecret!")

    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))

    with pytest.raises(CmdbCredentialDecryptError):
        decrypt_credential_password(ciphertext)
