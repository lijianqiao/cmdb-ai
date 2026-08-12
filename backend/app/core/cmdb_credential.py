"""CMDB 资产静态密码的对称加密/解密。

实现流程：
1. 静态设备密码要能在真正连接设备时被程序取回明文，不能像登录密码那样只做
   不可逆哈希；这里用 cryptography 的 Fernet 对称加密（AES128-CBC + HMAC），
   密钥来自独立配置项 CMDB_CREDENTIAL_KEY，不与签发 JWT 的 SECRET_KEY 混用——
   两者的轮换周期和影响面完全不同，混用会让"改一个密钥顺带搞坏另一个功能"。
2. CMDB_CREDENTIAL_KEY 允许留空（不像 SECRET_KEY 那样强制所有环境配置），
   因为不是每个部署都需要静态密码这个功能；留空时这里直接抛错，调用方
   （API 层）把它转换成明确的错误提示，而不是等到真正用到才炸出裸 500。
3. 全部加解密只在这一个模块里发生，其它代码只通过这里的两个函数接触明文
   密码，方便审计"明文密码到底在哪些地方出现过"——目前的答案是：只在这里，
   以及调用它的 API 请求体反序列化那一刻。
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class CmdbCredentialKeyMissingError(RuntimeError):
    """CMDB_CREDENTIAL_KEY 未配置，无法加密或解密静态密码。"""


class CmdbCredentialDecryptError(RuntimeError):
    """密文损坏或密钥已更换，无法解密。"""


def _fernet() -> Fernet:
    key = settings.CMDB_CREDENTIAL_KEY
    if key is None or not key.get_secret_value().strip():
        raise CmdbCredentialKeyMissingError(
            "CMDB_CREDENTIAL_KEY 未配置，无法保存或读取静态密码"
        )
    return Fernet(key.get_secret_value().encode("utf-8"))


def encrypt_credential_password(plain_password: str) -> str:
    """加密一个静态设备密码，返回可入库的密文字符串。"""
    return _fernet().encrypt(plain_password.encode("utf-8")).decode("utf-8")


def decrypt_credential_password(ciphertext: str) -> str:
    """解密一个静态设备密码密文，返回明文。"""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise CmdbCredentialDecryptError(
            "密文无法解密，密钥可能已更换或数据已损坏"
        ) from exc
