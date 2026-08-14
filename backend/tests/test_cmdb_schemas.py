"""CmdbAsset 请求/响应模型的凭据一致性校验。"""

import pytest
from pydantic import ValidationError

from app.schemas.cmdb import CmdbAssetCreate, CmdbAssetResponse, CmdbAssetUpdate


def _base_create_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "asset_type": "server",
        "hostname": "srv-01",
        "ip_address": "10.0.0.1",
        "vendor": "generic",
    }
    kwargs.update(overrides)
    return kwargs


def test_create_defaults_to_no_credential() -> None:
    payload = CmdbAssetCreate.model_validate(_base_create_kwargs())
    assert payload.credential_type == "none"
    assert payload.credential_username == ""
    assert payload.credential_password is None


def test_create_none_type_rejects_username_or_password() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(
            _base_create_kwargs(credential_type="none", credential_username="admin")
        )


def test_create_static_requires_username_and_password() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(
            _base_create_kwargs(credential_type="static", credential_username="admin")
        )

    ok = CmdbAssetCreate.model_validate(
        _base_create_kwargs(
            credential_type="static", credential_username="admin", credential_password="p@ss"
        )
    )
    assert ok.credential_password == "p@ss"


def test_create_dynamic_requires_username_and_rejects_password() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(_base_create_kwargs(credential_type="dynamic"))

    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(
            _base_create_kwargs(
                credential_type="dynamic", credential_username="admin", credential_password="nope"
            )
        )

    ok = CmdbAssetCreate.model_validate(
        _base_create_kwargs(credential_type="dynamic", credential_username="admin")
    )
    assert ok.credential_username == "admin"
    assert ok.credential_password is None


def test_update_allows_partial_fields_without_touching_credentials() -> None:
    payload = CmdbAssetUpdate.model_validate({"hostname": "srv-renamed"})
    assert payload.hostname == "srv-renamed"
    assert "credential_type" not in payload.model_fields_set


def test_update_credential_type_must_be_provided_alongside_other_credential_fields() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetUpdate.model_validate({"credential_username": "admin"})


def test_update_static_password_can_be_omitted_to_keep_existing_secret() -> None:
    payload = CmdbAssetUpdate.model_validate(
        {"credential_type": "static", "credential_username": "admin"}
    )
    assert payload.credential_type == "static"
    assert "credential_password" not in payload.model_fields_set


def test_response_never_exposes_ciphertext_field() -> None:
    assert "credential_password_encrypted" not in CmdbAssetResponse.model_fields
    assert "credential_password" not in CmdbAssetResponse.model_fields
    assert "credential_password_set" in CmdbAssetResponse.model_fields


def test_create_requires_valid_vendor() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(_base_create_kwargs(vendor="totally_made_up"))


def test_create_accepts_catalog_vendor() -> None:
    payload = CmdbAssetCreate.model_validate(_base_create_kwargs(vendor="huawei_vrp"))
    assert payload.vendor == "huawei_vrp"


def test_create_accepts_cisco_small_business_vendor() -> None:
    payload = CmdbAssetCreate.model_validate(
        _base_create_kwargs(vendor="cisco_small_business")
    )
    assert payload.vendor == "cisco_small_business"
