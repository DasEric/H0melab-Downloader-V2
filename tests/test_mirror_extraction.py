import pytest

from h0melab.models.filmpalast_to import FilmPalastEpisode


@pytest.mark.parametrize(
    "base",
    [
        "",
        "/",
        "https://filmpalast.to/",
        "http://filmpalast.to/",
        "https://www.filmpalast.example/",
    ],
)
def test_filmpalast_genre_links(base):
    movie = FilmPalastEpisode("https://filmpalast.to/stream/example")
    movie._FilmPalastEpisode__html = (
        f'<a href="{base}search/genre/action" class="genre">Action</a>'
        f'<a href="{base}search/genre/drama">Drama</a>'
        '<a href="https://unrelated.example/search/genre/horror">Horror</a>'
    )
    assert movie.genres == ["Action", "Drama"]


@pytest.mark.parametrize("kind", ["aniworld", "filmpalast", "megakino"])
@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_hoster_redirect_failure_does_not_poison_cache(monkeypatch, kind, failure):
    from h0melab.config import GLOBAL_SESSION
    from h0melab.models.h0melab_to.episode import AniworldEpisode
    from h0melab.models.megakino.series import MegaKinoEpisode

    cls = {
        "aniworld": AniworldEpisode,
        "filmpalast": FilmPalastEpisode,
        "megakino": MegaKinoEpisode,
    }[kind]
    model = object.__new__(cls)
    prefix = "_" + cls.__name__ + "__"
    setattr(model, prefix + "provider_url", None)
    setattr(model, prefix + "redirect_url", "https://source.example/redirect/1")
    calls = []

    class Response:
        def raise_for_status(self):
            if len(calls) == 1:
                raise RuntimeError("503 upstream")

        @property
        def url(self):
            assert len(calls) > 1, "Failed response URL must not be cached"
            return "https://hoster.example/video"

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1 and failure == "timeout":
            raise TimeoutError("upstream timeout")
        return Response()

    monkeypatch.setattr(GLOBAL_SESSION, "get", get)
    with pytest.raises((RuntimeError, TimeoutError)):
        _ = model.provider_url
    assert model.provider_url == "https://hoster.example/video"
    assert model.provider_url == "https://hoster.example/video"
    assert calls == [("https://source.example/redirect/1", {"timeout": 15})] * 2


def test_aniworld_invalid_redirect_is_revalidated_each_time(monkeypatch):
    from types import SimpleNamespace

    from h0melab.config import GLOBAL_SESSION
    from h0melab.models.h0melab_to.episode import AniworldEpisode

    model = object.__new__(AniworldEpisode)
    model._AniworldEpisode__provider_url = None
    model._AniworldEpisode__redirect_url = "https://aniworld.to/redirect/1"
    model._AniworldEpisode__selected_provider = "VOE"
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(raise_for_status=lambda: None, url=url)

    monkeypatch.setattr(GLOBAL_SESSION, "get", get)
    for _ in range(2):
        with pytest.raises(ValueError, match="Failed to resolve provider URL"):
            _ = model.provider_url
        assert model._AniworldEpisode__provider_url is None
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_filmpalast_metadata_failure_is_not_cached(monkeypatch, failure):
    from h0melab.config import GLOBAL_SESSION

    model = FilmPalastEpisode("https://filmpalast.to/stream/example")
    calls = []

    class Response:
        def raise_for_status(self):
            if len(calls) == 1:
                raise RuntimeError("503 upstream")

        @property
        def text(self):
            assert len(calls) > 1, "Failed metadata must not be consumed"
            return "<html>verified metadata</html>"

    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert kwargs.get("timeout") == 15
        assert kwargs["headers"]["Accept-Encoding"] == "gzip, deflate"
        if len(calls) == 1 and failure == "timeout":
            raise TimeoutError("metadata timeout")
        return Response()

    monkeypatch.setattr(GLOBAL_SESSION, "get", get)
    with pytest.raises((RuntimeError, TimeoutError)):
        _ = model._html
    assert model._FilmPalastEpisode__html is None
    assert model._html == "<html>verified metadata</html>"
    assert model._html == "<html>verified metadata</html>"
    assert len(calls) == 2
