"""Settings parsing — the values that arrive as *env vars* on a deploy.

The regression this file pins: pydantic-settings JSON-decodes ``list[...]`` env
values before any validator runs, so the comma-separated form documented in
``.env.example`` (``ALLOWED_MEDIA_HOSTS=a,b``) crashed every Render deploy with
``error parsing value for field "allowed_media_hosts" from source
"EnvSettingsSource"``. The fields are ``NoDecode`` now, and both the
comma-separated and JSON-array forms must keep working.
"""

from __future__ import annotations

import pytest
from pydantic_settings import SettingsError

from waifu.settings import Settings


def _settings(**env: object) -> Settings:
    return Settings(
        bot_token="123:abc",
        owner_id=1,
        redis_url="redis://localhost:6379/0",
        **env,
    )


@pytest.mark.parametrize(
    "value",
    [
        "api.telegram.org,files.catbox.moe",  # the documented .env.example form
        " api.telegram.org , cdn.telegram.org ",  # humans add spaces
        '["api.telegram.org", "cdn.telegram.org"]',  # a JSON array works too
        "",  # empty = "use the defaults path" — must not crash
    ],
)
def test_allowed_media_hosts_env_forms(monkeypatch, value: str) -> None:
    monkeypatch.setenv("ALLOWED_MEDIA_HOSTS", value)
    # a fresh Settings() reads the environment; the value above must parse
    settings = Settings(
        bot_token="123:abc",
        owner_id=1,
        admin_ids=[1],
        redis_url="redis://localhost:6379/0",
    )
    if value == "":
        assert settings.allowed_media_hosts == []
    elif value == '["api.telegram.org", "cdn.telegram.org"]':
        assert settings.allowed_media_hosts == ["api.telegram.org", "cdn.telegram.org"]
    else:
        assert settings.allowed_media_hosts == [p.strip() for p in value.split(",")]


def test_admin_ids_env_form(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_IDS", "1, 42, 99")
    settings = Settings(
        bot_token="123:abc",
        owner_id=1,
        redis_url="redis://localhost:6379/0",
    )
    assert settings.admin_ids == [1, 42, 99]


def test_programmatic_lists_still_accept_real_lists() -> None:
    """NoDecode must not leak into keyword construction (tests, doctor, CI)."""
    s = _settings(
        allowed_media_hosts=["example.com"],
        admin_ids=[7],
    )
    assert s.allowed_media_hosts == ["example.com"]
    assert s.admin_ids == [7]


def test_garbage_env_is_a_clean_validation_error(monkeypatch) -> None:
    """A truly unparseable value must raise a pydantic ValidationError-ish error,
    never the opaque SettingsError that killed the deploy."""
    monkeypatch.setenv("ADMIN_IDS", "one,two,three")
    with pytest.raises(Exception) as excinfo:
        Settings(
            bot_token="123:abc",
            owner_id=1,
            redis_url="redis://localhost:6379/0",
        )
    # Not the env-decode failure that killed the Render deploy.
    assert not isinstance(excinfo.value, SettingsError)


def test_deploy_smoke_redis_storage_builds_without_a_live_server() -> None:
    """The Render deploy died twice on startup, both times *after* settings parsed:
    once on env-var list decoding, once on ``DefaultKeyBuilder(global_prefix=…)`` —
    a keyword aiogram 3.31 never had. Both paths die before the first update, so
    neither is caught by a test that only drives the dispatcher on MemoryStorage.
    This pins the whole startup chain: env settings → RedisStorage construction.
    """
    import os

    from aiogram.fsm.storage.redis import RedisStorage

    from waifu.core.dp import build_storage

    os.environ["ADMIN_IDS"] = "1, 42"
    os.environ["ALLOWED_MEDIA_HOSTS"] = "api.telegram.org,cdn.telegram.org"
    try:
        settings = Settings(
            bot_token="123:abc",
            owner_id=1,
            redis_url="redis://user:pass@deploy-host:6379/0",
        )
        storage = build_storage(settings)
        # Construction must not require the server to be up (lazy connection).
        assert isinstance(storage, RedisStorage)
    finally:
        os.environ.pop("ADMIN_IDS", None)
        os.environ.pop("ALLOWED_MEDIA_HOSTS", None)


@pytest.mark.parametrize(
    ("env_name", "value", "field", "expected"),
    [
        ("GUESS_REACTIONS", "🔥,⭐,💀", "guess_reactions", ["🔥", "⭐", "💀"]),
        ("STREAK_MULTIPLIER_CURVE", "1.0, 1.1, 1.2", "streak_multiplier_curve", [1.0, 1.1, 1.2]),
    ],
)
def test_other_list_env_fields_parse(
    monkeypatch, env_name: str, value: str, field: str, expected: list
) -> None:
    """Same latent bug class as ALLOWED_MEDIA_HOSTS: any list-typed field whose env
    value is comma-separated would have died the same deploy-death. All four
    list fields are NoDecode now — pin each one."""
    monkeypatch.setenv(env_name, value)
    settings = Settings(
        bot_token="123:abc",
        owner_id=1,
        redis_url="redis://localhost:6379/0",
    )
    assert getattr(settings, field) == expected


def test_settings_error_explainer_names_the_variable(monkeypatch, capsys) -> None:
    """The deploy log must become an answer: which var, what value, what format."""
    from pydantic_settings import SettingsError

    import waifu.__main__ as cli

    monkeypatch.setenv("ALLOWED_MEDIA_HOSTS", "api.telegram.org,cdn.telegram.org")
    cli._explain_settings_error(
        SettingsError(
            'error parsing value for field "allowed_media_hosts" from source "EnvSettingsSource"'
        )
    )
    out = capsys.readouterr().err
    assert "ALLOWED_MEDIA_HOSTS" in out
    assert "api.telegram.org" in out
    assert "comma-separated" in out
