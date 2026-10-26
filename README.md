# H0melab Downloader v2

[![Version](https://img.shields.io/badge/version-2.3.1-2563eb)](https://github.com/DasEric/H0melab-Downloader-V2/releases)
[![Tests](https://github.com/DasEric/H0melab-Downloader-V2/actions/workflows/tests.yaml/badge.svg)](https://github.com/DasEric/H0melab-Downloader-V2/actions/workflows/tests.yaml)
[![License](https://img.shields.io/github/license/DasEric/H0melab-Downloader-V2)](LICENSE)

H0melab Downloader is a browser-based media downloader maintained by [DasEric](https://github.com/DasEric). It combines multiple supported catalogue sites, a persistent download queue, a library, Auto-Sync, Discord requests, and a TMDB-backed availability watchlist in one responsive Web UI.

> This project is a continuation of the original **AniWorld Downloader** by [Phoenixthrush](https://github.com/phoenixthrush/AniWorld-Downloader) and its contributors. The provider integrations and a substantial part of the downloader core originate from that project. H0melab Downloader keeps that attribution while providing a new product identity and browser-only workflow.

## Highlights

- Responsive Web UI with desktop sidebar and mobile drawer
- Film, series, anime, manga, and library workflows
- Persistent queue with total and current-episode progress
- Configurable parallel HLS transfer count
- Optional German soft subtitles for SerienStream, remuxed for Plex and Jellyfin
- Auto-Sync for explicitly selected series
- TMDB availability watchlist for upcoming **and already released** films or series
- Strict automatic matching: direct TMDB identity or exact title and year
- Configurable availability checks from once daily to hourly
- Optional local login, OIDC SSO, API keys, and Discord request bot
- Docker-first deployment with a non-root runtime user

## Requirements

- Docker with Docker Compose (recommended), or Python 3.11 or newer
- FFmpeg for downloading and combining media streams
- A modern browser for the Web UI
- Optional: a TMDB API key for the availability watchlist

## Supported catalogues

| Catalogue | Content | Default |
| --- | --- | --- |
| AniWorld | Anime and anime films | Enabled |
| SerienStream | Series | Enabled |
| MegaKino | Films and series | Enabled |
| FilmPalast / Filmo | Films | Enabled |
| MangaFire | Manga | Enabled |
| Moflix | Films and series | Enabled |
| Cineby | Films and series | Disabled |
| Kinox | Films and series | Disabled |
| BurningSeries | Series | Disabled |
| Hanime | Adult animation | Disabled |

Every catalogue can be enabled or disabled under **Settings**. Availability, captcha requirements and individual stream providers can change independently of this project.

## Docker Compose

```bash
git clone https://github.com/DasEric/H0melab-Downloader-V2.git
cd H0melab-Downloader-V2
mkdir -p Downloads
docker compose up -d
```

Open `http://localhost:8080`.

The included Compose file deliberately keeps the old `aniworld-data` Docker volume name. Existing installations therefore retain their users, API keys, queue, watchlist, themes, secrets, and settings when they pull v2 and recreate the container.

Useful operating commands:

```bash
docker compose logs -f
docker compose pull
docker compose up -d
docker compose down
```

To build the image from the checked-out source, replace `image:` in `docker-compose.yaml` with `build: .` and run `docker compose up -d --build`.

## Upgrade from v5

The first v2 start performs a non-destructive migration:

1. `~/.aniworld` is copied to `~/.h0melab-downloader` when the new directory does not exist.
2. The SQLite database is copied with SQLite's backup API to `h0melab.db`; the original `aniworld.db` remains as a rollback copy.
3. Existing movie watchlist rows are copied into the new film-and-series schema with their IDs and queue links intact.
4. Existing `ANIWORLD_*` values are mapped to `H0MELAB_*`; v2 names win when both are present.
5. Unknown/custom `.env` entries are preserved.
6. `.web-settings.env`, authentication secrets, themes, browser profile, downloads, custom paths, Auto-Sync state, and Discord settings remain in the persistent data directory.

Back up the persistent volume before any production upgrade. Do not delete the old data directory or volume until the migrated instance has been verified.

## Configuration

Settings can be changed in the browser. A fully documented template is available at [`src/h0melab/.env.example`](src/h0melab/.env.example).

Frequently used variables:

```dotenv
H0MELAB_DOWNLOAD_PATH=Downloads
H0MELAB_UI_LANGUAGE=de
H0MELAB_HLS_CONCURRENCY=8
H0MELAB_TMDB_API_KEY=
H0MELAB_UPCOMING_CHECKS_PER_DAY=1
H0MELAB_WEB_AUTH=0
```

`H0MELAB_HLS_CONCURRENCY` is read when the next episode begins. Changing it never modifies an episode that is already running.

## Availability watchlist

1. Add a TMDB API key under **Settings → Integrations**.
2. Choose how many checks should run per day (1–24).
3. Open **Demnächst**, search for a film or series, and add it.
4. The scheduler searches every enabled compatible site after the release date, or immediately for already released and unknown-date titles.
5. Only an exact title/year match or direct TMDB match is queued. Ambiguous matches stay on the list.

## Theming

Administrators can import or edit a theme under **Settings → Appearance**. The shipped [`themes/template.css`](themes/template.css) documents every supported token and surface; [`themes/light.css`](themes/light.css) is a complete example. Store remote styles on a host that serves CSS with the correct content type.

## Optional integrations

### Jellyfin connector

Use AniWorld Requests **1.5.0 or newer** with H0melab Downloader **2.3.1 or newer**. Create a full-access API key, then configure the connection in the Jellyfin plugin. Assign the central movie and show destinations to the corresponding Jellyfin libraries in the plugin settings; it verifies that the selected paths share the same storage even when container paths differ.

### Movie/show destinations and grouped series

In **Settings → Custom Paths → Movie and show destinations**, select two existing, separate mounts. The detected content type then takes priority over site defaults for both native downloads and connector requests. A show from a movie site goes to the show mount. Conflicting manual paths, unavailable mounts and changed queued destinations are reported rather than silently redirected. Without this setup, existing source defaults remain in use.

FilmPalast/Filmo episode pages such as `SiloS01E01` and `SiloS02E01` appear under one source-specific **Silo** card. Open it to verify the current seasons and episodes. A source failure or incomplete page list is not presented as a complete show. Different providers and remakes remain separate; alternative hosters for the same episode do not become duplicate episode downloads.

MegaKino season pages use the season stated in the title, including `Staffel 2` and `4 Staffel`, even when the page is listed under a film genre. Seasons of `Tulsa King`, for example, share one `Tulsa King` folder with separate `Season 02`, `Season 04`, etc. The date of an individual season is not used as the show's premiere year. If the source omits or contradicts the episode numbering, the request stops instead of putting the show in the movie library.

Videos are written under `Show/Season 01/Show - S01E01 - german-dub.mkv` or a movie's own folder. Verified years are included when known. Variants remain separate filenames, without an extra language-level library folder. Point Jellyfin/Plex movie and show libraries at the corresponding roots. No Plex account connection is required for this layout.

A show request keeps its episodes together in the queue, with resumable 500-entry work sections up to 20,000 entries. Existing media is never automatically moved. Review misplaced files and destination conflicts separately, and rescan both libraries after an explicitly reviewed move. Finish or cancel new queue entries before downgrading.

Connector requests use the actual film/show type rather than a source's default destination. Language and provider options are checked per episode; partial language availability requires an explicit selection. Connector language variants use separate filenames and do not remux another request's existing file.

Completed connector files have persistent receipts, so Jellyfin users can remove only their own newly created files. Requesting an existing file does not transfer ownership, and clearing the queue does not remove receipts. Older/manual downloads remain administrator-managed. Keep the downloader database and the connector's persistent data when upgrading.

OIDC SSO, the Discord request bot, API keys, custom output paths, and custom themes are optional. Enable only the integrations used by your deployment and keep all associated secrets in the persistent settings volume.

## API

The Web UI uses the JSON API exposed by the same service. Administrators can create scoped API keys under **Settings → API keys**. Send a key using the `X-API-Key` header:

```bash
curl -H "X-API-Key: h0d_yourkey" http://localhost:8080/api/queue
```

Available scopes are read-only, download access and administrator access. API keys never expose stored TMDB, Discord or OIDC secrets. The current endpoint list and examples are shown directly on the settings page so they match the installed version.

## Updating

```bash
git pull --rebase
docker compose pull
docker compose up -d
```

For a source installation, activate the virtual environment and run `pip install -e ".[all,test]"` again after updating. Keep a backup of the persistent configuration directory or Docker volume before a major-version upgrade.

## Troubleshooting

- Check `docker compose logs -f` or the terminal output first.
- Verify that the download directory is writable by the container or service account.
- Confirm that FFmpeg is installed when running without Docker.
- If a custom theme hides controls, open `/settings?nocss=1` and remove the theme.
- If a catalogue requires a captcha, configure its browser/captcha options under **Settings**.
- A title on the availability watchlist is queued only after the configured language and a usable provider are available.
- For MegaKino season-loading errors on older builds, update the downloader to **2.3.1 or newer** and reopen the title. Existing misplaced downloads are not moved automatically.

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[all,test]"
h0melab-downloader
```

Run the test suite with:

```bash
python -m pytest -q
```

## Security

- Run the service only on a trusted network or enable authentication.
- Put public deployments behind HTTPS and set `H0MELAB_WEB_BASE_URL`.
- TMDB, Discord, and OIDC secrets are masked by the API and excluded from settings exports.
- JSON write endpoints reject non-JSON bodies; authenticated deployments use secure session settings and CSRF protection for browser forms.
- The Docker image runs as an unprivileged user.

## Credits and license

H0melab Downloader is developed and maintained by [DasEric](https://github.com/DasEric) in the repository [DasEric/H0melab-Downloader-V2](https://github.com/DasEric/H0melab-Downloader-V2).

Original project and creator credit: [Phoenixthrush/AniWorld-Downloader](https://github.com/phoenixthrush/AniWorld-Downloader), Phoenixthrush, Sirox, and all upstream contributors. Provider names and URLs remain their real external service names and are not part of the H0melab product branding.

Licensed under the [MIT License](LICENSE).
