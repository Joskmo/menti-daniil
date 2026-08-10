import pytest

from bridge.config import Settings

VALID_SETTINGS = {
    "YONOTE_BASE_URL": "https://joskmo.yonote.ru",
    "YONOTE_API_KEY": "yn_0123456789abcdefABCDEFghijklMNOP",
    "GITHUB_WEBHOOK_SECRET": (
        "Gh7w9_hk2YQaZp0D6vN4cX8sT3mJ5rU1BfKqL-eWioCPbAznVxMdtyERSuOgIlHj"
    ),
    "YONOTE_WEBHOOK_PATH_SECRET": (
        "Yo4pF8nR0-xCt9kW2qAz6mHs1Vb5JdE7uLg3iONyKQcZrPTaUMeDXjIfSwBvlG_h"
    ),
}


def test_settings_require_nonempty_secrets() -> None:
    values = {**VALID_SETTINGS, "YONOTE_API_KEY": ""}

    with pytest.raises(ValueError, match="YONOTE_API_KEY"):
        Settings.from_mapping(values)


def test_settings_have_safe_deployment_defaults() -> None:
    settings = Settings.from_mapping(VALID_SETTINGS)

    assert settings.repository == "Joskmo/menti-daniil"
    assert settings.base_branch == "main"
    assert settings.poll_interval == 15
    assert settings.port == 8080


@pytest.mark.parametrize(
    "name",
    ["YONOTE_API_KEY", "GITHUB_WEBHOOK_SECRET", "YONOTE_WEBHOOK_PATH_SECRET"],
)
def test_settings_reject_placeholder_secrets(name: str) -> None:
    values = {**VALID_SETTINGS, name: "replace-me"}

    with pytest.raises(ValueError, match=name):
        Settings.from_mapping(values)


@pytest.mark.parametrize(
    "name",
    ["YONOTE_API_KEY", "GITHUB_WEBHOOK_SECRET", "YONOTE_WEBHOOK_PATH_SECRET"],
)
def test_settings_reject_secrets_shorter_than_32_characters(name: str) -> None:
    values = {**VALID_SETTINGS, name: "x" * 31}

    with pytest.raises(ValueError, match=name):
        Settings.from_mapping(values)


def test_settings_reject_repetitive_secrets() -> None:
    values = {**VALID_SETTINGS, "GITHUB_WEBHOOK_SECRET": "x" * 32}

    with pytest.raises(ValueError, match="GITHUB_WEBHOOK_SECRET"):
        Settings.from_mapping(values)


@pytest.mark.parametrize(
    "predictable",
    [
        "0123456789abcdefghijklmnopqrstuv",
        "vutsrqponmlkjihgfedcba9876543210",
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_",
    ],
)
def test_settings_reject_predictable_sequence_secrets(predictable: str) -> None:
    values = {**VALID_SETTINGS, "GITHUB_WEBHOOK_SECRET": predictable}

    with pytest.raises(ValueError, match="GITHUB_WEBHOOK_SECRET"):
        Settings.from_mapping(values)


def test_settings_reject_extreme_character_imbalance() -> None:
    values = {**VALID_SETTINGS, "GITHUB_WEBHOOK_SECRET": "A" * 63 + "B"}

    with pytest.raises(ValueError, match="GITHUB_WEBHOOK_SECRET"):
        Settings.from_mapping(values)


def test_settings_reject_secret_reuse() -> None:
    values = {
        **VALID_SETTINGS,
        "YONOTE_WEBHOOK_PATH_SECRET": VALID_SETTINGS["GITHUB_WEBHOOK_SECRET"],
    }

    with pytest.raises(ValueError, match="must be distinct"):
        Settings.from_mapping(values)


@pytest.mark.parametrize("interval", ["0", "-1"])
def test_settings_require_positive_poll_interval(interval: str) -> None:
    values = {**VALID_SETTINGS, "POLL_INTERVAL_SECONDS": interval}

    with pytest.raises(ValueError, match="POLL_INTERVAL_SECONDS"):
        Settings.from_mapping(values)


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_settings_require_port_in_tcp_range(port: str) -> None:
    values = {**VALID_SETTINGS, "PORT": port}

    with pytest.raises(ValueError, match="PORT"):
        Settings.from_mapping(values)


@pytest.mark.parametrize("port", ["1", "65535"])
def test_settings_accept_port_range_boundaries(port: str) -> None:
    settings = Settings.from_mapping({**VALID_SETTINGS, "PORT": port})

    assert settings.port == int(port)
