"""Evidence-based provider matching. Scores explain evidence, never erase conflicts."""

import re
import unicodedata
from datetime import date


def _text(value):

    if not isinstance(value, str):
        return ""

    value = unicodedata.normalize("NFKC", value).casefold()

    return " ".join(re.findall(r"\w+", value))


def _year(value):

    value = str(value or "")

    return value if re.fullmatch(r"(?:18|19|20|21)\d{2}", value) else None


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _kind(value):

    return "tv" if value == "series" else value


def _aliases(item):

    alternatives = item.get("alternative_titles") or []

    if not isinstance(alternatives, list):
        alternatives = []

    return {
        _text(x)
        for x in [item.get("title"), item.get("original_title"), *alternatives]
        if _text(x)
    }


def assess_candidate(identity, hit):

    reasons, score, conflict = [], 0, False

    kind = _kind(hit.get("media_type"))

    typed = kind in ("movie", "tv") and kind == identity.get("media_type")

    if not typed:
        reasons.append("Media type is missing or differs")

        conflict = kind in ("movie", "tv")

    if hit.get("metadata_conflicts"):
        conflict = True

        reasons.extend(hit["metadata_conflicts"])

    id_match = False

    for field in ("tmdb_id", "imdb_id"):
        expected, actual = identity.get(field), hit.get(field)

        if actual in (None, ""):
            continue

        valid = (
            (type(actual) is int and actual > 0)
            if field == "tmdb_id"
            else (
                isinstance(actual, str)
                and re.fullmatch(r"tt\d{7,12}", actual) is not None
            )
        )

        if not valid:
            conflict = True

            reasons.append(f"Invalid provider {field}")

        elif expected in (None, ""):
            reasons.append(f"Provider {field} has no verified counterpart")

            conflict = True

        elif actual != expected:
            conflict = True

            reasons.append(f"Conflicting {field}")

        else:
            id_match = True

            score += 100

            reasons.append(f"Confirmed {field}")

    title_match = bool(_aliases(identity) & _aliases(hit))

    if title_match:
        score += 20

        reasons.append("Exact title or verified alias")

    wanted_year = _year(identity.get("release_year") or identity.get("year"))

    found_year = _year(hit.get("release_year") or hit.get("year"))

    year_match = bool(wanted_year and wanted_year == found_year)

    if year_match:
        score += 20

        reasons.append("Release year matches")

    elif wanted_year and found_year:
        conflict = True

        reasons.append("Conflicting release year")

    raw_date = hit.get("release_date")
    found_date = _date(raw_date)
    wanted_date = _date(identity.get("release_date"))
    if raw_date not in (None, "") and found_date is None:
        conflict = True
        reasons.append("Invalid provider release date")
    if found_date:
        if found_year and str(found_date.year) != found_year:
            conflict = True
            reasons.append("Provider release date conflicts with its year")
        if wanted_date and found_date != wanted_date:
            conflict = True
            reasons.append("Release dates differ; review regional release information")
        elif wanted_date:
            score += 10
            reasons.append("Release date matches")

    if hit.get("metadata_error"):
        reasons.append("Provider metadata could not be verified")

    eligible = (
        typed
        and not conflict
        and not hit.get("metadata_error")
        and (id_match or (title_match and year_match))
    )

    if not eligible and not conflict:
        reasons.append("Insufficient identity evidence; manual review required")

    return {
        "eligible": bool(eligible),
        "score": score,
        "reasons": reasons,
        "conflict": conflict,
    }


def choose_candidate(identity, hits):

    unique = {}

    for hit in hits:
        if (
            assess_candidate(identity, hit)["eligible"]
            and isinstance(hit.get("url"), str)
            and hit["url"]
        ):
            unique[hit["url"]] = hit

    return next(iter(unique.values()), None)


AUTO_REASON = "automatic: best available source"


def hard_conflict(identity, hit):
    """A different media type or a foreign external ID; never auto-accepted."""
    kind = _kind(hit.get("media_type"))
    if kind in ("movie", "tv") and kind != identity.get("media_type"):
        return True
    for field in ("tmdb_id", "imdb_id"):
        actual, expected = hit.get(field), identity.get(field)
        if actual not in (None, "") and expected not in (None, "") and actual != expected:
            return True
    return False


def auto_acceptable(identity, hit):
    """Strictly verified, or the same title without a hard conflict."""
    if not isinstance(hit.get("url"), str) or not hit["url"]:
        return False
    if assess_candidate(identity, hit)["eligible"]:
        return True
    if (
        hit.get("metadata_error")
        or hard_conflict(identity, hit)
        or _kind(hit.get("media_type")) != identity.get("media_type")
    ):
        return False
    id_match = any(
        hit.get(f) not in (None, "") and hit.get(f) == identity.get(f)
        for f in ("tmdb_id", "imdb_id")
    )
    return id_match or bool(_aliases(identity) & _aliases(hit))


def rank_candidates(identity, hits):
    """Usable sources in site priority (SerienStream first for series)."""
    from .sitesearch import sites_for

    order = list(sites_for(identity.get("media_type")))
    unique = {}
    for position, hit in enumerate(hits):
        if hit.get("url") not in unique and auto_acceptable(identity, hit):
            unique[hit["url"]] = (position, hit)

    def key(pair):
        position, hit = pair
        assessment = assess_candidate(identity, hit)
        source = hit.get("source")
        site = order.index(source) if source in order else len(order)
        return (site, not assessment["eligible"], -assessment["score"], position)

    return [hit for _position, hit in sorted(unique.values(), key=key)]


def episode_acceptable(identity, tmdb_episode, provider_row):
    """Same season/episode number and no foreign series ID; titles may differ."""
    if assess_episode(identity, tmdb_episode, provider_row)["eligible"]:
        return True
    for field, expected in (
        ("series_tmdb_id", identity["tmdb_id"]),
        ("series_imdb_id", identity.get("imdb_id")),
    ):
        actual = provider_row.get(field)
        if actual is not None and actual != expected:
            return False
    for field, minimum in (("season_number", 0), ("episode_number", 1)):
        actual, expected = provider_row.get(field), tmdb_episode.get(field)
        if type(actual) is not int or actual < minimum or actual != expected:
            return False
    return bool(provider_row.get("url"))


def assess_episode(identity, tmdb_episode, provider_row):

    reasons, conflict = [], False

    for field, expected in (
        ("series_tmdb_id", identity["tmdb_id"]),
        ("series_imdb_id", identity.get("imdb_id")),
    ):
        actual = provider_row.get(field)

        if actual is not None and (
            expected is None or type(actual) is not type(expected) or actual != expected
        ):
            reasons.append(f"Conflicting {field}")

            conflict = True

    if (
        tmdb_episode.get("series_tmdb_id", tmdb_episode.get("show_id"))
        != identity["tmdb_id"]
    ):
        reasons.append("TMDB episode belongs to a different series")

        conflict = True

    for field in ("season_number", "episode_number"):
        actual, expected = provider_row.get(field), tmdb_episode.get(field)

        minimum = 0 if field == "season_number" else 1

        if (
            type(actual) is not int
            or type(expected) is not int
            or actual < minimum
            or actual != expected
        ):
            reasons.append(f"Missing or conflicting {field}")

            conflict = True

    names = {
        _text(provider_row.get(k))
        for k in ("title_de", "title_en", "episode_title")
        if _text(provider_row.get(k))
    }

    expected_names = {
        _text(tmdb_episode.get("name")),
        _text(tmdb_episode.get("original_name")),
    } - {""}

    generic = re.compile(r"^(?:episode|folge|ep) \d+$")

    names = {name for name in names if not generic.fullmatch(name)}

    title_match = bool(names & expected_names)

    if names and expected_names and not title_match:
        reasons.append("Episode title conflicts with TMDB numbering")

        conflict = True

    # Matching numbers without meaningful episode evidence do not prove alternate numbering.

    if not title_match:
        reasons.append("Episode mapping requires manual confirmation")

    return {
        "eligible": not conflict and title_match,
        "score": 40 if title_match else 0,
        "reasons": reasons,
        "conflict": conflict,
    }


def enrich_candidate(hit):
    """Read existing provider models without replacing contradictory search evidence.



    Missing type stays missing: no title, URL or requested-type guessing occurs.

    metadata_conflicts makes changed source IDs/type/year a hard matching failure.

    """

    from ..providers import resolve_provider

    result = dict(hit)

    conflicts = list(hit.get("metadata_conflicts") or [])

    def merge(field, value):

        if value in (None, ""):
            return

        old = result.get(field)

        if old not in (None, ""):
            left = _kind(old) if field == "media_type" else old

            right = _kind(value) if field == "media_type" else value

            if field == "release_year":
                left, right = str(old), str(value)

            if left != right:
                conflicts.append(f"Provider detail conflicts with search {field}")

                return

        result[field] = value

    try:
        provider = resolve_provider(hit["url"])

        factory = provider.series_cls or provider.episode_cls

        model = factory(url=hit["url"])

        # Some providers expose only one spelling, others expose both.

        for attribute, field in (
            ("tmdb_id", "tmdb_id"),
            ("imdb_id", "imdb_id"),
            ("imdb", "imdb_id"),
        ):
            merge(field, getattr(model, attribute, None))

        for field in ("imdb_id", "tmdb_id"):
            external = hit.get("external_ids")

            if isinstance(external, dict):
                merge(field, external.get(field))

        merge("imdb_id", hit.get("imdb"))

        kind = getattr(model, "media_type", None)

        if kind in ("movie", "tv", "series"):
            merge("media_type", kind)

        for attribute, true_kind, false_kind in (
            ("is_movie", "movie", "tv"),
            ("is_series", "tv", "movie"),
        ):
            value = getattr(model, attribute, None)

            if type(value) is bool:
                merge("media_type", true_kind if value else false_kind)

        merge("release_date", getattr(model, "release_date", None))

        year = getattr(model, "release_year", None)

        if year not in (None, ""):
            # A verified series run range denotes its premiere, not the last year.

            match = re.fullmatch(
                r"((?:18|19|20|21)\d{2})\s*[-–]\s*((?:18|19|20|21)\d{2})", str(year)
            )

            if (
                match
                and _kind(result.get("media_type")) == "tv"
                and match[1] <= match[2]
            ):
                year = match[1]

            if result.get("year") and str(result["year"]) != str(year):
                conflicts.append("Provider detail conflicts with search year")

            merge("release_year", year)

        # Different localized titles are not ID contradictions; preserve search

        # text while requiring the detail title to participate in matching.

        title = getattr(model, "title", None)

        if isinstance(title, str) and title.strip():
            result["title"] = title

        original = getattr(model, "original_title", None)

        if isinstance(original, str) and original.strip():
            result["original_title"] = original

    except Exception:
        # Never expose provider exception URLs, tokens or request payloads.

        result["metadata_error"] = "Provider metadata could not be verified"

    if conflicts:
        result["metadata_conflicts"] = list(dict.fromkeys(conflicts))

    return result


class IdentityReviewRequired(ValueError):
    """Locally authored identity failure that must pause for administrator review."""


def revalidate_selection(identity, request):
    """Read the selected detail page afresh; never reuse search evidence as fresh data."""
    url = request.get("selected_url")
    previous = next(
        (hit for hit in request.get("candidates", []) if hit.get("url") == url), None
    )
    if not url or previous is None:
        raise IdentityReviewRequired(
            "Selected provider evidence is missing before download"
        )
    fresh = enrich_candidate({"url": url})
    assessment = assess_candidate(identity, fresh)
    # An automatic or reviewed pick accepted soft conflicts (e.g. year) already.
    conflict = (
        hard_conflict(identity, fresh)
        if request.get("review_reason")
        else assessment["conflict"]
    )
    if (
        fresh.get("metadata_error")
        or conflict
        or _kind(fresh.get("media_type")) != identity["media_type"]
    ):
        raise IdentityReviewRequired(
            "Fresh provider identity conflicts or cannot be verified"
        )
    if not request.get("review_reason"):
        if not assessment["eligible"]:
            raise IdentityReviewRequired(
                "Fresh provider identity has insufficient automatic evidence"
            )
    else:
        # A manual decision binds the reviewed page, not every future page at its URL.
        old_titles, new_titles = _aliases(previous), _aliases(fresh)
        if old_titles and (not new_titles or not old_titles & new_titles):
            raise IdentityReviewRequired(
                "Reviewed provider title changed before download"
            )
        for field in ("tmdb_id", "imdb_id"):
            if (
                previous.get(field) not in (None, "")
                and fresh.get(field) != previous[field]
            ):
                raise IdentityReviewRequired(
                    "Reviewed provider identity evidence changed before download"
                )
        old_date = previous.get("release_date")
        if old_date not in (None, "") and fresh.get("release_date") != old_date:
            raise IdentityReviewRequired(
                "Reviewed provider release date changed before download"
            )
        old_year = _year(previous.get("release_year") or previous.get("year"))
        new_year = _year(fresh.get("release_year") or fresh.get("year"))
        if old_year and new_year != old_year:
            raise IdentityReviewRequired(
                "Reviewed provider year changed before download"
            )
    return fresh


def revalidate_episode_source(identity, request, entry):
    """Bind execution to the selected source episode, including reviewed mappings."""
    from .seerr_processing import episode_rows

    snapshot = request.get("episode_review") or {}
    old_rows = [
        row for row in snapshot.get("source", []) if row.get("url") == entry["url"]
    ]
    canonical = [
        row
        for row in snapshot.get("canonical", [])
        if row.get("season_number") == entry["season"]
        and row.get("episode_number") == entry["episode"]
    ]
    if len(old_rows) != 1 or len(canonical) != 1:
        raise IdentityReviewRequired(
            "Episode evidence is missing or ambiguous before download"
        )
    source_season = entry.get("source_season", entry["season"])
    # Gap-filled episodes come from another site's series page.
    rows = episode_rows(
        {"url": entry.get("source_url") or request["selected_url"]}, [source_season]
    )
    fresh = [row for row in rows if row.get("url") == entry["url"]]
    if len(fresh) != 1:
        raise IdentityReviewRequired("Provider episode disappeared or became ambiguous")
    row = fresh[0]
    for field, expected in (
        ("season_number", source_season),
        ("episode_number", entry.get("source_episode", entry["episode"])),
    ):
        if type(row.get(field)) is not int or row[field] != expected:
            raise IdentityReviewRequired(
                "Provider episode numbering changed before download"
            )
    for field, expected in (
        ("series_tmdb_id", identity["tmdb_id"]),
        ("series_imdb_id", identity.get("imdb_id")),
    ):
        actual = row.get(field)
        if actual is not None and (
            type(actual) is not type(expected) or actual != expected
        ):
            raise IdentityReviewRequired("Provider episode series identity changed")
    manual = any(
        mapping.get("url") == entry["url"]
        and mapping.get("season_number") == entry["season"]
        and mapping.get("episode_number") == entry["episode"]
        for mapping in request.get("episode_mapping", [])
    )
    if manual:
        fields = ("title_de", "title_en", "episode_title")
        old_names = {_text(old_rows[0].get(field)) for field in fields} - {""}
        new_names = {_text(row.get(field)) for field in fields} - {""}
        if old_names and not old_names & new_names:
            raise IdentityReviewRequired(
                "Reviewed episode title changed before download"
            )
    else:
        episode = {**canonical[0], "series_tmdb_id": identity["tmdb_id"]}
        if not episode_acceptable(identity, episode, row):
            raise IdentityReviewRequired("Fresh episode mapping requires manual review")
