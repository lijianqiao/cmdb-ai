"""Fail-fast production configuration tests."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _production_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "ENVIRONMENT": "production",
        "DATABASE_URL": "postgresql+psycopg://app:safe-password@db:5432/admin",
        "SECRET_KEY": "production-secret-key-that-is-long-enough",
        "COOKIE_SECURE": True,
        "BACKEND_CORS_ORIGINS": "https://admin.example.com",
        "ALLOWED_HOSTS": "admin.example.com",
        "INIT_SUPERUSER_USERNAME": None,
        "INIT_SUPERUSER_EMAIL": None,
        "INIT_SUPERUSER_PASSWORD": None,
    }
    config.update(overrides)
    return config


def test_production_requires_explicit_secret_key() -> None:
    config = _production_config()
    config["SECRET_KEY"] = None

    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings.model_validate(config)


@pytest.mark.parametrize("password", ["", "change-me", "password", "postgres"])
def test_production_rejects_placeholder_database_password(password: str) -> None:
    database_url = f"postgresql+psycopg://app:{password}@db:5432/admin"

    with pytest.raises(ValidationError, match="数据库"):
        Settings.model_validate(_production_config(DATABASE_URL=database_url))


def test_production_accepts_url_encoded_database_password() -> None:
    settings = Settings.model_validate(
        _production_config(
            DATABASE_URL="postgresql+psycopg://app:p%40ssword@db:5432/admin",
        )
    )

    assert settings.ENVIRONMENT == "production"


def test_production_migration_requires_explicit_privileged_url() -> None:
    settings = Settings.model_validate(_production_config())

    with pytest.raises(RuntimeError, match="MIGRATION_DATABASE_URL"):
        _ = settings.migration_database_url


def test_production_migration_requires_distinct_database_role() -> None:
    settings = Settings.model_validate(
        _production_config(
            MIGRATION_DATABASE_URL=("postgresql+psycopg://app:another-safe-password@db:5432/admin")
        )
    )

    with pytest.raises(RuntimeError, match="账号分离"):
        _ = settings.migration_database_url


def test_production_accepts_distinct_migration_database_role() -> None:
    migration_url = "postgresql+psycopg://migrator:safe-password@db:5432/admin"
    settings = Settings.model_validate(_production_config(MIGRATION_DATABASE_URL=migration_url))

    assert settings.migration_database_url == migration_url


def test_llm_price_settings_reject_negative_values() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            SECRET_KEY="x" * 32,
            LLM_CHAT_INPUT_COST_PER_MILLION_USD=-0.01,
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_llm_price_settings_reject_non_finite_values(bad: float) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            SECRET_KEY="x" * 32,
            LLM_CHAT_INPUT_COST_PER_MILLION_USD=bad,
        )


def test_migration_database_url_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="数据库迁移仅支持 PostgreSQL"):
        Settings.model_validate(
            {
                "ENVIRONMENT": "test",
                "DATABASE_URL": "sqlite+aiosqlite://",
                "MIGRATION_DATABASE_URL": "sqlite:///migration.db",
            }
        )


def test_production_rejects_wildcard_allowed_hosts() -> None:
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        Settings.model_validate(_production_config(ALLOWED_HOSTS="*"))


def test_production_rejects_development_cors_origins() -> None:
    with pytest.raises(ValidationError, match="CORS"):
        Settings.model_validate(
            _production_config(BACKEND_CORS_ORIGINS="http://localhost:5173,http://localhost:3000")
        )


def test_production_rejects_insecure_cors_origin() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings.model_validate(_production_config(BACKEND_CORS_ORIGINS="http://admin.example.com"))


def test_production_rejects_development_allowed_hosts() -> None:
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        Settings.model_validate(_production_config(ALLOWED_HOSTS="localhost"))


def test_production_rejects_non_postgresql_database() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings.model_validate(
            _production_config(DATABASE_URL="sqlite+aiosqlite:///production.db")
        )


def test_non_production_generates_ephemeral_secret() -> None:
    settings = Settings.model_validate(
        {
            "ENVIRONMENT": "test",
            "DATABASE_URL": "sqlite+aiosqlite://",
        }
    )

    assert len(settings.secret_key) >= 32
    assert settings.migration_database_url == settings.DATABASE_URL


def test_empty_cmdb_credential_key_treated_as_unset() -> None:
    settings = Settings(_env_file=None, SECRET_KEY="x" * 32, CMDB_CREDENTIAL_KEY="")

    assert settings.CMDB_CREDENTIAL_KEY is None


def test_spawn_limit_defaults_are_bounded() -> None:
    value = Settings(_env_file=None, SECRET_KEY="x" * 32)

    assert value.AGENT_MAX_CONCURRENT_CHILDREN == 5
    assert value.AGENT_MAX_SPAWN_DEPTH == 2
    assert value.AGENT_MAX_CHILDREN_PER_SESSION == 50
    assert value.AGENT_MAX_TOTAL_CHILD_COST_USD == 5.0
    assert value.AGENT_CHILD_MAX_STEPS == 20
    assert value.AGENT_CHILD_MAX_COST_USD == 1.0
    assert value.AGENT_CHILD_MAX_WALL_TIME_SECONDS == 120.0
    assert value.AGENT_CLOSE_TIMEOUT_SECONDS == 5.0
    assert value.AGENT_TERMINAL_RECEIPT_TTL_SECONDS == 300.0
    assert value.AGENT_RECEIPT_GC_INTERVAL_SECONDS == 60.0


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("AGENT_MAX_CONCURRENT_CHILDREN", 0),
        ("AGENT_MAX_SPAWN_DEPTH", 0),
        ("AGENT_MAX_TOTAL_CHILD_COST_USD", -0.01),
        ("AGENT_MAX_TOTAL_CHILD_COST_USD", float("inf")),
        ("AGENT_CHILD_MAX_COST_USD", float("nan")),
        ("AGENT_CHILD_MAX_WALL_TIME_SECONDS", 0),
        ("AGENT_CLOSE_TIMEOUT_SECONDS", 0),
    ],
)
def test_spawn_limits_reject_invalid_values(name: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SECRET_KEY="x" * 32, **{name: bad})
