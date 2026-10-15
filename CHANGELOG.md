# Changes

## 2.1.0

- Add category selection to the video/manga homepage and the book/audio catalog. Only sources of the chosen category are offered; category navigation preserves the deployment's URL prefix.
- Add authenticated, read-only `GET /api/sources` capability metadata (`schema_version: 1`) for the Jellyfin connector. Existing API endpoints remain available.
- Add category-aware interactive video search and browse responses with explicit source and media type. Uncertain mixed-source entries are classified from their metadata rather than assigned the requested type.
- Preserve relevant media identity fields through search normalization. Full meaningful query words must match title/known aliases, or author for catalogs; mismatched explicit years are rejected. Watchlist exact-identity matching is unchanged.
- Keep movie, series and manga results separate. Fix the “gravity falls” case by excluding unrelated single-word/full-text provider hits.
- Ignore stale search/detail responses after query, source, category or dialog changes.
- Add optional `request_key` idempotency for video and catalog queue submissions. Existing queue rows and clients without a request key retain their behavior; the database adds a nullable column and unique index.
- Expose an exact-asset `downloaded` flag in catalog details, separate from Jellyfin availability.
- Resolve the runtime/UI version consistently from the current checkout or installed wheel, avoiding stale editable-install metadata after an update.

### Release and upgrade

Deploy this version before Jellyfin connector **1.3.0**. Back up the persistent configuration/database first and keep the old program version together with its data backup.

Tests run on pushes and pull requests. Docker publication follows the existing **published GitHub release** event: push the commit and tag `v2.1.0`, then publish its GitHub release. A tag push alone runs tests but does not publish the Docker image. Wait for the image build to finish before updating the running downloader and releasing the connector.

The Jellyfin E-Book, audio book and podcast switches belong to the connector; they do not globally disable the downloader's own catalog pages. Existing video-site enable switches remain authoritative. A source being enabled does not guarantee that the external site is currently reachable.

For a rollback, stop new submissions and use the matching program/data backup. Check queue IDs and already downloaded files before resubmitting requests. External downloads are not reversed by replacing application files.
