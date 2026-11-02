from datetime import datetime, timedelta

import pyotp
import pytest

from data import indmoney_auth as auth
from data.indmoney_client import IndMoneyClient

IST = auth.IST


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = status < 400
        self.headers = {"content-type": "application/json"}
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture
def totp_env(monkeypatch):
    monkeypatch.setattr(auth, "load_env", lambda *a: None)
    monkeypatch.setenv("INDMONEY_CLIENT_ID", "client-1")
    monkeypatch.setenv("INDMONEY_MPIN", "0000")
    monkeypatch.setenv("INDMONEY_TOTP_SECRET", pyotp.random_base32())


def test_token_expiry_is_7am_or_24h():
    evening = datetime(2026, 11, 2, 20, 0, tzinfo=IST)
    assert auth.token_expiry(evening) == datetime(2026, 11, 3, 7, 0, tzinfo=IST)
    early = datetime(2026, 11, 2, 6, 0, tzinfo=IST)
    assert auth.token_expiry(early) == datetime(2026, 11, 2, 7, 0, tzinfo=IST)


def test_generates_once_then_uses_cache(totp_env, monkeypatch, tmp_path):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append((headers, json))
        return FakeResp(200, {"success": True, "data": {"token": "tok-abc"}})

    monkeypatch.setattr(auth.requests, "post", fake_post)
    cache = tmp_path / "t.json"
    assert auth.get_token(cache_path=cache).get() == "tok-abc"
    assert auth.get_token(cache_path=cache).get() == "tok-abc"
    assert len(calls) == 1
    headers, body = calls[0]
    assert headers["x-api-key"] == "client-1" and body["mpin"] == "0000" and len(body["totp"]) == 6


def test_expired_cache_regenerates(totp_env, monkeypatch, tmp_path):
    cache = tmp_path / "t.json"
    auth._write_cache(cache, "old", datetime.now(IST) - timedelta(minutes=1))
    monkeypatch.setattr(auth.requests, "post",
                        lambda *a, **k: FakeResp(200, {"token": "new"}))
    assert auth.get_token(cache_path=cache).get() == "new"


def test_generation_failure_raises_without_retry(totp_env, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(auth.requests, "post",
                        lambda *a, **k: calls.append(1) or FakeResp(401, {"message": "invalid totp"}))
    with pytest.raises(auth.AuthError, match="invalid totp"):
        auth.get_token(cache_path=tmp_path / "t.json")
    assert len(calls) == 1


class FakeProvider:
    """Returns the current token; force=True rotates to a new one (like TOTP regeneration)."""

    def __init__(self):
        self.n, self.forced = 1, 0

    def __call__(self, force):
        if force:
            self.n += 1
            self.forced += 1
        return auth.Secret(f"tok{self.n}")


def test_client_refreshes_token_once_on_401(monkeypatch):
    provider = FakeProvider()
    client = IndMoneyClient(provider, requests_per_second=1000)
    seen = []

    def fake_get(url, params, timeout):
        seen.append(client._session.headers["Authorization"])
        return FakeResp(401, {}) if len(seen) == 1 else FakeResp(200, {"success": True, "data": {"ok": 1}})

    monkeypatch.setattr(client._session, "get", fake_get)
    assert client._json("/user/profile") == {"ok": 1}
    assert provider.forced == 1 and seen == ["tok1", "tok2"]


def test_client_gives_up_after_one_refresh_per_request(monkeypatch):
    provider = FakeProvider()
    client = IndMoneyClient(provider, requests_per_second=1000)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: FakeResp(401, {}))
    with pytest.raises(Exception, match="even after refreshing"):
        client._json("/user/profile")
    assert provider.forced == 1


def test_client_picks_up_rotated_token_on_later_requests(monkeypatch):
    provider = FakeProvider()
    client = IndMoneyClient(provider, requests_per_second=1000)
    seen = []
    monkeypatch.setattr(client._session, "get",
                        lambda *a, **k: seen.append(client._session.headers["Authorization"])
                        or FakeResp(200, {"data": {}}))
    client._json("/a")
    provider.n = 7  # e.g. the cached token was regenerated after 7 AM by another process
    client._json("/b")
    assert seen == ["tok1", "tok7"]
