"""Moflix HTTP access and the structured data delivered with its public pages."""

import json
import re
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from curl_cffi import requests

BASE_URL = "https://moflix-stream.xyz"
REQUEST_TIMEOUT = 10
_local = threading.local()


class MoflixResponseError(ValueError):
    """A public, credential-free explanation of an unusable Moflix response."""


def canonical_title_url(url):
    """Canonicalise a title URL after MOFLIX_SERIES_PATTERN has accepted it."""
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) > 2 and parts[2].lower() != "season":
        del parts[2]
    parts[0] = "titles"
    if len(parts) > 2:
        parts[2] = "season"
    if len(parts) > 4:
        parts[4] = "episodes"
    return urlunsplit(
        ("https", "moflix-stream.xyz", "/" + "/".join(parts), parsed.query, "")
    )


def _session():
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session(impersonate="chrome124")
        _local.session = session
    return session


def validate_response(response):
    headers = getattr(response, "headers", {})
    text = getattr(response, "text", "")
    lower = text[:20000].lower() if isinstance(text, str) else ""
    if headers.get("cf-mitigated") == "challenge" or any(
        marker in lower
        for marker in (
            "<title>just a moment",
            "<title>attention required",
            "cf_chl_opt",
        )
    ):
        raise MoflixResponseError(
            "Moflix returned a verification page. Please try again later."
        )
    status = response.status_code
    if status >= 400:
        raise MoflixResponseError(f"Moflix request failed (HTTP {status}).")
    response.raise_for_status()
    return response


def fetch(url, session_cookies=None, csrf_token=None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "moflix-stream.xyz":
        raise MoflixResponseError("Invalid Moflix request URL.")
    headers = {"Referer": BASE_URL + "/"}
    if csrf_token:
        headers.update(
            {
                "X-XSRF-TOKEN": csrf_token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            }
        )
    for attempt in range(2):
        try:
            response = _session().get(
                url, cookies=session_cookies, headers=headers, timeout=REQUEST_TIMEOUT
            )
        except requests.exceptions.Timeout:
            raise MoflixResponseError("Moflix request timed out.") from None
        except requests.exceptions.RequestException:
            raise MoflixResponseError("Moflix connection failed.") from None
        if response.status_code not in (429, 500, 502, 503, 504) or attempt:
            return validate_response(response)
        retry_after = getattr(response, "headers", {}).get("Retry-After", "1")
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            return validate_response(response)
        if not 0 <= delay <= 2:
            return validate_response(response)
        response.close()
        time.sleep(delay)


def read_json(response):
    validate_response(response)
    content_type = getattr(response, "headers", {}).get("Content-Type", "")
    if content_type and "json" not in content_type.lower():
        raise MoflixResponseError("Moflix returned a page instead of JSON data.")
    try:
        data = response.json()
    except ValueError:
        raise MoflixResponseError("Moflix returned invalid JSON data.") from None
    if not isinstance(data, dict):
        raise MoflixResponseError("Moflix returned an invalid data format.")
    return data


def parse_bootstrap(text):
    match = re.search(r"window\.bootstrapData\s*=\s*", text)
    if not match:
        raise MoflixResponseError("Moflix bootstrap data not found.")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[match.end() :])
    except ValueError:
        raise MoflixResponseError("Moflix bootstrap data is invalid.") from None
    if not isinstance(data, dict):
        raise MoflixResponseError("Moflix bootstrap data has an invalid format.")
    return data


def page_loader(path, name):
    response = fetch(BASE_URL + path)
    data = parse_bootstrap(response.text)
    loaders = data.get("loaders")
    loader = loaders.get(name) if isinstance(loaders, dict) else None
    if not isinstance(loader, dict):
        raise MoflixResponseError("Moflix page data is missing or has changed.")
    return loader


def _cards(items, limit=None):
    if not isinstance(items, list):
        raise MoflixResponseError("Moflix title list has an invalid format.")
    results = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get("model_type") != "title":
            continue
        title_id = item.get("id")
        if isinstance(title_id, bool) or not re.fullmatch(
            r"[1-9][0-9]*", str(title_id)
        ):
            continue
        title_id = str(title_id)
        name = item.get("name")
        if title_id in seen or not isinstance(name, str) or not name.strip():
            continue
        seen.add(title_id)
        poster = item.get("poster") or ""
        poster = (
            urljoin(BASE_URL + "/", poster)
            if isinstance(poster, str) and poster
            else ""
        )
        if urlsplit(poster).scheme not in ("http", "https"):
            poster = ""
        result = {
            "title": name.strip(),
            "url": f"{BASE_URL}/titles/{title_id}",
            "poster_url": poster,
        }
        result.update(
            {
                key: item[key]
                for key in ("is_series", "year", "original_title")
                if item.get(key) is not None
            }
        )
        results.append(result)
        if limit is not None and len(results) >= limit:
            break
    return results


def search(keyword):
    keyword = keyword.strip()
    if not keyword:
        return []
    loader = page_loader("/search/" + quote(keyword, safe=""), "searchPage")
    if loader.get("query") != keyword:
        raise MoflixResponseError("Moflix returned results for a different search.")
    return _cards(loader.get("results"))


def recent_titles():
    loader = page_loader("/now-playing", "channelPage")
    channel = loader.get("channel")
    if not isinstance(channel, dict) or channel.get("slug") != "now-playing":
        raise MoflixResponseError("Moflix returned a different browse channel.")
    content = channel.get("content")
    if not isinstance(content, dict):
        raise MoflixResponseError("Moflix browse data has an invalid format.")
    return _cards(content.get("data"), limit=24)


def paginated_items(url, key, get):
    """Follow numeric pagination on the same endpoint, never an external next URL."""
    items = []
    current_page = int(dict(parse_qsl(urlsplit(url).query)).get("page", "1"))
    for _ in range(100):
        data = read_json(get(url)).get(key)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise MoflixResponseError(f"Moflix {key} list has an invalid format.")
        items.extend(data["data"])
        next_page = data.get("next_page")
        if next_page is None:
            last_page = data.get("last_page")
            if isinstance(last_page, int) and last_page > current_page:
                next_page = current_page + 1
            else:
                return items
        if (
            isinstance(next_page, bool)
            or not re.fullmatch(r"[1-9][0-9]*", str(next_page))
            or int(next_page) <= current_page
        ):
            raise MoflixResponseError("Moflix returned invalid pagination.")
        current_page = int(next_page)
        parsed = urlsplit(url)
        params = [(k, v) for k, v in parse_qsl(parsed.query) if k != "page"]
        url = urlunsplit(
            parsed._replace(query=urlencode([*params, ("page", next_page)]))
        )
    raise MoflixResponseError("Moflix pagination exceeded its limit.")
