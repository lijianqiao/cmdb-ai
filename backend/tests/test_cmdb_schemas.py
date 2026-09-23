"""CmdbAsset 请求/响应模型的凭据一致性校验。"""

import pytest
from pydantic import ValidationError

from app.schemas.cmdb import CmdbAssetCreate, CmdbAssetResponse, CmdbAssetUpdate


def _base_create_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "asset_type": "switch",
        "hostname": "sw-01",
        "ip_address": "10.0.0.1",
        "vendor": "huawei_vrp",
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
    payload = CmdbAssetUpdate.model_validate({"hostname": "sw-renamed"})
    assert payload.hostname == "sw-renamed"
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


# 定位收敛到网络设备：主机类厂商和非网络资产类型不再能登记，
# 旧数据仍能读出来（响应模型是普通字符串），只在创建/编辑时拒绝。


@pytest.mark.parametrize("vendor", ["linux", "generic"])
def test_create_rejects_retired_host_vendors(vendor: str) -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(_base_create_kwargs(vendor=vendor))


def test_create_accepts_other_vendor_for_devices_not_yet_supported() -> None:
    """暂不支持的网络设备厂商以 other 登记：能进台账和依赖图，只是不能下命令。"""
    payload = CmdbAssetCreate.model_validate(_base_create_kwargs(vendor="other"))
    assert payload.vendor == "other"


@pytest.mark.parametrize("asset_type", ["server", "load_balancer", "storage", "anything"])
def test_create_rejects_non_network_asset_types(asset_type: str) -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(_base_create_kwargs(asset_type=asset_type))


@pytest.mark.parametrize(
    "asset_type", ["switch", "router", "firewall", "wireless_controller", "other"]
)
def test_create_accepts_network_asset_types(asset_type: str) -> None:
    payload = CmdbAssetCreate.model_validate(_base_create_kwargs(asset_type=asset_type))
    assert payload.asset_type == asset_type


def test_update_rejects_non_network_asset_type() -> None:
    with pytest.raises(ValidationError):
        CmdbAssetUpdate.model_validate({"asset_type": "server"})


def test_create_defaults_ssh_port_to_22() -> None:
    """不填端口就是 22：已有的登记方式和连接行为都不变。"""
    assert CmdbAssetCreate.model_validate(_base_create_kwargs()).ssh_port == 22


@pytest.mark.parametrize("port", [0, 65536, -1, True])
def test_create_rejects_invalid_ssh_port(port: object) -> None:
    with pytest.raises(ValidationError):
        CmdbAssetCreate.model_validate(_base_create_kwargs(ssh_port=port))


def test_update_can_change_only_the_ssh_port() -> None:
    """改端口不用连带提交凭据：端口和凭据是两件事。"""
    payload = CmdbAssetUpdate.model_validate({"ssh_port": 2222})
    assert payload.ssh_port == 2222
    assert payload.model_fields_set == {"ssh_port"}


def test_update_rejects_explicit_null_ssh_port() -> None:
    """编辑时不传端口就保留原值；显式传 null 会往非空列里写空值，直接拒绝。"""
    with pytest.raises(ValidationError, match="ssh_port"):
        CmdbAssetUpdate.model_validate({"ssh_port": None})


def test_response_still_reads_legacy_asset_type_and_vendor() -> None:
    """清理前的旧数据（服务器、linux）还在库里时，列表和详情必须照样能返回。"""
    response = CmdbAssetResponse.model_validate(
        {
            "id": 1,
            "asset_type": "server",
            "vendor": "linux",
            "hostname": "srv-legacy",
            "ip_address": "10.0.20.11",
            "location": "",
            "owner_user_id": None,
            "business_system": "",
            "subnet_cidr": "",
            "notes": "",
            "credential_type": "none",
            "credential_username": "",
            "credential_password_set": False,
            "ssh_port": 22,
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
        }
    )
    assert response.asset_type == "server"
    assert response.vendor == "linux"
