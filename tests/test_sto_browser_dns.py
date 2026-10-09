"""Offline regressions for SerienStream's separate browser DNS and lifecycle."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from h0melab.models.common import common
from h0melab.models.s_to import http
from h0melab.playwright import captcha, dns


@pytest.fixture(autouse=True)
def reset_browser_dns(monkeypatch):
    monkeypatch.setattr(dns, "_cache", {})
    monkeypatch.setattr(dns, "_host_locks", {})
    monkeypatch.setattr(http, "_active_idx", 0)


def answer(address="186.2.163.190", ttl=120):
    return {
        "Status": 0,
        "Question": [{"name": "serienstream.to.", "type": 1}],
        "Answer": [
            {"name": "serienstream.to.", "type": 1, "data": address, "TTL": ttl}
        ],
    }


def stub_response(monkeypatch, payload):
    response = Mock()
    response.json.return_value = payload
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = response
    constructor = Mock(return_value=session)
    monkeypatch.setattr(dns, "Session", constructor)
    return constructor, session, response


def test_doh_uses_literal_tls_endpoint_without_app_state(monkeypatch):
    from h0melab.config import CA_CERT_BUNDLE

    constructor, session, response = stub_response(monkeypatch, answer())
    assert dns._query_doh("serienstream.to", dns._DOH_ENDPOINTS[0][1]) == (
        "186.2.163.190",
        120,
    )
    constructor.assert_called_once_with(
        retries=0, disable_http3=True, multiplexed=False
    )
    assert session.trust_env is False
    assert session.verify == CA_CERT_BUNDLE
    session.get.assert_called_once_with(
        "https://1.1.1.1/dns-query",
        params={"name": "serienstream.to", "type": "A"},
        headers={"Accept": "application/dns-json"},
        timeout=(2, 3),
        allow_redirects=False,
    )
    response.raise_for_status.assert_called_once()
    session.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"Status": 3},
        dict(answer(), TC=True),
        dict(answer(), Question=[]),
        dict(answer(), Answer=[]),
        dict(answer(), Question=[{"name": "wrong.example", "type": 1}]),
        dict(
            answer(),
            Answer=[
                {
                    "name": "wrong.example",
                    "type": 1,
                    "data": "186.2.163.190",
                    "TTL": 120,
                }
            ],
        ),
    ],
)
def test_doh_rejects_incomplete_or_unrelated_answers(monkeypatch, payload):
    stub_response(monkeypatch, payload)
    with pytest.raises(ValueError):
        dns._query_doh("serienstream.to", dns._DOH_ENDPOINTS[0][1])


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "bad,MAP * 127.0.0.1",
    ],
)
def test_doh_rejects_non_public_or_invalid_ipv4(monkeypatch, address):
    stub_response(monkeypatch, answer(address))
    with pytest.raises(ValueError, match="public IPv4"):
        dns._query_doh("serienstream.to", dns._DOH_ENDPOINTS[0][1])


def test_cname_chain_limits_cache_to_shortest_ttl(monkeypatch):
    payload = answer(ttl=900)
    payload["Answer"][0]["name"] = "target.example."
    payload["Answer"].extend(
        [
            {
                "name": "middle.example.",
                "type": 5,
                "data": "target.example.",
                "TTL": 80,
            },
            {
                "name": "serienstream.to.",
                "type": 5,
                "data": "middle.example.",
                "TTL": 40,
            },
        ]
    )
    stub_response(monkeypatch, payload)
    assert dns._query_doh("serienstream.to", dns._DOH_ENDPOINTS[0][1]) == (
        "186.2.163.190",
        40,
    )


@pytest.mark.parametrize("ttl,expected", [(900, 300), (0, 0), (-1, 0)])
def test_ttl_is_bounded(monkeypatch, ttl, expected):
    stub_response(monkeypatch, answer(ttl=ttl))
    assert dns._query_doh("serienstream.to", dns._DOH_ENDPOINTS[0][1])[1] == expected


def test_resolver_fallback_and_exact_host_whitelist(monkeypatch):
    query = Mock(side_effect=[ValueError("unavailable"), ("186.2.163.190", 120)])
    monkeypatch.setattr(dns, "_query_doh", query)
    assert dns.browser_dns_args(
        [
            "SERIENSTREAM.TO.",
            "serienstream.to",
            "voe.sx",
            "*",
            "serienstream.to,MAP * x",
        ]
    ) == ["--host-resolver-rules=MAP serienstream.to 186.2.163.190"]
    assert query.call_args_list == [
        (("serienstream.to", endpoint),) for _, endpoint in dns._DOH_ENDPOINTS
    ]


def test_failure_keeps_normal_dns_and_is_not_cached(monkeypatch):
    query = Mock(side_effect=ValueError("no answer"))
    monkeypatch.setattr(dns, "_query_doh", query)
    assert dns.browser_dns_args(["serienstream.to"]) == []
    assert dns.browser_dns_args(["serienstream.to"]) == []
    assert query.call_count == 4
    assert dns._cache == {}


def test_expiry_and_navigation_failure_invalidation(monkeypatch):
    clock = [100]
    monkeypatch.setattr(dns.time, "monotonic", lambda: clock[0])
    query = Mock(return_value=("186.2.163.190", 10))
    monkeypatch.setattr(dns, "_query_doh", query)
    hosts = ["serienstream.to"]
    dns.browser_dns_args(hosts)
    clock[0] = 109
    dns.browser_dns_args(hosts)
    assert query.call_count == 1
    clock[0] = 110
    dns.browser_dns_args(hosts)
    assert query.call_count == 2
    dns.clear_browser_dns_cache("SERIENSTREAM.TO.")
    dns.browser_dns_args(hosts)
    assert query.call_count == 3
    dns.clear_browser_dns_cache()
    assert dns._cache == {}


def test_opt_out_does_not_query_dns(monkeypatch):
    monkeypatch.setenv("H0MELAB_STO_BROWSER_DNS", "0")
    query = Mock()
    monkeypatch.setattr(dns, "_query_doh", query)
    assert dns.browser_dns_args(["serienstream.to"]) == []
    query.assert_not_called()


def test_concurrent_lookups_share_one_result(monkeypatch):
    entered, release = Event(), Event()

    def query(*_args):
        entered.set()
        assert release.wait(5)
        return "186.2.163.190", 120

    fetch = Mock(side_effect=query)
    monkeypatch.setattr(dns, "_query_doh", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dns.browser_dns_args, ["serienstream.to"])
        assert entered.wait(5)
        second = pool.submit(dns.browser_dns_args, ["serienstream.to"])
        release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert fetch.call_count == 1


@pytest.mark.parametrize("control", [common.DownloadPaused, common.DownloadCancelled])
def test_waiting_dns_worker_remains_cancellable(monkeypatch, control):
    lock = Lock()
    lock.acquire()
    monkeypatch.setitem(dns._host_locks, "serienstream.to", lock)
    query = Mock()
    monkeypatch.setattr(dns, "_query_doh", query)
    try:
        with pytest.raises(control):
            dns.browser_dns_args(
                ["serienstream.to"], check_control=Mock(side_effect=control("stopped"))
            )
        assert lock.locked()  # a waiter must not release the owning worker's lock
        query.assert_not_called()
    finally:
        lock.release()


@pytest.fixture
def browser_runtime(monkeypatch, tmp_path):
    runtime = MagicMock()
    executable = tmp_path / "chromium"
    executable.write_bytes(b"fixture")
    runtime.chromium.executable_path = str(executable)
    monkeypatch.setattr(captcha, "_PROFILE_LOCK", Lock())
    monkeypatch.setattr(
        captcha, "_resolve_profile_dir", lambda: str(tmp_path / "profile")
    )
    monkeypatch.setattr(captcha, "_install_stealth", Mock())
    return runtime


@pytest.mark.parametrize("persistent", [True, False])
def test_dns_args_reach_both_browser_launch_modes(
    monkeypatch, browser_runtime, persistent
):
    monkeypatch.setattr(captcha, "_persistent_profile_enabled", lambda: persistent)
    mapping = "--host-resolver-rules=MAP serienstream.to 186.2.163.190"
    resolve = Mock(return_value=[mapping])
    monkeypatch.setattr(dns, "browser_dns_args", resolve)
    handle = captcha._launch_browser_context(
        browser_runtime, dns_hosts=("serienstream.to",)
    )
    try:
        launch = (
            browser_runtime.chromium.launch_persistent_context
            if persistent
            else browser_runtime.chromium.launch
        )
        assert mapping in launch.call_args.kwargs["args"]
        resolve.assert_called_once_with(
            ("serienstream.to",), check_control=common._raise_for_queue_control
        )
    finally:
        handle.close()
    assert not captcha._PROFILE_LOCK.locked()


def test_unrelated_browser_does_not_change_dns(monkeypatch, browser_runtime):
    monkeypatch.setattr(captcha, "_persistent_profile_enabled", lambda: False)
    resolve = Mock()
    monkeypatch.setattr(dns, "browser_dns_args", resolve)
    handle = captcha._launch_browser_context(browser_runtime)
    handle.close()
    resolve.assert_not_called()


@pytest.mark.parametrize("persistent", [True, False])
def test_launch_setup_failure_closes_context_and_releases_profile(
    monkeypatch, browser_runtime, persistent
):
    monkeypatch.setattr(captcha, "_persistent_profile_enabled", lambda: persistent)
    monkeypatch.setattr(
        captcha, "_install_stealth", Mock(side_effect=ValueError("setup"))
    )
    with pytest.raises(ValueError, match="setup"):
        captcha._launch_browser_context(browser_runtime)
    context = (
        browser_runtime.chromium.launch_persistent_context.return_value
        if persistent
        else browser_runtime.chromium.launch.return_value.new_context.return_value
    )
    context.close.assert_called_once()
    assert not captcha._PROFILE_LOCK.locked()
    if not persistent:
        browser_runtime.chromium.launch.return_value.close.assert_called_once()


def test_new_context_failure_closes_launched_browser(monkeypatch, browser_runtime):
    monkeypatch.setattr(captcha, "_persistent_profile_enabled", lambda: False)
    browser_runtime.chromium.launch.return_value.new_context.side_effect = RuntimeError(
        "new"
    )
    with pytest.raises(RuntimeError, match="new"):
        captcha._launch_browser_context(browser_runtime)
    browser_runtime.chromium.launch.return_value.close.assert_called_once()


def test_handle_close_does_not_release_a_new_owners_lock(monkeypatch):
    lock = Lock()
    monkeypatch.setattr(captcha, "_PROFILE_LOCK", lock)
    lock.acquire()
    handle = captcha._BrowserHandle(Mock(), None, True)
    handle.close()
    assert lock.acquire(blocking=False)
    try:
        handle.close()
        assert lock.locked()
    finally:
        lock.release()


@pytest.fixture
def modal(monkeypatch):
    from patchright import sync_api

    from h0melab import autodeps

    handle = Mock()
    page = handle.context.new_page.return_value
    page.url = "about:blank"
    page.frames = []
    page.evaluate.return_value = True
    page.goto.side_effect = lambda url, **_kwargs: setattr(page, "url", url)
    monkeypatch.setattr(sync_api, "sync_playwright", MagicMock())
    monkeypatch.setattr(autodeps, "_ensure_xvfb", Mock())
    launch = Mock(return_value=handle)
    monkeypatch.setattr(captcha, "_launch_browser_context", launch)
    for name in (
        "_attach_debug_listeners",
        "_focus_page",
        "_sync_session_user_agent",
        "_remove_ad_overlays",
        "_inject_session_cookies",
        "_export_session_cookies",
    ):
        monkeypatch.setattr(captcha, name, Mock())
    monkeypatch.setattr(captcha, "_click_submit_button", Mock(return_value=True))
    monkeypatch.setattr(captcha._local, "queue_id", None, raising=False)
    monkeypatch.setattr(captcha, "_captcha_timeout", lambda _default: 2)
    clock = [0]
    monkeypatch.setattr(
        captcha,
        "_time",
        SimpleNamespace(
            time=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    return handle, page, launch


@pytest.mark.parametrize("source", ["named", "other", "main", "popup"])
def test_modal_never_returns_another_source_mirror(modal, source):
    handle, page, _ = modal
    url = "https://serienstream.cx/r?t=token"
    if source in ("named", "other"):
        page.frames = [
            SimpleNamespace(
                name="player-iframe" if source == "named" else "other", url=url
            )
        ]
    elif source == "main":
        page.goto.side_effect = lambda *_a, **_kw: setattr(page, "url", url)
    else:

        def click(*_args):
            callback = handle.context.on.call_args.args[1]
            callback(Mock(url=url))
            return True

        page.evaluate.side_effect = click
    assert (
        captcha.solve_sto_modal(
            "https://serienstream.to/serie/example/staffel-1/episode-1",
            "VOE",
            "Deutsch",
        )
        is None
    )
    handle.close.assert_called_once()
    captcha._export_session_cookies.assert_not_called()


def test_modal_accepts_provider_alias_and_selected_language_without_redirect(modal):
    handle, page, launch = modal
    page.frames = [
        SimpleNamespace(name="player-iframe", url="https://provider-alias.example/e/id")
    ]
    assert (
        captcha.solve_sto_modal(
            "https://serienstream.to/serie/example/staffel-1/episode-1",
            "VOE",
            "Englisch",
        )
        == "https://provider-alias.example/e/id"
    )
    assert page.evaluate.call_args.args[1] == {
        "playPath": None,
        "provider": "VOE",
        "language": "Englisch",
    }
    assert launch.call_args.kwargs["dns_hosts"] == tuple(http.STO_DOMAINS)
    handle.close.assert_called_once()
    captcha._export_session_cookies.assert_called_once_with(handle.context)


@pytest.mark.parametrize("control", [common.DownloadPaused, common.DownloadCancelled])
def test_modal_control_cleans_browser_and_interactive_state(
    monkeypatch, modal, control
):
    handle, _, _ = modal
    monkeypatch.setattr(captcha._local, "queue_id", 12345)
    begin, end = Mock(), Mock()
    monkeypatch.setattr(captcha, "_on_captcha_start", begin)
    monkeypatch.setattr(captcha, "_on_captcha_end", end)
    monkeypatch.setattr(
        common, "_raise_for_queue_control", Mock(side_effect=control("stopped"))
    )
    with pytest.raises(control):
        captcha.solve_sto_modal(
            "https://serienstream.to/serie/example", "VOE", "Deutsch"
        )
    handle.close.assert_called_once()
    assert 12345 not in captcha._active_sessions
    assert captcha._captcha_state is None
    begin.assert_called_once()
    end.assert_called_once_with(12345)


def test_modal_navigation_error_preserves_both_domains_and_cleans_browser(modal):
    handle, page, _ = modal
    page.goto.side_effect = RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED")
    with pytest.raises(http.SerienstreamNavigationError) as error:
        captcha.solve_sto_modal(
            "https://serienstream.to/serie/example", "VOE", "Deutsch"
        )
    assert error.value.failures == (
        ("serienstream.to", "ERR_NAME_NOT_RESOLVED"),
        ("serienstream.cx", "ERR_NAME_NOT_RESOLVED"),
    )
    assert "CAPTCHA" not in str(error.value)
    handle.close.assert_called_once()
    assert captcha._captcha_state is None


@pytest.mark.parametrize("terminal", [True, False])
def test_navigation_retry_classification_keeps_other_provider_fallback(
    monkeypatch, tmp_path, terminal
):
    calls = []

    class Episode:
        url = "https://aniworld.to/anime/stream/example/staffel-1/episode-1"
        selected_language = "German Dub"
        selected_subtitle_language = "none"
        _base_folder = tmp_path
        _folder_path = tmp_path
        _episode_path = tmp_path / "example.mkv"
        _file_name = "Example"
        _selected_provider = "VOE"

        @property
        def selected_provider(self):
            return self._selected_provider

        @selected_provider.setter
        def selected_provider(self, value):
            self._selected_provider = value

        @property
        def stream_url(self):
            calls.append(self.selected_provider)
            if self.selected_provider == "VOE":
                if terminal:
                    raise http.SerienstreamNavigationError(
                        [("serienstream.to", "ERR_NAME_NOT_RESOLVED")]
                    )
                raise ValueError("temporary provider failure")
            return "https://cdn.example/master.m3u8"

    monkeypatch.setattr(common.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        common, "_get_provider_attempt_order", lambda _: ("VOE", "Vidoza")
    )
    monkeypatch.setattr(common, "_prepare_resolution_naming", Mock())
    monkeypatch.setattr(common, "_cleanup_episode_download", Mock())
    monkeypatch.setattr(
        common,
        "check_downloaded",
        lambda _: {"exists": True, "audio_langs": {"deu"}, "video_langs": {"und"}},
    )
    common.download(Episode())
    assert calls == ["VOE"] * (1 if terminal else 3) + ["Vidoza"]


def test_mirror_cookie_injection_and_redirect_ignore_changed_global_preference(
    monkeypatch,
):
    page, context, logger = Mock(), Mock(), Mock()
    page.url = "https://serienstream.to/serie/example"
    monkeypatch.setattr(http, "_active_idx", 1)
    inject = Mock()
    monkeypatch.setattr(captcha, "_inject_session_cookies", inject)
    dns._cache["serienstream.cx"] = (99999999999, "186.2.163.190")

    def navigate(url, **_kwargs):
        http._active_idx = 0  # another HTTP worker changes the preference mid-solve
        if "serienstream.cx" in url:
            raise RuntimeError("net::ERR_CONNECTION_RESET at /r?t=secret")

    page.goto.side_effect = navigate
    opened, redirect = captcha._open_sto_episode_with_fallback(
        page,
        "https://serienstream.to/serie/example?token=secret",
        "https://serienstream.cx/r?t=signed%2Btoken",
        logger,
        context=context,
    )
    assert opened == page.url
    assert redirect == "https://serienstream.to/r?t=signed%2Btoken"
    assert inject.call_args_list == [
        ((context, "https://serienstream.cx/serie/example?token=secret"),),
        ((context, "https://serienstream.to/serie/example?token=secret"),),
    ]
    assert "serienstream.cx" not in dns._cache
    assert "secret" not in str(logger.mock_calls)


def test_non_network_navigation_error_does_not_try_other_mirror():
    page = Mock()
    page.goto.side_effect = ValueError("application failure")
    with pytest.raises(ValueError, match="application failure"):
        captcha._open_sto_episode_with_fallback(
            page, "https://serienstream.to/serie/example", None, Mock()
        )
    page.goto.assert_called_once()


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "about:blank",
        "data:text/html,example",
        "javascript:alert(1)",
        "/r?t=token",
        "https://user@voe.sx/e/id",
        "https://[invalid/",
        "https:///path",
        "https://challenges.cloudflare.com/turnstile/widget",
        "https://hcaptcha.com/widget",
        "https://www.serienstream.cx/",
        "https://S.TO/r?t=token",
    ],
)
def test_result_url_validation_rejects_source_and_non_provider_urls(url):
    assert not captcha._is_provider_result_url(url)
