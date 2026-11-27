"""Identity preparation in the existing download worker, never a second queue."""

import json
import time
from datetime import UTC, date, datetime

from ..logger import get_logger
from . import db, media_targets, seerr, seerr_matching, settings_store, sitesearch, tmdb

logger = get_logger(__name__)


class ReviewRequired(ValueError):
    """Locally authored review text; external exception messages are never displayed."""


class ProviderSearchUnavailable(ReviewRequired):
    """An incomplete source search must not become an automatic selection."""


def stopped(item):
    """Honor controls after blocking metadata calls and before any download."""
    current = db.get_queue_item(item["id"])
    if current is None:
        return True
    request = seerr.get_request(queue_id=item["id"])
    if current["status"] == "cancelled" or current["cancel_requested"]:
        db.set_queue_status(item["id"], "cancelled")
        seerr.update(request["request_id"], status="cancelled")
        return True
    if current["pause_requested"]:
        db.finish_pause(item["id"])
        return True
    return False


def review(item, message, identity_status="manual_review"):
    if stopped(item):
        return False
    request = seerr.get_request(queue_id=item["id"])
    changed = False
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE download_queue SET status='queued',pause_requested=1,errors=? "
            "WHERE id=? AND status IN ('queued','running') "
            "AND cancel_requested=0 AND pause_requested=0",
            (json.dumps([{"error": message}]), item["id"]),
        ).rowcount
        if changed:
            conn.execute(
                "UPDATE seerr_requests SET status='manual_review',identity_status=?,error=?,updated=? "
                "WHERE request_id=?",
                (identity_status, message, time.time(), request["request_id"]),
            )
    if not changed:
        stopped(item)
        return False
    seerr.notify(request["request_id"], "manual_review", item["title"])
    return False


def retry_later(item, provider=False):
    """Persist a bounded backoff on the existing queue, without another scheduler."""
    if stopped(item):
        return False
    request = seerr.get_request(queue_id=item["id"])
    attempt = request["retry_count"] + 1
    if attempt > 3:
        return review(
            item,
            "Provider search remained unavailable after three retries; check sources and retry manually"
            if provider
            else "Temporary metadata failure persisted after three retries; check API access and retry manually",
            "provider_failed" if provider else "metadata_failed",
        )
    due = time.time() + (30, 120, 600)[attempt - 1]
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE download_queue SET status='queued',not_before=?,started_at=NULL "
            "WHERE id=? AND status IN ('queued','running') AND cancel_requested=0 AND pause_requested=0",
            (due, item["id"]),
        ).rowcount
        if changed:
            conn.execute(
                "UPDATE seerr_requests SET status='waiting_retry',identity_status=?, "
                "retry_count=?,error=?,updated=? WHERE request_id=?",
                (
                    "provider_retry" if provider else "metadata_retry",
                    attempt,
                    "Provider search unavailable; automatic retry scheduled"
                    if provider
                    else "Temporary TMDB failure; automatic retry scheduled",
                    time.time(),
                    request["request_id"],
                ),
            )
    return False


def candidates(identity):
    found = {}
    search_failed = False
    sites = sitesearch.sites_for(identity["media_type"])
    for site in sites:
        if not settings_store.site_enabled(site):
            continue
        for title in dict.fromkeys([identity["title"], identity.get("original_title")]):
            if not title:
                continue
            results = sitesearch.search(site, title)
            if getattr(results, "error", None):
                search_failed = True
                continue
            for hit in results[:100]:
                if hit.get("url") not in found:
                    enriched = seerr_matching.enrich_candidate(hit)
                    enriched["assessment"] = seerr_matching.assess_candidate(
                        identity, enriched
                    )
                    found[hit["url"]] = enriched
    hits = list(found.values())
    if search_failed and not any(hit["assessment"]["eligible"] for hit in hits):
        raise ProviderSearchUnavailable(
            "Provider search failed; retry after checking provider availability"
        )
    return hits


def episode_rows(hit, seasons):
    from ..providers import resolve_provider
    from . import video_library
    from .views import api_media

    url = hit["url"]
    provider = resolve_provider(url)
    group = video_library.resolve_group(url)
    if group:
        return [
            {
                **row,
                "season_number": row["season"],
                "episode_number": row["episode"],
                "episode_title": "",
            }
            for row in video_library.active_members(group)
        ]
    if provider.name in api_media.SINGLE_PAGE_SITES:
        return api_media._single_page_episodes(provider, url)
    rows = []
    model = provider.series_cls(url=url)
    for season in model.seasons:
        if season.season_number in seasons:
            rows.extend(
                {**row, "season_number": season.season_number}
                for row in api_media._season_episodes(provider, season.url, url)
            )
    return rows


def _aired(episode):
    air_date = episode.get("air_date")
    try:
        return not (
            bool(air_date) and date.fromisoformat(air_date) > datetime.now(UTC).date()
        )
    except (ValueError, TypeError):
        return True


def _manual_match(identity, mapping, rows):
    """A reviewed mapping never overrides numbering checks or a foreign series ID."""
    return [
        row
        for row in rows
        if row.get("url") == mapping.get("url")
        and type(row.get("season_number")) is int
        and row["season_number"] >= 0
        and type(row.get("episode_number")) is int
        and row["episode_number"] > 0
        and (
            "source_season_number" not in mapping
            or row["season_number"] == mapping["source_season_number"]
        )
        and all(
            row.get(k) in (None, identity.get(v))
            for k, v in (("series_tmdb_id", "tmdb_id"), ("series_imdb_id", "imdb_id"))
        )
    ]


def _has_language(row, language):
    """Rows without language data are not rejected; listed languages must match."""
    languages = row.get("available_languages")
    return not language or not isinstance(languages, list) or language in languages


MISSING = ("source_missing", "language_missing")


def _map_episodes(identity, canonical_seasons, rows, mappings, language=None):
    """Map one source's rows onto TMDB episodes; returns (entries, pending, used rows)."""
    from . import seerr_files

    entries, pending, used = [], [], []
    taken = set()
    for number, canonical in canonical_seasons.items():
        for episode in canonical:
            receipt = {
                "_seerr_identity": identity,
                "season": number,
                "episode": episode["episode_number"],
                "url": "",
                "_receipt_only": True,
            }
            if seerr_files.existing(receipt):
                entries.append(receipt)
                continue
            if not _aired(episode):
                pending.append({**episode, "season_number": number, "reason": "not_yet_aired"})
                continue
            mapping = [
                m
                for m in mappings
                if m.get("season_number") == number
                and m.get("episode_number") == episode["episode_number"]
            ]
            matches = (
                _manual_match(identity, mapping[0], rows) if len(mapping) == 1 else []
            )
            numbered = []
            if not matches:
                free = [row for row in rows if row.get("url") not in taken]
                numbered = [
                    row
                    for row in free
                    if seerr_matching.episode_acceptable(identity, episode, row)
                ]
                # An episode only offered in another language (e.g. no German
                # dub yet) is looked up on the next site instead of failing.
                free = [row for row in free if _has_language(row, language)]
                matches = [
                    row
                    for row in free
                    if seerr_matching.assess_episode(identity, episode, row)["eligible"]
                ] or [
                    row
                    for row in free
                    if seerr_matching.episode_acceptable(identity, episode, row)
                ]
            if not matches:
                reason = "language_missing" if numbered else "source_missing"
                pending.append({**episode, "season_number": number, "reason": reason})
                continue
            row = matches[0]
            taken.add(row["url"])
            used.append(row)
            entries.append(
                {
                    "url": row["url"],
                    "season": number,
                    "episode": episode["episode_number"],
                    "source_season": row["season_number"],
                    "source_episode": row["episode_number"],
                }
            )
    return entries, pending, used


SOURCE_FIELDS = (
    "url",
    "season_number",
    "episode_number",
    "title_de",
    "title_en",
    "episode_title",
    "series_tmdb_id",
    "series_imdb_id",
)


def _episode_review(hit, rows, canonical_seasons, pending):
    """Persist the exact source snapshot and canonical episodes for inspection."""
    return {
        "provider_url": hit["url"],
        "source": [{k: row[k] for k in SOURCE_FIELDS if k in row} for row in rows],
        "canonical": [
            {
                k: episode[k]
                for k in ("season_number", "episode_number", "name", "air_date")
                if k in episode
            }
            for season in canonical_seasons.values()
            for episode in season
        ],
        "pending": pending,
    }


def _series_plan(item, identity, request, seasons, canonical_seasons, ranked, chosen):
    """Take the first usable source in priority order; fill gaps from the next ones."""
    primary = first = None
    for hit in ranked:
        if stopped(item):
            return None
        manual = chosen is not None and hit["url"] == chosen["url"]
        mappings = (request.get("episode_mapping") or []) if manual else []
        source_seasons = sorted(
            set(seasons)
            | {m["source_season_number"] for m in mappings if "source_season_number" in m}
        )
        try:
            rows = episode_rows(hit, source_seasons)
        except Exception:
            # Provider errors can carry signed URLs; log only which site failed.
            logger.warning("Episode list unavailable on %s; trying next source", hit.get("source"))
            continue
        entries, pending, used = _map_episodes(
            identity, canonical_seasons, rows, mappings, item.get("language")
        )
        first = first or (hit, rows, pending)
        if primary is None:
            if any(not entry.get("_receipt_only") for entry in entries):
                primary = (hit, entries, pending, list(rows))
        else:
            _hit, primary_entries, primary_pending, primary_used = primary
            found = {
                (entry["season"], entry["episode"]): (entry, row)
                for entry, row in zip(
                    [e for e in entries if not e.get("_receipt_only")], used
                )
            }
            still_missing = []
            for episode in primary_pending:
                key = (episode["season_number"], episode["episode_number"])
                if episode["reason"] in MISSING and key in found:
                    entry, row = found[key]
                    primary_entries.append({**entry, "source_url": hit["url"]})
                    primary_used.append(row)
                else:
                    still_missing.append(episode)
            primary_pending[:] = still_missing
        if primary is not None and not any(
            episode["reason"] in MISSING for episode in primary[2]
        ):
            break
    if primary is None:
        if first is not None:
            seerr.update(
                request["request_id"],
                episode_review=_episode_review(first[0], first[1], canonical_seasons, first[2]),
            )
        raise ReviewRequired("No enabled site offers the requested episodes")
    return primary


def prepare(item):
    if stopped(item):
        return False
    request = seerr.get_request(queue_id=item["id"])
    payload = request["payload"]
    if not seerr.enabled():
        return review(item, "Seerr integration is disabled; enable it before retrying")
    try:
        seerr.notify(request["request_id"], "approved_request", item["title"])
        seerr.update(
            request["request_id"], status="checking", error="", episode_review={}
        )
        identity = tmdb.identity_details(
            payload["media_type"], payload.get("tmdb_id"), payload.get("imdb_id")
        )
        if stopped(item):
            return False
        seerr.update(
            request["request_id"], identity=identity, identity_status="confirmed"
        )
        target = media_targets.resolve(
            "movie" if identity["media_type"] == "movie" else "series"
        )
        if target is None:
            raise ReviewRequired(
                "Configure the existing movie and series destinations first"
            )
        path_id, root, _revision = target
        from . import seerr_inventory

        seerr_inventory.discover(root, identity, payload["seasons"])
        from . import seerr_files

        canonical_seasons = {}
        if identity["media_type"] == "tv":
            seasons = payload["seasons"]
            if not seasons:
                raise ReviewRequired("Explicit requested seasons are required")
            available = {row["season_number"] for row in identity["seasons"]}
            if not set(seasons) <= available:
                raise ReviewRequired(
                    "Requested season is not part of the verified series"
                )
            canonical_seasons = {
                number: tmdb.season_details(identity["tmdb_id"], number)["episodes"]
                for number in seasons
            }
            if any(not episodes for episodes in canonical_seasons.values()):
                raise ReviewRequired("Requested season has no verified episodes")
            receipt_entries = [
                {
                    "_seerr_identity": identity,
                    "season": number,
                    "episode": episode["episode_number"],
                    "url": "",
                    "_receipt_only": True,
                }
                for number, episodes in canonical_seasons.items()
                for episode in episodes
            ]
        else:
            receipt_entries = [
                {
                    "_seerr_identity": identity,
                    "season": 0,
                    "episode": 0,
                    "url": "",
                    "_receipt_only": True,
                }
            ]
        if receipt_entries and all(
            seerr_files.existing(entry) for entry in receipt_entries
        ):
            if stopped(item):
                return False
            with db.session() as conn:
                conn.execute(
                    "UPDATE download_queue SET title=?,episodes=?,total_episodes=?,"
                    "current_episode=0,custom_path_id=? WHERE id=?",
                    (
                        identity["title"],
                        json.dumps(receipt_entries),
                        len(receipt_entries),
                        path_id,
                        item["id"],
                    ),
                )
            item.update(db.get_queue_item(item["id"]))
            seerr.update(
                request["request_id"],
                target_path=str(root),
                identity_status="confirmed",
            )
            return True
        hits = candidates(identity)
        if stopped(item):
            return False
        selected_url = request.get("selected_url") or ""
        if selected_url and request.get("review_reason"):
            # Compare against the stored evidence before replacing candidates
            # with the latest search snapshot.
            try:
                seerr_matching.revalidate_selection(identity, request)
            except seerr_matching.IdentityReviewRequired:
                # A changed page no longer binds the old choice; choose again
                # automatically instead of waiting for a manual approval.
                selected_url = ""
                seerr.update(
                    request["request_id"],
                    selected_url="",
                    review_reason="",
                    episode_mapping=[],
                )
                request = seerr.get_request(request_id=request["request_id"])
            if stopped(item):
                return False
        seerr.update(request["request_id"], candidates=hits)
        ranked = seerr_matching.rank_candidates(identity, hits)
        # Only an administrator's choice is preferred; an earlier automatic
        # pick is simply ranked again with the fresh results.
        chosen = (
            next((h for h in hits if h["url"] == selected_url), None)
            if request.get("review_reason")
            and request.get("review_reason") != seerr_matching.AUTO_REASON
            else None
        )
        if chosen is not None and (
            chosen.get("metadata_error")
            or seerr_matching.hard_conflict(identity, chosen)
        ):
            chosen = None
        if chosen is not None:
            ranked = [chosen] + [h for h in ranked if h["url"] != chosen["url"]]
        if not ranked:
            raise ReviewRequired("No matching source found on any enabled site")
        pending = []
        if identity["media_type"] == "movie":
            hit = ranked[0]
            entries = [{"url": hit["url"], "season": 0, "episode": 0}]
        else:
            plan = _series_plan(
                item, identity, request, payload["seasons"], canonical_seasons, ranked, chosen
            )
            if plan is None:
                return False
            hit, entries, pending, source_rows = plan
            seerr.update(
                request["request_id"],
                episode_review=_episode_review(hit, source_rows, canonical_seasons, pending),
            )
        manual = chosen is not None and hit["url"] == chosen["url"]
        if manual:
            review_reason = request.get("review_reason") or ""
        elif seerr_matching.assess_candidate(identity, hit)["eligible"]:
            review_reason = ""
        else:
            review_reason = seerr_matching.AUTO_REASON
        seerr.update(
            request["request_id"],
            review_reason=review_reason,
            **({} if manual else {"episode_mapping": []}),
        )
        if stopped(item):
            return False
        source_entries = [entry for entry in entries if not entry.get("_receipt_only")]
        if len({entry["url"] for entry in source_entries}) != len(source_entries):
            raise ReviewRequired(
                "One provider episode cannot represent multiple requested episodes"
            )
        if not entries:
            raise ReviewRequired("No verified downloadable entries")
        for entry in entries:
            entry["_seerr_identity"] = identity
        # Re-preparation starts from the first entry; identity receipts skip completed
        # files even when providers changed ordering between process restarts.
        with db.session() as conn:
            conn.execute(
                "UPDATE download_queue SET title=?,series_url=?,episodes=?,total_episodes=?,current_episode=0,custom_path_id=?,source_site=? WHERE id=?",
                (
                    identity["title"],
                    hit["url"],
                    json.dumps(entries),
                    len(entries),
                    path_id,
                    hit.get("source"),
                    item["id"],
                ),
            )
        item.update(db.get_queue_item(item["id"]))
        seerr.update(
            request["request_id"],
            status="downloading",
            target_path=str(root),
            identity_status="confirmed",
            selected_url=hit["url"],
        )
        seerr.notify(request["request_id"], "identity_confirmed", identity["title"])
        seerr.notify(request["request_id"], "download_started", identity["title"])
        return True
    except ProviderSearchUnavailable:
        return retry_later(item, provider=True)
    except tmdb.TMDBTransientError:
        return retry_later(item)
    except tmdb.TMDBError:
        return review(
            item,
            "TMDB/IMDb verification failed; retry after checking configuration or API availability",
            "metadata_failed",
        )
    except Exception as exc:
        # Never persist provider exception messages containing signed URLs/secrets.
        message = (
            str(exc)
            if type(exc) is ReviewRequired
            else "Provider verification failed; retry or review the request"
        )
        return review(item, message)
