"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: data_encryption.py
@DateTime: 2026-08-13 12:55
@Docs: 使用 CMDB_CREDENTIAL_KEY 对数据库可逆秘密值做 Fernet 加解密。
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class DataEncryptionKeyMissingError(RuntimeError):
    """共享数据库加密密钥未配置。"""


class DataDecryptError(RuntimeError):
    """数据库密文损坏或共享密钥不匹配。"""


def _fernet() -> Fernet:
    key = settings.CMDB_CREDENTIAL_KEY
    if key is None or not key.get_secret_value().strip():
        raise DataEncryptionKeyMissingError(
            "CMDB_CREDENTIAL_KEY 未配置，无法保存或读取数据库秘密值"
        )
    return Fernet(key.get_secret_value().encode("utf-8"))


def encrypt_secret(plain_value: str) -> str:
    """
    加密明文秘密值。

    Args:
        plain_value: 待加密的明文字符串

    Returns:
        可入库的密文字符串
    """
    return _fernet().encrypt(plain_value.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """
    解密密文秘密值。

    Args:
        ciphertext: 数据库中的密文字符串

    Returns:
        解密后的明文

    Raises:
        DataDecryptError: 密文无法解密时
    """
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise DataDecryptError("数据库密文无法解密，共享密钥可能已更换") from exc
