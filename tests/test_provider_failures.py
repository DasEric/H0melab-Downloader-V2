"""Regressions for missing Chromium and permanently deleted VOE links (#305)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from h0melab.extractors.provider import voe
from h0melab.models.s_to import episode as sto
from h0melab.playwright import captcha


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize("redirect", [False, True])
def test_deleted_voe_does_not_retry(monkeypatch, status, redirect):
    responses = [("deleted", "https://voe.sx/e/deleted", status)]
    if redirect:
        responses.insert(
            0,
            (
                'window.location="https://voe.sx/e/deleted"',
                "https://voe.sx/e/start",
                200,
            ),
        )
    fetch = Mock(side_effect=responses)
    sleep = Mock()
    monkeypatch.setattr(voe, "_voe_get", fetch)
    monkeypatch.setattr(voe.time, "sleep", sleep)
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        voe.get_direct_link_from_voe("https://voe.sx/e/start")
    assert fetch.call_count == len(responses)
    sleep.assert_not_called()


def test_temporary_voe_failure_still_retries(monkeypatch):
    fetch = Mock(
        side_effect=[
            ("error", "https://voe.sx/e/start", 500),
            ("'hls': 'https://cdn.example/video.m3u8'", "https://voe.sx/e/start", 200),
        ]
    )
    sleep = Mock()
    monkeypatch.setattr(voe, "_voe_get", fetch)
    monkeypatch.setattr(voe.time, "sleep", sleep)
    assert (
        voe.get_direct_link_from_voe("https://voe.sx/e/start")
        == "https://cdn.example/video.m3u8"
    )
    sleep.assert_called_once_with(2)


def test_missing_chromium_error_survives_modal_solver(monkeypatch, tmp_path):
    from patchright import sync_api

    from h0melab import autodeps

    runtime = MagicMock()
    runtime.chromium.executable_path = str(tmp_path / "missing-chromium")
    playwright = MagicMock()
    playwright.return_value.__enter__.return_value = runtime
    monkeypatch.setattr(sync_api, "sync_playwright", playwright)
    monkeypatch.setattr(autodeps, "_ensure_xvfb", Mock())
    with pytest.raises(RuntimeError, match="python -m patchright install chromium"):
        captcha.solve_sto_modal(
            "https://serienstream.to/serie/example/staffel-1/episode-1",
            "VOE",
            "Deutsch",
        )
    runtime.chromium.launch.assert_not_called()
    runtime.chromium.launch_persistent_context.assert_not_called()


def test_failed_modal_does_not_cache_serienstream_url(monkeypatch):
    episode = sto.SerienstreamEpisode(
        "https://serienstream.to/serie/example/staffel-1/episode-1"
    )
    redirect = "https://serienstream.to/r?t=example"
    monkeypatch.setattr(sto.SerienstreamEpisode, "provider_link", lambda *a: redirect)
    monkeypatch.setattr(sto, "sto_get", lambda *a: SimpleNamespace(url=redirect))
    solve = Mock(return_value=None)
    monkeypatch.setattr(captcha, "solve_sto_modal", solve)
    for _ in range(2):
        with pytest.raises(ValueError, match="Failed to resolve provider URL"):
            _ = episode.stream_url
    assert solve.call_count == 2


def test_modal_navigation_falls_back_after_dns_failure(monkeypatch):
    from h0melab.models.s_to import http

    monkeypatch.setattr(http, "_active_idx", 0)

    class Page:
        def __init__(self):
            self.calls = []

        def goto(self, url, **_kwargs):
            self.calls.append(url)
            if "serienstream.to" in url:
                raise RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED")

    page = Page()
    redirect = "https://serienstream.to/r?t=signed-token"
    opened, rewritten_redirect = captcha._open_sto_episode_with_fallback(
        page,
        "https://serienstream.to/serie/example/staffel-1/episode-1",
        redirect,
        Mock(),
    )

    assert page.calls == [
        "https://serienstream.to/serie/example/staffel-1/episode-1",
        "https://serienstream.cx/serie/example/staffel-1/episode-1",
    ]
    assert opened.startswith("https://serienstream.cx/")
    assert rewritten_redirect == "https://serienstream.cx/r?t=signed-token"


@pytest.mark.parametrize(
    "final_url",
    [
        "https://serienstream.cx/r?t=token",
        "https://www.serienstream.to/r?t=token",
        "https://186.2.175.5/r?t=token",
        "https://s.to/r?t=token",
    ],
)
def test_http_mirror_switch_is_not_a_provider_result(monkeypatch, final_url):
    episode = sto.SerienstreamEpisode(
        "https://serienstream.to/serie/example/staffel-1/episode-1"
    )
    monkeypatch.setattr(
        sto.SerienstreamEpisode,
        "provider_link",
        lambda *a: "https://serienstream.to/r?t=token",
    )
    monkeypatch.setattr(sto, "sto_get", lambda *a: SimpleNamespace(url=final_url))
    solve = Mock(return_value="https://voe.sx/e/example")
    monkeypatch.setattr(captcha, "solve_sto_modal", solve)

    assert episode.provider_url == "https://voe.sx/e/example"
    solve.assert_called_once()


def test_all_browser_mirrors_report_their_errors_without_tokens(monkeypatch):
    from h0melab.models.s_to import http

    monkeypatch.setattr(http, "_active_idx", 0)
    page = Mock()
    page.goto.side_effect = [
        RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED at /r?t=secret"),
        RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED at /r?t=secret"),
    ]
    with pytest.raises(RuntimeError) as error:
        captcha._open_sto_episode_with_fallback(
            page,
            "https://serienstream.to/serie/example/staffel-1/episode-1",
            "https://serienstream.to/r?t=secret",
            Mock(),
        )

    message = str(error.value)
    assert "serienstream.to" in message
    assert "ERR_CONNECTION_REFUSED" in message
    assert "serienstream.cx" in message
    assert "ERR_NAME_NOT_RESOLVED" in message
    assert "secret" not in message


def test_episode_navigation_uses_actual_redirected_mirror(monkeypatch):
    from h0melab.models.s_to import http

    monkeypatch.setattr(http, "_active_idx", 0)
    page = Mock()
    page.url = "https://serienstream.cx/serie/example/staffel-1/episode-1"
    opened, redirect = captcha._open_sto_episode_with_fallback(
        page,
        "https://serienstream.to/serie/example/staffel-1/episode-1",
        "https://serienstream.to/r?t=signed%2Btoken",
        Mock(),
    )

    assert opened == page.url
    assert redirect == "https://serienstream.cx/r?t=signed%2Btoken"


def test_network_filter_accepts_configured_ip_mirror():
    from h0melab.config import STO_IP

    assert captcha._ad_host_allowed(STO_IP, "serienstream.to")


@pytest.mark.parametrize(
    "result",
    [
        "https://serienstream.cx/r?t=token",
        "about:blank",
        "https://challenges.cloudflare.com/turnstile/widget",
    ],
)
def test_invalid_modal_result_is_not_cached(monkeypatch, result):
    episode = sto.SerienstreamEpisode(
        "https://serienstream.to/serie/example/staffel-1/episode-1"
    )
    redirect = "https://serienstream.to/r?t=secret"
    monkeypatch.setattr(sto.SerienstreamEpisode, "provider_link", lambda *a: redirect)
    monkeypatch.setattr(sto, "sto_get", lambda *a: SimpleNamespace(url=redirect))
    solve = Mock(return_value=result)
    monkeypatch.setattr(captcha, "solve_sto_modal", solve)
    for _ in range(2):
        with pytest.raises(ValueError, match="Failed to resolve provider URL") as error:
            _ = episode.provider_url
        assert "secret" not in str(error.value)
    assert solve.call_count == 2


def test_english_string_selection_reaches_correct_browser_button(monkeypatch):
    episode = sto.SerienstreamEpisode(
        "https://serienstream.to/serie/example/staffel-1/episode-1"
    )
    monkeypatch.setattr(
        sto.SerienstreamEpisode, "selected_language", property(lambda _: "English Dub")
    )
    redirect = "https://serienstream.to/r?t=token"
    monkeypatch.setattr(sto.SerienstreamEpisode, "provider_link", lambda *a: redirect)
    monkeypatch.setattr(sto, "sto_get", lambda *a: SimpleNamespace(url=redirect))
    solve = Mock(return_value="https://voe.sx/e/id")
    monkeypatch.setattr(captcha, "solve_sto_modal", solve)
    assert episode.provider_url == "https://voe.sx/e/id"
    assert solve.call_args.args[2] == "Englisch"
