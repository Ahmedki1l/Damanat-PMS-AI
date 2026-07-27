"""The HikCentral layer must never be able to stop the app from booting.

On 2026-07-27 a deployed `HIK_BASE_URL` was missing its `https://` scheme. The
validator raised, `Settings()` failed at import time, and every gunicorn worker
crash-looped: an optional plate-validation add-on took the entire backend down.

These tests pin the rule that replaced it — a misconfigured HikCentral layer
degrades to `HIK_VALIDATION_MODE=off`, loudly, and nothing else is affected.
Contrast `test_entry_v2_config.py`, which still asserts a hard raise: Entry V2
owns the entry path and has no safe fallback, whereas `off` is exactly the
behaviour that shipped before HikCentral existed.
"""

import pytest

from app.config import Settings

VALID = {
    "HIK_BASE_URL": "https://10.1.20.51",
    "HIK_ENTRY_RESOURCE_IDS": "447",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ── The outage, reproduced ──────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["shadow", "authoritative"])
def test_scheme_less_base_url_disables_the_layer_instead_of_crashing(mode):
    """The exact deployed value that crash-looped production."""
    with pytest.warns(RuntimeWarning, match="scheme"):
        configured = _settings(
            HIK_VALIDATION_MODE=mode,
            HIK_BASE_URL="10.1.20.51",
            HIK_ENTRY_RESOURCE_IDS="447",
        )

    assert configured.HIK_VALIDATION_MODE == "off"
    assert "https://10.1.20.51" in configured.hik_disabled_reason()


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"HIK_BASE_URL": ""}, "HIK_BASE_URL is required"),
        ({"HIK_BASE_URL": "ftp://10.1.20.51"}, "scheme"),
        ({"HIK_BASE_URL": "https://u:p@10.1.20.51"}, "credential-free"),
        ({"HIK_BASE_URL": "https://10.1.20.51/?a=b"}, "credential-free"),
        ({"HIK_BASE_URL": "https://10.1.20.51#frag"}, "credential-free"),
        ({"HIK_BASE_URL": "https://10.1.20.51:notaport"}, "not a valid URL"),
        ({"HIK_ENTRY_RESOURCE_IDS": " , "}, "HIK_ENTRY_RESOURCE_IDS is required"),
    ],
)
def test_every_misconfiguration_degrades_rather_than_raises(overrides, expected):
    with pytest.warns(RuntimeWarning, match=expected):
        configured = _settings(
            HIK_VALIDATION_MODE="authoritative", **{**VALID, **overrides}
        )

    assert configured.HIK_VALIDATION_MODE == "off"
    assert configured.hik_disabled_reason()


def test_missing_credentials_do_not_disable_the_layer():
    """The real login payload is not captured yet; the client owns auth."""
    configured = _settings(
        HIK_VALIDATION_MODE="shadow",
        HIK_USERNAME="",
        HIK_PASSWORD="",
        **VALID,
    )

    assert configured.HIK_VALIDATION_MODE == "shadow"
    assert configured.hik_disabled_reason() == ""


# ── The happy path still works ──────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["shadow", "authoritative"])
def test_valid_configuration_is_preserved(mode):
    configured = _settings(HIK_VALIDATION_MODE=mode, **VALID)

    assert configured.HIK_VALIDATION_MODE == mode
    assert configured.hik_disabled_reason() == ""
    assert configured.hik_entry_resource_ids() == "447"


def test_trailing_slash_is_stripped_so_paths_do_not_double_up():
    configured = _settings(
        HIK_VALIDATION_MODE="shadow",
        HIK_BASE_URL="https://10.1.20.51/",
        HIK_ENTRY_RESOURCE_IDS="447",
    )

    assert configured.HIK_BASE_URL == "https://10.1.20.51"


def test_off_mode_ignores_configuration_entirely():
    """A blank, invalid config must stay silent when the layer is off."""
    configured = _settings(HIK_VALIDATION_MODE="off", HIK_BASE_URL="nonsense")

    assert configured.HIK_VALIDATION_MODE == "off"
    assert configured.hik_disabled_reason() == ""
