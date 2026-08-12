"""CMDB 资产静态密码的对称加密/解密。

实现流程：
1. 静态设备密码要能在真正连接设备时被程序取回明文，不能像登录密码那样只做
   不可逆哈希；这里复用 app.core.data_encryption 的共享 Fernet 密钥
   CMDB_CREDENTIAL_KEY，不与签发 JWT 的 SECRET_KEY 混用。
2. CMDB_CREDENTIAL_KEY 允许留空（不像 SECRET_KEY 那样强制所有环境配置），
   因为不是每个部署都需要静态密码这个功能；留空时这里直接抛错，调用方
   （API 层）把它转换成明确的错误提示，而不是等到真正用到才炸出裸 500。
3. 本模块保留原有公共函数名，内部委托通用加解密模块，确保已有 CMDB 调用方
   无需修改 import。
"""

from app.core.data_encryption import (
    DataDecryptError,
    DataEncryptionKeyMissingError,
    decrypt_secret,
    encrypt_secret,
)


class CmdbCredentialKeyMissingError(DataEncryptionKeyMissingError):
    """CMDB 凭据缺少共享数据库加密密钥。"""


class CmdbCredentialDecryptError(DataDecryptError):
    """CMDB 凭据密文无法解密。"""


def encrypt_credential_password(plain_password: str) -> str:
    """
    加密一个静态设备密码，返回可入库的密文字符串。

    Args:
        plain_password: 明文设备密码

    Returns:
        Fernet 密文
    """
    try:
        return encrypt_secret(plain_password)
    except DataEncryptionKeyMissingError as exc:
        raise CmdbCredentialKeyMissingError(str(exc)) from exc


def decrypt_credential_password(ciphertext: str) -> str:
    """
    解密一个静态设备密码密文，返回明文。

    Args:
        ciphertext: 数据库中的密文

    Returns:
        明文设备密码
    """
    try:
        return decrypt_secret(ciphertext)
    except DataEncryptionKeyMissingError as exc:
        raise CmdbCredentialKeyMissingError(str(exc)) from exc
    except DataDecryptError as exc:
        raise CmdbCredentialDecryptError(str(exc)) from exc
