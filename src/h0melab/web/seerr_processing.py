"""Identity preparation in the existing download worker, never a second queue."""

import json
import time
from datetime import UTC, date, datetime

from . import db, media_targets, seerr, seerr_matching, settings_store, sitesearch, tmdb


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
    sites = sitesearch.sites_for(identity["media_type"])
    for site in sites:
        if not settings_store.site_enabled(site):
            continue
        for title in dict.fromkeys([identity["title"], identity.get("original_title")]):
            if not title:
                continue
            results = sitesearch.search(site, title)
            if getattr(results, "error", None):
                raise ProviderSearchUnavailable(
                    "Provider search failed; retry after checking provider availability"
                )
            for hit in results[:100]:
                if hit.get("url") not in found:
                    enriched = seerr_matching.enrich_candidate(hit)
                    enriched["assessment"] = seerr_matching.assess_candidate(
                        identity, enriched
                    )
                    found[hit["url"]] = enriched
    return list(found.values())


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
        if request.get("selected_url") and request.get("review_reason"):
            # Compare against the administrator's stored evidence before replacing
            # candidates with the latest search snapshot.
            try:
                seerr_matching.revalidate_selection(identity, request)
            except seerr_matching.IdentityReviewRequired:
                if stopped(item):
                    return False
                # A changed page revokes the old approval. Publish fresh evidence
                # for a new explicit review, but never download in this attempt.
                seerr.update(
                    request["request_id"],
                    candidates=hits,
                    selected_url="",
                    review_reason="",
                    episode_mapping=[],
                    episode_review={},
                )
                return review(
                    item,
                    "Reviewed provider evidence changed; inspect the refreshed candidates and approve again",
                )
            if stopped(item):
                return False
        seerr.update(request["request_id"], candidates=hits)
        if request.get("selected_url"):
            hit = next((h for h in hits if h["url"] == request["selected_url"]), None)
            if not hit:
                raise ReviewRequired("Selected provider is no longer available")
            assessment = seerr_matching.assess_candidate(identity, hit)
            observed_kind = hit.get("media_type")
            if observed_kind == "series":
                observed_kind = "tv"
            if (
                assessment["conflict"]
                or hit.get("metadata_error")
                or observed_kind != identity["media_type"]
            ):
                raise ReviewRequired(
                    "Selected provider identity conflicts or is unverified"
                )
            if not request.get("review_reason") and not assessment["eligible"]:
                raise ReviewRequired(
                    "Previously automatic selection no longer has sufficient identity evidence"
                )
        else:
            hit = seerr_matching.choose_candidate(identity, hits)
        if not hit:
            raise ReviewRequired("Provider results require manual selection")
        entries = []
        pending = []
        if identity["media_type"] == "movie":
            entries = [{"url": hit["url"], "season": 0, "episode": 0}]
        else:
            seasons = payload["seasons"]
            source_seasons = sorted(
                set(seasons)
                | {
                    mapping["source_season_number"]
                    for mapping in (request.get("episode_mapping") or [])
                    if "source_season_number" in mapping
                }
            )
            rows = episode_rows(hit, source_seasons)
            # Persist the exact source snapshot and canonical choices even when
            # matching pauses, so the administrator can make an informed mapping.
            source_fields = (
                "url",
                "season_number",
                "episode_number",
                "title_de",
                "title_en",
                "episode_title",
                "series_tmdb_id",
                "series_imdb_id",
            )
            seerr.update(
                request["request_id"],
                episode_review={
                    "provider_url": hit["url"],
                    "source": [
                        {k: row[k] for k in source_fields if k in row} for row in rows
                    ],
                    "canonical": [
                        {
                            k: episode[k]
                            for k in (
                                "season_number",
                                "episode_number",
                                "name",
                                "air_date",
                            )
                            if k in episode
                        }
                        for season in canonical_seasons.values()
                        for episode in season
                    ],
                },
            )
            for number in seasons:
                canonical = canonical_seasons[number]
                if not canonical:
                    raise ReviewRequired("Requested season has no verified episodes")
                for episode in canonical:
                    from . import seerr_files

                    receipt_entry = {
                        "_seerr_identity": identity,
                        "season": number,
                        "episode": episode["episode_number"],
                        "url": "",
                        "_receipt_only": True,
                    }
                    if seerr_files.existing(receipt_entry):
                        entries.append(receipt_entry)
                        continue
                    air_date = episode.get("air_date")
                    try:
                        future = (
                            bool(air_date)
                            and date.fromisoformat(air_date)
                            > datetime.now(UTC).date()
                        )
                    except (ValueError, TypeError):
                        future = False
                    if future:
                        pending.append({**episode, "reason": "not_yet_aired"})
                        continue
                    matches = [
                        row
                        for row in rows
                        if seerr_matching.assess_episode(identity, episode, row)[
                            "eligible"
                        ]
                    ]
                    if len(matches) != 1 or any(
                        m.get("season_number") == number
                        and m.get("episode_number") == episode["episode_number"]
                        for m in (request.get("episode_mapping") or [])
                    ):
                        matches = []
                        mapping = [
                            m
                            for m in (request.get("episode_mapping") or [])
                            if m.get("season_number") == number
                            and m.get("episode_number") == episode["episode_number"]
                        ]
                        if len(mapping) == 1:
                            verified = [
                                row
                                for row in rows
                                if row.get("url") == mapping[0].get("url")
                                and type(row.get("season_number")) is int
                                and row["season_number"] >= 0
                                and type(row.get("episode_number")) is int
                                and row["episode_number"] > 0
                                and (
                                    "source_season_number" not in mapping[0]
                                    or row["season_number"]
                                    == mapping[0]["source_season_number"]
                                )
                            ]
                            # A manual title decision never overrides a foreign series ID.
                            verified = [
                                row
                                for row in verified
                                if all(
                                    row.get(k) in (None, identity.get(v))
                                    for k, v in (
                                        ("series_tmdb_id", "tmdb_id"),
                                        ("series_imdb_id", "imdb_id"),
                                    )
                                )
                            ]
                            if len(verified) == 1:
                                matches = verified
                    if (
                        not matches
                        and not any(
                            row.get("season_number") == number
                            and row.get("episode_number") == episode["episode_number"]
                            for row in rows
                        )
                        and not any(
                            m.get("season_number") == number
                            and m.get("episode_number") == episode["episode_number"]
                            for m in (request.get("episode_mapping") or [])
                        )
                    ):
                        pending.append({**episode, "reason": "source_missing"})
                        continue
                    if len(matches) != 1:
                        raise ReviewRequired(
                            f"Episode S{number:02d}E{episode['episode_number']:02d} requires manual mapping"
                        )
                    entries.append(
                        {
                            "url": matches[0]["url"],
                            "season": number,
                            "episode": episode["episode_number"],
                            "source_season": matches[0]["season_number"],
                            "source_episode": matches[0]["episode_number"],
                        }
                    )
        if identity["media_type"] == "tv":
            snapshot = seerr.get_request(request_id=request["request_id"])[
                "episode_review"
            ]
            snapshot["pending"] = pending
            seerr.update(request["request_id"], episode_review=snapshot)
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
