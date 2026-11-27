"""Bounded source failover for one canonical movie or episode at a time."""

from . import db, seerr, seerr_files, seerr_matching, seerr_processing


def alternatives(item, entry):
    """Resolve alternative episodes independently; never reuse another site's numbering."""
    request = seerr.get_request(queue_id=item["id"])
    identity = entry["_seerr_identity"]
    seen = {entry["url"], entry.get("source_url") or request.get("selected_url")}
    # Next best sources in priority order (SerienStream first for series).
    for hit in seerr_matching.rank_candidates(identity, request.get("candidates", [])):
        url = hit["url"]
        if url in seen:
            continue
        seen.add(url)
        if seerr_processing.stopped(item):
            return
        alternative = dict(entry)
        alternative.pop("source_url", None)
        evidence = {
            **request,
            "selected_url": url,
            "review_reason": ""
            if seerr_matching.assess_candidate(identity, hit)["eligible"]
            else seerr_matching.AUTO_REASON,
            "episode_mapping": [],
        }
        if identity["media_type"] == "movie":
            alternative["url"] = url
        else:
            canonical = [
                row
                for row in request.get("episode_review", {}).get("canonical", [])
                if row.get("season_number") == entry["season"]
                and row.get("episode_number") == entry["episode"]
            ]
            if len(canonical) != 1:
                continue
            try:
                rows = seerr_processing.episode_rows(hit, [entry["season"]])
            except Exception:
                rows = []
            wanted = {**canonical[0], "series_tmdb_id": identity["tmdb_id"]}
            rows = [
                row
                for row in rows
                if seerr_processing._has_language(row, item.get("language"))
            ]
            matches = [
                row
                for row in rows
                if seerr_matching.assess_episode(identity, wanted, row)["eligible"]
            ] or [
                row
                for row in rows
                if seerr_matching.episode_acceptable(identity, wanted, row)
            ]
            if not matches or not matches[0].get("url"):
                continue
            row = matches[0]
            alternative.update(
                url=row["url"],
                source_season=row["season_number"],
                source_episode=row["episode_number"],
            )
            evidence["episode_review"] = {"canonical": canonical, "source": [row]}
        yield alternative, evidence


def download_entry(item, entry, selected_path, index):
    """Try each verified source once; completed entries and controls take precedence."""
    from ..models.common.common import DownloadPaused
    from .worker import _build_episode, _episode_request

    def attempts():
        yield entry, None
        yield from alternatives(item, entry)

    last_error = None
    for candidate, evidence in attempts():
        if seerr_processing.stopped(item):
            return None
        if seerr_files.existing(entry):
            return True
        url, extra = _episode_request(candidate)
        db.update_queue_progress(item["id"], index, url)
        try:
            _provider, episode = _build_episode(url, extra, item, selected_path)
            if evidence is None:
                return seerr_files.download(item, candidate, episode)
            return seerr_files.download(
                item, candidate, episode, source_request=evidence
            )
        except DownloadPaused:
            raise
        except Exception as exc:
            last_error = exc
    if seerr_processing.stopped(item):
        return None
    if last_error is not None:
        raise last_error
    raise seerr_matching.IdentityReviewRequired(
        "No verified source remains for this episode"
    )
