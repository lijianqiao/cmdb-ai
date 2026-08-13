"""CMDB asset request and response models."""

from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.agent.device_commands import VendorName
from app.schemas.common import ApiModel

type CredentialType = Literal["none", "static", "dynamic"]

_CREDENTIAL_FIELDS = {"credential_type", "credential_username", "credential_password"}


class CmdbAssetCreate(ApiModel):
    """Create a CMDB asset, optionally with a login credential."""

    asset_type: str = Field(min_length=1, max_length=50)
    vendor: VendorName
    hostname: str = Field(min_length=1, max_length=255)
    ip_address: str = Field(min_length=1, max_length=45)
    location: str = Field(default="", max_length=200)
    owner_user_id: int | None = None
    business_system: str = Field(default="", max_length=100)
    subnet_cidr: str = Field(default="", max_length=45)
    notes: str = Field(default="", max_length=2000)
    credential_type: CredentialType = "none"
    credential_username: str = Field(default="", max_length=100)
    credential_password: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_credential(self) -> Self:
        if self.credential_type == "none":
            if self.credential_username or self.credential_password is not None:
                raise ValueError("credential_type 为 none 时不能填写账号或密码")
        elif self.credential_type == "static":
            if not self.credential_username:
                raise ValueError("静态凭据必须填写账号")
            if self.credential_password is None:
                raise ValueError("静态凭据必须填写密码")
        elif self.credential_type == "dynamic":
            if not self.credential_username:
                raise ValueError("动态凭据必须填写账号")
            if self.credential_password is not None:
                raise ValueError("动态凭据不需要也不允许填写密码")
        return self


class CmdbAssetUpdate(ApiModel):
    """Partially update a CMDB asset; unset fields are left untouched."""

    asset_type: str | None = Field(default=None, min_length=1, max_length=50)
    vendor: VendorName | None = None
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    ip_address: str | None = Field(default=None, min_length=1, max_length=45)
    location: str | None = Field(default=None, max_length=200)
    owner_user_id: int | None = None
    business_system: str | None = Field(default=None, max_length=100)
    subnet_cidr: str | None = Field(default=None, max_length=45)
    notes: str | None = Field(default=None, max_length=2000)
    credential_type: CredentialType | None = None
    credential_username: str | None = Field(default=None, max_length=100)
    credential_password: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_credential(self) -> Self:
        touched = _CREDENTIAL_FIELDS & self.model_fields_set
        if not touched:
            return self
        if "credential_type" not in self.model_fields_set:
            raise ValueError("修改凭据信息时必须同时提供 credential_type")

        if self.credential_type == "none":
            if self.credential_username or self.credential_password is not None:
                raise ValueError("credential_type 为 none 时不能填写账号或密码")
        elif self.credential_type == "static":
            if not self.credential_username:
                raise ValueError("静态凭据必须填写账号")
            # 密码字段允许不传（保留原密文），但显式传入时不能是空字符串。
        elif self.credential_type == "dynamic":
            if not self.credential_username:
                raise ValueError("动态凭据必须填写账号")
            if self.credential_password is not None:
                raise ValueError("动态凭据不需要也不允许填写密码")
        return self

    @model_validator(mode="after")
    def reject_empty_update(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        return self


class CmdbAssetResponse(ApiModel):
    """Public asset representation — ciphertext and plaintext password never appear here."""

    id: int
    asset_type: str
    vendor: str
    hostname: str
    ip_address: str
    location: str
    owner_user_id: int | None
    business_system: str
    subnet_cidr: str
    notes: str
    credential_type: CredentialType
    credential_username: str
    credential_password_set: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CmdbAssetDependencyCreate(ApiModel):
    """Add one dependency edge from an existing parent asset to another asset."""

    child_asset_id: int = Field(ge=1)
    relation_type: str = Field(min_length=1, max_length=50)


class CmdbAssetDependencyResponse(ApiModel):
    """One directed dependency edge between two CMDB assets."""

    model_config = ConfigDict(from_attributes=True)

    parent_asset_id: int
    child_asset_id: int
    relation_type: str
    created_at: datetime


class CmdbAssetDependencyListResponse(ApiModel):
    """One asset's direct dependency edges in both directions."""

    children: list[CmdbAssetDependencyResponse]
    parents: list[CmdbAssetDependencyResponse]


class CmdbCredentialRevealResponse(ApiModel):
    """按需解密的静态凭据明文，仅通过专用查看接口返回。"""

    password: str
