"""Configuration loading regressions using synthetic environment data."""

from io import StringIO

import pytest

from meta_ads_mcp import config


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_request_timeout_must_be_positive_and_finite(monkeypatch, value):
    with monkeypatch.context() as scoped:
        scoped.setenv("META_REQUEST_TIMEOUT", value)
        with pytest.raises(ValueError, match="finite positive"):
            config.reload_settings()


def test_api_version_defaults_to_v26(monkeypatch) -> None:
    monkeypatch.delenv("META_API_VERSION", raising=False)

    assert config.reload_settings().api_version == "v26.0"


def test_api_version_preserves_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv("META_API_VERSION", "v25.0")

    assert config.reload_settings().api_version == "v25.0"


@pytest.mark.parametrize(("contents", "variable", "attribute", "expected"), [
    ("\ufeffMETA_ACCESS_TOKEN=test-bom-token\n", "META_ACCESS_TOKEN", "access_token", "test-bom-token"),
    ("META_APP_SECRET= # intentionally unset\n", "META_APP_SECRET", "app_secret", ""),
])
def test_dotenv_loading_preserves_bom_and_empty_values(monkeypatch, contents, variable, attribute, expected) -> None:
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setenv(variable, "existing-test-value")
    config.load_dotenv(stream=StringIO(contents), override=True)

    assert getattr(config.reload_settings(), attribute) == expected


def test_dotenv_loading_keeps_existing_environment_precedence(monkeypatch) -> None:
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setenv("META_ACCESS_TOKEN", "existing-test-token")
    config.load_dotenv(stream=StringIO("META_ACCESS_TOKEN=file-test-token\n"))

    assert config.reload_settings().access_token == "existing-test-token"
