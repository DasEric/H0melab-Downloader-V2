"""Public page loaders replace the blocked Moflix search and browse APIs."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from h0melab.models.moflix_stream import client as moflix


def response(*, text="", payload=None, status=200, headers=None):
    return SimpleNamespace(
        text=text,
        status_code=status,
        headers=headers or {},
        cookies={},
        json=Mock(return_value=payload),
        raise_for_status=Mock(),
        close=Mock(),
    )


def page(name, loader):
    data = {"csrf_token": "test", "loaders": {name: loader}}
    return response(
        text="<script>window.bootstrapData = "
        + json.dumps(data, indent=2)
        + ";</script>"
    )


def title(number=42, **extra):
    return {
        "id": number,
        "name": "Silo",
        "model_type": "title",
        "is_series": True,
        **extra,
    }


def test_sessions_are_reused_but_not_shared_across_threads(monkeypatch):
    monkeypatch.setattr(moflix, "_local", threading.local())
    factory = Mock(side_effect=lambda **kwargs: object())
    monkeypatch.setattr(moflix.requests, "Session", factory)
    barrier = threading.Barrier(2)

    def get_session():
        first = moflix._session()
        barrier.wait(timeout=10)
        assert moflix._session() is first
        return first

    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(lambda _: get_session(), range(2)))
    assert sessions[0] is not sessions[1]
    assert factory.call_count == 2


@pytest.fixture
def transport(monkeypatch):
    session = SimpleNamespace(get=Mock())
    monkeypatch.setattr(moflix, "_session", lambda: session)
    # A regression to the legacy top-level requests.get must never reach the network.
    monkeypatch.setattr(
        moflix.requests,
        "get",
        Mock(side_effect=AssertionError("legacy Moflix transport")),
    )
    return session.get


def test_bootstrap_supports_multiline_nested_json_and_embedded_braces():
    loader = {"results": [title(name='Nested }; "name"')], "query": "test"}
    assert (
        moflix.parse_bootstrap(page("searchPage", loader).text)["loaders"]["searchPage"]
        == loader
    )


@pytest.mark.parametrize(
    "text",
    ["<html></html>", "window.bootstrapData = {broken};", "window.bootstrapData = [];"],
)
def test_bootstrap_rejects_missing_or_invalid_data(text):
    with pytest.raises(moflix.MoflixResponseError, match="bootstrap"):
        moflix.parse_bootstrap(text)


def test_search_uses_public_page_and_normalises_titles(transport):
    from h0melab.search import query_moflix

    transport.return_value = page(
        "searchPage",
        {
            "query": "silo",
            "results": [
                title(poster="//images.example/silo.jpg", year=2023),
                title(),
                title(model_type="person"),
                title(None),
                title(True),
                title(0),
                title(name=" "),
                None,
            ],
        },
    )
    assert query_moflix("silo") == [
        {
            "title": "Silo",
            "url": moflix.BASE_URL + "/titles/42",
            "poster_url": "https://images.example/silo.jpg",
            "is_series": True,
            "year": 2023,
        }
    ]
    assert transport.call_args.args == (moflix.BASE_URL + "/search/silo",)


def test_search_encodes_reserved_characters_as_one_path_segment(transport):
    keyword = "Mädchen / A&B?#"
    transport.return_value = page("searchPage", {"query": keyword, "results": []})
    assert moflix.search(keyword) == []
    assert transport.call_args.args[0].endswith(
        "/search/M%C3%A4dchen%20%2F%20A%26B%3F%23"
    )


def test_empty_search_does_not_fetch(transport):
    assert moflix.search("  ") == []
    transport.assert_not_called()


def test_missing_poster_stays_empty_and_unsafe_scheme_is_removed(transport):
    transport.return_value = page(
        "searchPage",
        {
            "query": "silo",
            "results": [title(), title(43, poster="javascript:alert(1)")],
        },
    )
    assert [item["poster_url"] for item in moflix.search("silo")] == ["", ""]


@pytest.mark.parametrize(
    "loader",
    [
        {"query": "other", "results": []},
        {"query": "silo"},
        {"query": "silo", "results": {}},
    ],
)
def test_search_rejects_wrong_query_or_invalid_list(transport, loader):
    transport.return_value = page("searchPage", loader)
    with pytest.raises(moflix.MoflixResponseError):
        moflix.search("silo")


def test_browse_keeps_upstream_order_deduplicates_and_limits(transport):
    from h0melab.search import fetch_moflix_movies

    items = [title(9), title(2, is_series=False, poster="/poster.jpg"), title(9)]
    items += [title(i) for i in range(10, 40)]
    transport.return_value = page(
        "channelPage", {"channel": {"slug": "now-playing", "content": {"data": items}}}
    )
    cards = fetch_moflix_movies()
    assert len(cards) == 24
    assert [c["url"].rsplit("/", 1)[1] for c in cards[:3]] == ["9", "2", "10"]
    assert cards[1]["poster_url"] == moflix.BASE_URL + "/poster.jpg"
    assert cards[1]["is_series"] is False
    assert transport.call_args.args == (moflix.BASE_URL + "/now-playing",)


@pytest.mark.parametrize(
    "loader",
    [
        {},
        {"channel": {"slug": "movies", "content": {"data": []}}},
        {"channel": {"slug": "now-playing", "content": {}}},
    ],
)
def test_browse_rejects_changed_structure_or_wrong_channel(transport, loader):
    transport.return_value = page("channelPage", loader)
    with pytest.raises(moflix.MoflixResponseError):
        moflix.recent_titles()


@pytest.mark.parametrize("status", [200, 403])
def test_challenge_is_an_error_and_is_not_retried(transport, status):
    transport.return_value = response(
        text="<title>Just a moment...</title>", status=status
    )
    with pytest.raises(moflix.MoflixResponseError, match="verification page"):
        moflix.search("silo")
    assert transport.call_count == 1


def test_normal_page_may_include_cloudflare_precursor_script(transport):
    transport.return_value = page("searchPage", {"query": "silo", "results": []})
    transport.return_value.text += (
        '<script src="/cdn-cgi/challenge-platform/scripts/precursor/main.js"></script>'
    )
    assert moflix.search("silo") == []


def test_rate_limit_retries_once_and_honours_short_retry_after(transport, monkeypatch):
    limited = response(status=429, headers={"Retry-After": "2"})
    transport.side_effect = [
        limited,
        page("searchPage", {"query": "silo", "results": []}),
    ]
    sleep = Mock()
    monkeypatch.setattr(moflix.time, "sleep", sleep)
    assert moflix.search("silo") == []
    sleep.assert_called_once_with(2)
    limited.close.assert_called_once()


@pytest.mark.parametrize("retry_after", ["120", "-1", "tomorrow", "nan"])
def test_long_or_invalid_retry_after_does_not_block(
    transport, monkeypatch, retry_after
):
    transport.return_value = response(status=429, headers={"Retry-After": retry_after})
    sleep = Mock()
    monkeypatch.setattr(moflix.time, "sleep", sleep)
    with pytest.raises(moflix.MoflixResponseError, match="429"):
        moflix.search("silo")
    sleep.assert_not_called()
    assert transport.call_count == 1


def test_retries_are_bounded(transport, monkeypatch):
    transport.return_value = response(status=503)
    monkeypatch.setattr(moflix.time, "sleep", Mock())
    with pytest.raises(moflix.MoflixResponseError, match="503"):
        moflix.search("silo")
    assert transport.call_count == 2


@pytest.mark.parametrize(
    "error",
    [moflix.requests.exceptions.Timeout, moflix.requests.exceptions.ConnectionError],
)
def test_network_errors_do_not_expose_credentials(transport, error):
    transport.side_effect = error("secret-token in request")
    with pytest.raises(moflix.MoflixResponseError) as exc:
        moflix.search("silo")
    assert "secret-token" not in str(exc.value)
    assert transport.call_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/api",
        "http://moflix-stream.xyz/",
        "https://moflix-stream.xyz.evil.test/",
        "https://user:pass@moflix-stream.xyz/",
    ],
)
def test_transport_rejects_other_origins(transport, url):
    with pytest.raises(moflix.MoflixResponseError, match="URL"):
        moflix.fetch(url)
    transport.assert_not_called()


@pytest.mark.parametrize(
    "answer",
    [
        response(text="<html/>", headers={"Content-Type": "text/html"}),
        response(payload=[]),
        response(payload=None),
    ],
)
def test_api_requires_a_json_object(answer):
    with pytest.raises(moflix.MoflixResponseError):
        moflix.read_json(answer)


def test_pagination_follows_numeric_page_without_losing_parameters():
    get = Mock(
        side_effect=[
            response(
                payload={"episodes": {"data": [{"episode_number": 1}], "next_page": 2}}
            ),
            response(
                payload={
                    "episodes": {"data": [{"episode_number": 2}], "next_page": None}
                }
            ),
        ]
    )
    url = moflix.BASE_URL + "/api/v1/titles/42/seasons/1?perPage=500"
    assert moflix.paginated_items(url, "episodes", get) == [
        {"episode_number": 1},
        {"episode_number": 2},
    ]
    assert get.call_args.args == (url + "&page=2",)


@pytest.mark.parametrize("next_page", ["https://example.com/next", True, -1, "abc", 1])
def test_pagination_rejects_untrusted_next_page(next_page):
    get = Mock(
        return_value=response(
            payload={"episodes": {"data": [], "next_page": next_page}}
        )
    )
    with pytest.raises(moflix.MoflixResponseError, match="pagination"):
        moflix.paginated_items(
            moflix.BASE_URL + "/api/v1/titles/42/seasons/1", "episodes", get
        )
    assert get.call_count == 1


def test_pagination_supports_last_page_without_next_pointer():
    get = Mock(
        side_effect=[
            response(payload={"episodes": {"data": [1], "last_page": 2}}),
            response(payload={"episodes": {"data": [2], "last_page": 2}}),
        ]
    )
    assert moflix.paginated_items(
        moflix.BASE_URL + "/api/v1/titles/42/seasons/1", "episodes", get
    ) == [1, 2]


def test_failed_browse_is_not_cached_and_can_recover(client, monkeypatch, transport):
    from h0melab.web.views import api_media

    monkeypatch.setattr(api_media, "_browse_cache", {})
    transport.side_effect = [
        response(text="<title>Just a moment...</title>", status=403),
        page(
            "channelPage",
            {"channel": {"slug": "now-playing", "content": {"data": [title()]}}},
        ),
    ]
    failed = client.get("/api/moflix-movies")
    assert failed.status_code == 502
    assert "verification page" in failed.get_json()["error"]
    assert "moflix_movies" not in api_media._browse_cache
    recovered = client.get("/api/moflix-movies")
    assert recovered.status_code == 200
    assert recovered.get_json()["results"][0]["title"] == "Silo"
    assert client.get("/api/moflix-movies").status_code == 200
    assert transport.call_count == 2


def test_search_api_exposes_public_moflix_error(client, transport):
    transport.return_value = response(
        status=403, text="<title>Just a moment...</title>"
    )
    result = client.post("/api/search", json={"site": "moflix", "keyword": "silo"})
    assert result.status_code == 502
    assert "verification page" in result.get_json()["error"]
