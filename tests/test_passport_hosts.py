"""US/ore Passport routing regression for issue #29.

The legacy US hostname returns 90060 even when a supported app_version is
provided. The official Android app uses the ore.willand.com Passport host.
All tests here are offline and use synthetic credentials/tokens only.
"""

from custom_components.navimow_pro.api import passport
from custom_components.navimow_pro.const import (
    ALL_PASSPORT_HOSTS,
    passport_hosts,
)

US_HOST = "api-passport-ore.willand.com"
LEGACY_US_HOST = "api-passport-us.ninebot.com"


def test_us_and_ore_choose_the_official_passport_host():
    assert passport_hosts("us")[0] == US_HOST
    assert passport_hosts("ore")[0] == US_HOST
    assert ALL_PASSPORT_HOSTS.index(US_HOST) < ALL_PASSPORT_HOSTS.index(
        LEGACY_US_HOST
    )


def test_european_passport_host_priority_is_unchanged():
    assert passport_hosts("fra")[0] == "api-passport-fra.willand.com"


def test_us_login_does_not_hit_legacy_90060_host(monkeypatch):
    called = []

    def fake_post(path, params, host=passport.DEFAULT_HOST, timeout=20):
        called.append((host, path))
        if host != US_HOST:
            return {"resultCode": "90060", "resultDesc": "App version too low"}
        return {
            "resultCode": "90000",
            "data": {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "region": "ore",
            },
        }

    monkeypatch.setattr(passport, "_post", fake_post)
    tokens = passport.login(
        "synthetic@example.invalid", "synthetic-only", region="ore"
    )

    assert called == [(US_HOST, "/v3/user/login")]
    assert tokens.access_token == "synthetic-access"
    assert tokens.region == "ore"


def test_us_refresh_does_not_hit_legacy_90060_host(monkeypatch):
    called = []

    def fake_post(path, params, host=passport.DEFAULT_HOST, timeout=20):
        called.append((host, path))
        if host != US_HOST:
            return {"resultCode": "90060", "resultDesc": "App version too low"}
        return {
            "resultCode": "90000",
            "data": {
                "access_token": "synthetic-new-access",
                "refresh_token": "synthetic-new-refresh",
            },
        }

    monkeypatch.setattr(passport, "_post", fake_post)
    old = passport.Tokens(
        access_token="synthetic-old-access",
        refresh_token="synthetic-old-refresh",
        uuid="synthetic-uuid",
        region="ore",
    )
    updated = passport.refresh(old)

    assert called == [(US_HOST, "/v3/user/refresh")]
    assert updated.access_token == "synthetic-new-access"
    assert updated.region == "ore"
    assert updated.uuid == "synthetic-uuid"
