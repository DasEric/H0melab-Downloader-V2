"""The panel's DNS switch: what is routed where, and that a change is immediate."""

import socket

import pytest

from h0melab import net_dns
from h0melab.web import settings_store
from h0melab.web.settings_store import SettingsError, update_settings

PUBLIC = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 443))
PUBLIC_V6 = (
    socket.AF_INET6,
    socket.SOCK_STREAM,
    6,
    b"extra",
    ("2001:db8::7", 443, 0, 0),
)
SYSTEM = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.1", 443))


class FakeResolver:
    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    def getaddrinfo(self, host, port, family, type, proto, flags):
        self.calls.append(host)
        if self.error is not None:
            raise self.error
        return list(self.answer)

    def is_available(self):
        return True

    def close(self):
        pass


@pytest.fixture
def routing(monkeypatch):
    """Fake per-server resolvers and a fake system resolver."""
    servers = {}
    system_calls = []

    def system(host, port, family=0, type=0, proto=0, flags=0):
        system_calls.append(host)
        return [SYSTEM]

    monkeypatch.setattr(net_dns, "_system_getaddrinfo", system)
    monkeypatch.setattr(net_dns, "_server_resolver", lambda server: servers[server])
    monkeypatch.setattr(net_dns, "_drop_resolver", lambda server: None)
    return servers, system_calls


def test_default_is_the_system_resolver(monkeypatch, routing):
    monkeypatch.delenv(net_dns.DNS_KEY, raising=False)
    _servers, system_calls = routing
    assert net_dns.dns_mode() == "system"
    assert net_dns.doh_url() is None
    assert net_dns.getaddrinfo("moflix-stream.xyz", 443) == [SYSTEM]
    assert system_calls == ["moflix-stream.xyz"]


def test_unknown_value_falls_back_to_system(monkeypatch):
    monkeypatch.setenv(net_dns.DNS_KEY, "quad9")
    assert net_dns.dns_mode() == "system"


def test_cloudflare_answers_public_names_ipv4_first(monkeypatch, routing):
    servers, system_calls = routing
    servers["1.1.1.1"] = FakeResolver([PUBLIC_V6, PUBLIC])
    monkeypatch.setenv(net_dns.DNS_KEY, "cloudflare")

    result = net_dns.getaddrinfo("Moflix-Stream.xyz.", 443, 0, socket.SOCK_STREAM)

    assert result[0] == PUBLIC
    assert result[1][3] == "", "extra record data never lands in canonname"
    assert servers["1.1.1.1"].calls == ["moflix-stream.xyz"]
    assert system_calls == []


def test_second_server_and_then_system_take_over(monkeypatch, routing):
    servers, system_calls = routing
    servers["8.8.8.8"] = FakeResolver(error=TimeoutError())
    servers["8.8.4.4"] = FakeResolver(error=socket.gaierror("no answer"))
    monkeypatch.setenv(net_dns.DNS_KEY, "google")

    assert net_dns.getaddrinfo("example.org", 443) == [SYSTEM]
    assert servers["8.8.8.8"].calls == ["example.org"]
    assert servers["8.8.4.4"].calls == ["example.org"]
    assert system_calls == ["example.org"]


@pytest.mark.parametrize(
    "host",
    [
        "jellyfin",
        "localhost",
        "nas.local",
        "router.lan",
        "10.0.0.1",
        "::1",
        "fe80::1%eth0",
    ],
)
def test_local_names_never_go_to_public_servers(monkeypatch, routing, host):
    servers, system_calls = routing
    servers["1.1.1.1"] = FakeResolver([PUBLIC])
    monkeypatch.setenv(net_dns.DNS_KEY, "cloudflare")

    assert net_dns.getaddrinfo(host, 80) == [SYSTEM]
    assert servers["1.1.1.1"].calls == []
    assert system_calls == [host]


def test_requested_family_is_respected(monkeypatch, routing):
    servers, _system_calls = routing
    servers["1.1.1.1"] = FakeResolver([PUBLIC_V6, PUBLIC])
    monkeypatch.setenv(net_dns.DNS_KEY, "cloudflare")

    result = net_dns.getaddrinfo("example.org", 443, socket.AF_INET6)
    assert [entry[0] for entry in result] == [socket.AF_INET6]


def test_switch_is_immediate_without_restart(monkeypatch, routing):
    servers, system_calls = routing
    servers["1.1.1.1"] = FakeResolver([PUBLIC])
    servers["8.8.8.8"] = FakeResolver([PUBLIC])
    monkeypatch.setattr(settings_store, "_persist_settings", lambda updates: None)

    update_settings({"dns_mode": "cloudflare"})
    net_dns.getaddrinfo("example.org", 443)
    update_settings({"dns_mode": "google"})
    net_dns.getaddrinfo("example.org", 443)
    update_settings({"dns_mode": "system"})
    net_dns.getaddrinfo("example.org", 443)

    assert servers["1.1.1.1"].calls == ["example.org"]
    assert servers["8.8.8.8"].calls == ["example.org"]
    assert system_calls == ["example.org"]


def test_settings_expose_validate_and_export_the_choice(monkeypatch):
    monkeypatch.setattr(settings_store, "_persist_settings", lambda updates: None)
    settings = settings_store.read_settings()
    assert settings["dns_mode"] == "system"
    assert [m["key"] for m in settings["available_dns_modes"]] == [
        "system",
        "cloudflare",
        "google",
    ]
    assert settings["available_dns_modes"][1]["servers"] == ["1.1.1.1", "1.0.0.1"]
    assert settings["available_dns_modes"][2]["servers"] == ["8.8.8.8", "8.8.4.4"]

    with pytest.raises(SettingsError):
        update_settings({"dns_mode": "opendns"})

    update_settings({"dns_mode": "Google"})
    assert net_dns.dns_mode() == "google"
    assert "H0MELAB_DNS_SERVERS=google" in settings_store.export_env()


def test_curl_cffi_requests_get_the_providers_doh(monkeypatch):
    from curl_cffi import CurlOpt
    from curl_cffi.requests import session as curl_session

    calls = []

    def original(curl, method, url, **kwargs):
        return "prepared"

    original_name = curl_session.set_curl_options
    monkeypatch.setattr(curl_session, "set_curl_options", original)
    net_dns._install_curl_cffi()
    patched = curl_session.set_curl_options
    try:

        class Curl:
            def setopt(self, option, value):
                calls.append((option, value))

        monkeypatch.setenv(net_dns.DNS_KEY, "system")
        assert patched(Curl(), "GET", "https://example.org") == "prepared"
        assert calls == []

        monkeypatch.setenv(net_dns.DNS_KEY, "cloudflare")
        patched(Curl(), "GET", "https://example.org")
        assert calls == [(CurlOpt.DOH_URL, b"https://1.1.1.1/dns-query")]

        calls.clear()
        patched(Curl(), "GET", "https://example.org", doh_url="https://own/dns")
        assert calls == [], "an explicit per-request DoH URL wins"
    finally:
        curl_session.set_curl_options = original_name
