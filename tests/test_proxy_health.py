"""Proxy egress health helper.

check_proxy() probes SPIDER_PROXY_URL via an IP-echo endpoint and
reports the egress IP + geo. No live network — a fake aiohttp session
captures the request and returns canned payloads.
"""

from __future__ import annotations

from typing import Any, Self

import pytest

from spider import proxy_health


class _FakeResp:
    def __init__(
        self,
        *,
        status: int = 200,
        payload: Any = None,
        exc: Exception | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self._exc = exc

    async def __aenter__(self) -> Self:
        if self._exc is not None:
            raise self._exc
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def json(self, content_type: str | None = None) -> Any:
        return self._payload


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession.get()."""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append((url, kwargs))
        return self._resp


_SUCCESS = {
    "status": "success",
    "query": "84.17.52.9",
    "country": "Germany",
    "countryCode": "DE",
    "timezone": "Europe/Berlin",
    "isp": "Vodafone GmbH",
}


async def test_no_proxy_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    session = _FakeSession(_FakeResp(payload=_SUCCESS))
    health = await proxy_health.check_proxy(session)  # type: ignore[arg-type]
    assert health.configured is False
    assert health.reachable is False
    # No request should be made when there is nothing to check.
    assert session.calls == []


async def test_reachable_reports_egress_geo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "socks5://127.0.0.1:1080")
    session = _FakeSession(_FakeResp(payload=_SUCCESS))
    health = await proxy_health.check_proxy(session)  # type: ignore[arg-type]
    assert health.configured is True
    assert health.reachable is True
    assert health.egress_ip == "84.17.52.9"
    assert health.country_code == "DE"
    assert health.timezone == "Europe/Berlin"
    assert health.isp == "Vodafone GmbH"
    assert isinstance(health.ms, int)
    assert health.error is None
    # The actual proxy was passed through to aiohttp.
    _url, kwargs = session.calls[0]
    assert kwargs["proxy"] == "socks5://127.0.0.1:1080"


async def test_credentials_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://user:s3cret@phone-proxy:8888")
    session = _FakeSession(_FakeResp(payload=_SUCCESS))
    health = await proxy_health.check_proxy(session)  # type: ignore[arg-type]
    # Surfaced URL has no credentials...
    assert health.proxy_url == "http://phone-proxy:8888"
    assert "s3cret" not in (health.proxy_url or "")
    # ...but the real (credentialed) proxy was used for the request.
    _url, kwargs = session.calls[0]
    assert kwargs["proxy"] == "http://user:s3cret@phone-proxy:8888"


async def test_per_call_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    session = _FakeSession(_FakeResp(payload=_SUCCESS))
    health = await proxy_health.check_proxy(session, "http://override:9090")  # type: ignore[arg-type]
    assert health.proxy_url == "http://override:9090"
    _url, kwargs = session.calls[0]
    assert kwargs["proxy"] == "http://override:9090"


async def test_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "socks5://127.0.0.1:1080")
    session = _FakeSession(_FakeResp(exc=OSError("connection refused")))
    health = await proxy_health.check_proxy(session)  # type: ignore[arg-type]
    assert health.configured is True
    assert health.reachable is False
    assert health.error is not None
    assert "OSError" in health.error
    assert isinstance(health.ms, int)


async def test_non_200_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "socks5://127.0.0.1:1080")
    session = _FakeSession(_FakeResp(status=502, payload=None))
    health = await proxy_health.check_proxy(session)  # type: ignore[arg-type]
    assert health.reachable is False
    assert health.error == "echo endpoint HTTP 502"


async def test_geo_lookup_fail_still_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "socks5://127.0.0.1:1080")
    payload = {"status": "fail", "message": "private range", "query": "10.0.0.4"}
    session = _FakeSession(_FakeResp(payload=payload))
    health = await proxy_health.check_proxy(session)  # type: ignore[arg-type]
    # The proxy answered, so it is reachable; geo just couldn't resolve.
    assert health.reachable is True
    assert health.egress_ip == "10.0.0.4"
    assert health.error is not None
    assert "geo lookup failed" in health.error


def test_redact_proxy_url() -> None:
    assert proxy_health.redact_proxy_url("http://user:pass@host:8888") == "http://host:8888"
    assert proxy_health.redact_proxy_url("socks5://host:1080") == "socks5://host:1080"
    assert proxy_health.redact_proxy_url("not-a-url") == "not-a-url"
