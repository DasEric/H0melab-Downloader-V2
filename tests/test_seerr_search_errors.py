from h0melab.web import seerr_processing, sitesearch
import pytest


def test_search_exception_does_not_leak_and_is_not_empty_success(monkeypatch, caplog):
    secret = "signed-token-never-log"

    def fail(keyword):
        raise RuntimeError("https://provider.test/?token=" + secret)

    monkeypatch.setitem(sitesearch.SITE_SEARCH, "sto", fail)
    monkeypatch.setattr(sitesearch, "sites_for", lambda kind: ["sto"])
    monkeypatch.setattr(
        seerr_processing.settings_store, "site_enabled", lambda site: True
    )
    with pytest.raises(seerr_processing.ReviewRequired, match="Provider search failed"):
        seerr_processing.candidates({"media_type": "tv", "title": "Example"})
    assert secret not in caplog.text
    assert "Example" not in caplog.text


@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
def test_aniworld_failure_is_bounded_and_not_empty_success(monkeypatch, failure):
    from h0melab import search

    calls = []

    class Response:
        def raise_for_status(self):
            if failure == "http":
                raise RuntimeError("503 signed-token")

        def json(self):
            if failure == "json":
                raise ValueError("invalid JSON")
            pytest.fail("Failed HTTP response must not be decoded")

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if failure == "timeout":
            raise TimeoutError("signed-token")
        return Response()

    monkeypatch.setattr(search.GLOBAL_SESSION, "post", post)
    monkeypatch.setitem(sitesearch.SITE_SEARCH, "aniworld", search.query)
    result = sitesearch.search("aniworld", "Example")
    assert result == [] and result.error
    assert "signed-token" not in result.error
    assert calls == [
        (search.SEARCH_URL, {"data": {"keyword": "Example"}, "timeout": 15})
    ]


def test_aniworld_valid_empty_response_remains_success(monkeypatch):
    from h0melab import search
    from types import SimpleNamespace

    checked = []
    monkeypatch.setattr(
        search.GLOBAL_SESSION,
        "post",
        lambda *a, **kw: SimpleNamespace(
            raise_for_status=lambda: checked.append(True), json=lambda: []
        ),
    )
    monkeypatch.setitem(sitesearch.SITE_SEARCH, "aniworld", search.query)
    result = sitesearch.search("aniworld", "Example")
    assert result == [] and not getattr(result, "error", None)
    assert checked == [True]


@pytest.mark.parametrize(
    "kind,suffix",
    [
        ("Series", ""),
        ("Season", "/staffel-1"),
        ("Episode", "/staffel-1/episode-1"),
    ],
)
@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_aniworld_detail_failure_is_not_cached(monkeypatch, kind, suffix, failure):
    from h0melab import models
    from h0melab.config import GLOBAL_SESSION

    url = "https://aniworld.to/anime/stream/example" + suffix
    model = getattr(models, "Aniworld" + kind)(url=url)
    calls = []
    checked = []

    class Response:
        def raise_for_status(self):
            checked.append(True)
            if len(calls) == 1:
                raise RuntimeError("503 upstream")

        @property
        def text(self):
            assert len(calls) > 1, "Failure body must not become metadata"
            return "<html>verified response</html>"

    def get(request_url, **kwargs):
        calls.append((request_url, kwargs))
        if failure == "timeout" and len(calls) == 1:
            raise TimeoutError("upstream timeout")
        return Response()

    monkeypatch.setattr(GLOBAL_SESSION, "get", get)
    with pytest.raises((TimeoutError, RuntimeError)):
        _ = model._html
    assert model._html == "<html>verified response</html>"
    assert model._html == "<html>verified response</html>"
    assert calls == [(url, {"timeout": 15})] * 2
    assert len(checked) == (2 if failure == "http" else 1)
