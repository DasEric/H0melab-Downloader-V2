# H0melab Downloader v2

[![Version](https://img.shields.io/badge/version-2.5.0-2563eb)](https://github.com/DasEric/H0melab-Downloader-V2/releases)
[![Tests](https://github.com/DasEric/H0melab-Downloader-V2/actions/workflows/tests.yaml/badge.svg)](https://github.com/DasEric/H0melab-Downloader-V2/actions/workflows/tests.yaml)
[![License](https://img.shields.io/github/license/DasEric/H0melab-Downloader-V2)](LICENSE)

H0melab Downloader is a browser-based media downloader maintained by [DasEric](https://github.com/DasEric). It combines multiple supported catalogue sites, a persistent download queue, a library, Auto-Sync, Seerr requests and Discord notifications, and a TMDB-backed availability watchlist in one responsive Web UI.

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
- Optional local login, OIDC SSO, API keys, and Discord notification bot
- Docker-first deployment with a non-root runtime user

## Requirements

### ZDF and DMAX (native integration)

Select **ZDF** or **DMAX** in the Web UI and search by title or paste a video/show
URL. Both accept show overviews and `?staffel=NUMBER` to select one season.
Both sources have independent enable/disable switches
and use the existing queue and output destinations. These new download paths use
native HTTP/HLS and FFmpeg, not yt-dlp. The pre-existing yt-dlp fallback for other
providers is unchanged.

- ZDF: current `/video/` and `/play/` URLs and legacy `.html` video URLs;
  standard German/English audio when offered, and optional German VTT/SRT subtitles.
- ZDF show pages: lazy season selection, full cursor pagination, duplicate filtering,
  and standalone-film overview links. Pagination failures are errors, never silently
  truncated successful lists. Up to 20,000 videos can be expanded in one request.
- DMAX: Aurora title search, show/season overviews, paginated episode catalogues,
  and native anonymous playback for available unprotected content. Use current
  episode slugs from the show overview; expired legacy slugs report an error.
- ZDF title search uses its public HbbTV results (up to 100 supplied by the source)
  and resolves real video/show URLs through GraphQL; it does not guess URL paths.
- Best available native HLS variant is selected automatically. No manual quality,
  audio-description or sign-language selector is currently exposed.
- Series episode numbers are taken from source metadata, never guessed. Standalone
  videos use a source ID in their default filename. Series use the naming template.
- Broadcaster Auto-Sync, account login and live recording are not included.
  Public VOD needs no user login; account-only, protected, geo-blocked or expired
  content reports a source error rather than a successful empty download.
- Pause preserves native HTTP/HLS partial state; FFmpeg-only fallback restarts on
  resume. Playback addresses are resolved again when the queue retries a download.

Live validation on 2026-11-20: complete native HLS-to-MKV downloads succeeded for
the 81-second ZDF Bergdoktor season-19 trailer (52,891,851 bytes) and DMAX's
"24 Stunden auf der Feuerwache 3" (2,632 seconds, 2,348,833,168 bytes).
Both outputs passed video/audio probing with German audio. These are representative
tests, not a guarantee that every source title is available.
The ZDF `heute journal` collection returned all 318 available 2026 episodes across
multiple pages in a separate live test. Source numbering gaps were preserved.
Live title searches returned 100 ZDF Bergdoktor results and three DMAX Feuerwache
shows. The tested DMAX Munich show returned eight available episodes, preserving
numbering gaps. The old Loma/Discovery endpoints have been replaced with Aurora.
Browser verification on 2026-11-21 also confirmed both search tabs, show details,
season/episode pickers and native provider selection. DMAX pagination was checked
against the live API with four two-item pages (eight unique episodes).

Protocol references (reimplemented against the project's own interfaces):
[ZDF extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/zdf.py),
[Discovery/DMAX extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/dplay.py).
Run `python -m pytest tests/test_broadcasters.py -q` for the isolated integration tests.
Run `python -m pytest tests/test_zdf_collections.py -q` for collection/pagination tests.
Run `python -m pytest tests/test_broadcaster_search.py -q` for search and Aurora tests.

### Runtime requirements

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

### Seerr and Discord

Seerr and the [AniWorld-Jellyfin-Connector](https://github.com/DasEric/AniWorld-Jellyfin-Connector) are both supported request front ends. Configure Seerr's authenticated webhook under **Settings → Integrations → Seerr**; see the detailed setup below. The Jellyfin connector uses a full-access API key and requires the movie/show destinations below. Discord request commands have been retired; Discord remains an optional notification transport.

### Movie/show destinations and grouped series

In **Settings → Custom Paths → Movie and show destinations**, select two existing, separate mounts. The detected content type then takes priority over site defaults for native downloads; Seerr uses its explicit movie/tv type with these same destinations. A show from a movie site goes to the show mount. Conflicting manual paths, unavailable mounts and changed queued destinations are reported rather than silently redirected. Without this setup, existing source defaults remain in use.

FilmPalast/Filmo episode pages such as `SiloS01E01` and `SiloS02E01` appear under one source-specific **Silo** card. Open it to verify the current seasons and episodes. A source failure or incomplete page list is not presented as a complete show. Different providers and remakes remain separate; alternative hosters for the same episode do not become duplicate episode downloads.

MegaKino season pages use the season stated in the title, including `Staffel 2` and `4 Staffel`, even when the page is listed under a film genre. Seasons of `Tulsa King`, for example, share one `Tulsa King` folder with separate `Season 02`, `Season 04`, etc. The date of an individual season is not used as the show's premiere year. If the source omits or contradicts the episode numbering, the request stops instead of putting the show in the movie library.

Native manually selected videos are written under `Show/Season 01/Show - S01E01 - german-dub.mkv` or a movie's own folder. Verified years are included when known. Variants remain separate filenames, without an extra language-level library folder. Point Jellyfin/Plex movie and show libraries at the corresponding roots. No Plex account connection is required for this layout.

A show request keeps its episodes together in the queue, with resumable 500-entry work sections up to 20,000 entries. Existing media is never automatically moved. Review misplaced files and destination conflicts separately, and rescan both libraries after an explicitly reviewed move. Finish or cancel new queue entries before downgrading.

Seerr uses its separate canonical naming convention with flat movie files and `Show/Season 01/Show S01E01.ext`; native naming remains unchanged. All Seerr identity checks and download execution share the existing queue and providers. Keep the downloader SQLite database and persistent settings when upgrading.

OIDC SSO, the Discord notification bot, API keys, custom output paths, and custom themes are optional. Enable only the integrations used by your deployment and keep all associated secrets in the persistent settings volume.

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
- For MegaKino season-loading errors on older builds, update the downloader to **2.4.0 or newer** and reopen the title. Existing misplaced downloads are not moved automatically.

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

## Seerr-Integration (2.4.0)

Seerr nimmt Benutzeranfragen entgegen; Discord dient nur noch Benachrichtigungen.
Seit 2.5.0 ist der AniWorld-Jellyfin-Connector wieder parallel nutzbar. Die in 2.4.0
in SQLite `migration_archive` archivierten Connector-Belege und pausierten
Connector-Aufträge werden dabei verlustfrei wiederhergestellt.

### Einrichtung

1. Unter **Einstellungen → Eigene Pfade / Film- und Serienziele** zwei vorhandene,
   getrennte und erreichbare Mounts wählen. Seiten-Defaults steuern Seerr nicht.
2. Unter **Einstellungen → Integrationen** den TMDB-Key und ein zufälliges
   Seerr-Webhook-Secret mit 32–256 ASCII-Zeichen speichern, dann Seerr aktivieren.
3. In Seerr unter Notifications → Webhook die URL
   `https://DOWNLOADER/api/integrations/seerr/webhook` eintragen.
4. Authorization Header: `Bearer <Webhook-Secret>`. Benachrichtigungen
   **Media Approved** und **Media Auto Approved** aktivieren.
5. Diese benutzerdefinierte JSON-Vorlage verwenden:

```json
{
  "notification_type": "{{notification_type}}",
  "subject": "{{subject}}",
  "media_type": "{{media_type}}",
  "media_tmdbid": "{{media_tmdbid}}",
  "media_imdbid": "{{media_imdbid}}",
  "media_tvdbid": "{{media_tvdbid}}",
  "request_id": "{{request_id}}",
  "requestedBy_username": "{{requestedBy_username}}",
  "{{extra}}": []
}
```

Die spezielle `{{extra}}`-Eigenschaft wird von Seerr mit zusätzlichen Angaben,
insbesondere den angefragten Staffeln, gefüllt. Serien ohne explizite Staffeln
werden zur Prüfung zurückgestellt; es werden nicht automatisch alle geladen.
**Test Notification** in Seerr prüft Erreichbarkeit und Authentifizierung, ohne
Download. Der lokale Testbutton prüft nur gespeicherte Konfiguration und Ziele.

Konfigurierte Payloads dürfen zusätzlich `original_title`/`originalTitle`,
`release_year`/`releaseYear`/`year` und `release_date`/`releaseDate` enthalten,
flach oder unter `media`. Datumformat: `YYYY-MM-DD`. Widersprüchliche Werte und
ungültige Datumsangaben werden abgewiesen. Diese optionalen Angaben werden als
Webhook-Metadaten gespeichert; die unabhängige TMDB-Prüfung bleibt maßgeblich.
Es werden hierfür keine zusätzlichen Seerr-Templatevariablen vorausgesetzt.

### Identifikation und Verarbeitung

Die TMDB-ID und der explizite Typ `movie`/`tv` bestimmen die Identität. IMDb wird
über die offiziellen TMDB-Endpunkte `external_ids` und `find` abgeglichen; es
findet kein IMDb-Webscraping statt. Ein separater IMDb-API-Vertrag ist nicht nötig.
Anbieter-IDs haben Vorrang vor Titelähnlichkeit. Widersprüche führen nie zu einer
automatischen Auswahl. Ohne IDs werden bestätigter Typ, exakter Titel/Alias und
Premierejahr verlangt; mehrere geeignete Treffer benötigen manuelle Auswahl.
Falls ein Anbieter ein vollständiges Erscheinungsdatum liefert, wird es ebenfalls
geprüft. Abweichende Daten (auch mögliche regionale Veröffentlichungstermine)
werden nicht still gleichgesetzt, sondern benötigen Klärung. Fehlende Datumsangaben
erzeugen keinen erfundenen Konflikt; ein Datum allein beweist keine Identität.
Unmittelbar vor der Dateiausgabe werden Anbieteridentität und konkrete Folge erneut
geprüft. Geänderte Episodentitel, Quellnummern oder Serien-IDs führen zurück zur
manuellen Prüfung; eine frühere Freigabe gilt nicht für ausgetauschte Inhalte.

Seerr-Aufträge laufen durch die vorhandene Download-Queue und Provider-Engine.
Wiederholte Webhooks mit derselben Anfrage-ID erzeugen keinen zweiten Auftrag.
Unter **Seerr** werden Metadaten, Prüfergebnisse, Queuefortschritt, Ziel und Fehler
angezeigt. In der manuellen Prüfung kann ein gespeicherter Treffer mit Begründung
freigegeben werden; der Worker prüft ihn erneut. Explizite Episodenzuordnungen
können anhand der angezeigten TMDB-Episoden und Anbieterfolgen über Auswahlfelder bestätigt werden. Alternativ ist eine JSON-Liste aus URL, `season_number` und `episode_number` möglich. Bei abweichender Staffelorganisation benennt das optionale Feld `source_season_number` die Quellstaffel; heruntergeladen werden weiterhin nur die angefragten kanonischen Episoden.
Fremde Serien-IDs werden auch dadurch nicht akzeptiert.

Filme liegen direkt im Filmziel als `Titel (Jahr).ext`. Serien verwenden eine
stabile TMDB-gebundene Hauptordnerzuordnung und
`Serienname/Season 01/Serienname S01E01.ext`. Die Dateiendung bleibt erhalten.
Identitätsbelege erkennen bereits fertiggestellte Dateien, auch nach Änderungen
der Zielkonfiguration. Zusätzlich werden bestehende Medien im gewählten Ziel anhand
lokaler [Jellyfin-NFO-Dateien](https://jellyfin.org/docs/general/server/metadata/nfo/)
erkannt: Filme benötigen eine passende TMDB-/IMDb-ID in der gleichnamigen NFO
(oder eine eindeutige einzelne Videodatei neben `movie.nfo`). Serien benötigen eine
passende ID in `tvshow.nfo` sowie explizite Staffel-/Episodennummern in der NFO
zur jeweiligen Videodatei. Widersprechende IDs, bloße Titel/Dateinamen, XML-DTDs,
verlinkte Pfade und leere Dateien werden nicht als Identitätsnachweis übernommen.
Der Scan ist auf 20.000 Einträge und 1 MiB je NFO begrenzt; eine Überschreitung
pausiert die Anfrage zur Prüfung. Ein bereits belegter Film benötigt keine
verfügbare Anbieterquelle. Bestehende Dateien ohne passenden Beleg werden nicht
überschrieben und erfordern Prüfung. Bestehende Medien werden nicht verschoben.

Die Download-Queue wird pro Datenbank durch einen exklusiven Betriebssystem-Dateilock
verarbeitet. Weitere Webprozesse warten, ohne laufende Aufträge zurückzusetzen.
Nach Prozessabbruch gibt das Betriebssystem den Lock frei; der nächste Worker
übernimmt zurückgebliebene Aufträge. Bereits angeforderte Stornierungen bleiben
storniert, Pausen bleiben pausiert. Die SQLite-Datenbank und die zugehörige
`.worker.lock` müssen für alle Prozesse über denselben lokalen Datenpfad erreichbar
sein; die Lockdatei während des Betriebs nicht löschen oder ersetzen.

Bei teilweise verfügbaren Staffeln werden eindeutig bestätigte Folgen verarbeitet.
Noch nicht ausgestrahlte Folgen (TMDB-Ausstrahlungsdatum in der Zukunft) und fehlende
Anbieterfolgen bleiben einzeln mit Grund in der Seerr-Übersicht sichtbar. Der Status
`partial` hält den bestehenden Queueauftrag pausiert; er meldet keine vollständige
Staffel. Über **Metadaten und Anbieter erneut prüfen** werden später nur die noch
fehlenden Folgen ergänzt. Mehrdeutige oder widersprüchliche Episodentreffer bleiben
vor jeglichem Download in der manuellen Prüfung. Sind alle angefragten Folgen
bereits durch Identitätsbelege vorhanden, ist keine Anbieterquelle erforderlich.


### Docker und Fehlerdiagnose

Kein weiterer Container wird benötigt. Bestehende Konfigurations-/SQLite-Volumes
und Medienmounts beibehalten. Optional können vor dem Start
`H0MELAB_SEERR_ENABLED=1`, `H0MELAB_SEERR_WEBHOOK_SECRET` und
`H0MELAB_TMDB_API_KEY` gesetzt werden. Secrets nicht in Images oder öffentliche
Compose-Dateien aufnehmen. In der WebUI gespeicherte Einstellungen bleiben im
persistenten Konfigurationsvolume erhalten; Secrets fehlen im Konfigurationsexport.

- HTTP 401: Authorization-Header/Secret prüfen (kein normaler Downloader-API-Key).
- HTTP 503: Seerr-Integration ist deaktiviert.
- HTTP 400/409: Payload bzw. widersprüchliche Wiederverwendung einer Anfrage-ID prüfen.
- Metadatenprüfung fehlgeschlagen: TMDB-Key, API-Erreichbarkeit und IDs prüfen.
- Manuelle Prüfung: Kandidaten und Staffel-/Episodeninformationen vergleichen.
  Ändern sich bereits freigegebene Anbieter-Metadaten, verfällt die alte Freigabe
  einschließlich Episodenzuordnung. Aktuelle Kandidaten werden angezeigt; dieser
  Versuch lädt nichts herunter. Prüfe und bestätige die aktuelle Auswahl erneut.
- Fehlendes Ziel: Mounts und zentrale Film-/Serienzielkonfiguration kontrollieren.

Offizielle Referenzen: [Seerr Webhooks](https://docs.seerr.dev/using-seerr/notifications/webhook/),
[TMDB](https://developer.themoviedb.org/docs),
[Jellyfin Serien](https://jellyfin.org/docs/general/server/media/shows/).

### Vorübergehende TMDB-Ausfälle

Nach den begrenzten HTTP-Versuchen werden vorübergehende Netzwerkfehler, HTTP 429
und HTTP 5xx in derselben Download-Queue nach 30, 120 und 600 Sekunden erneut
geprüft. Andere Aufträge dürfen währenddessen weiterlaufen. Nach drei geplanten
Wiederholungen erfolgt manuelle Prüfung. Ungültige IDs, widersprüchliche Metadaten
und ungültige Zugangsdaten lösen diese Wiederholungen nicht aus. Die Anfrageansicht
zeigt Anzahl und nächsten Termin; ein expliziter manueller Retry setzt den Zähler zurück.


Fehlgeschlagene Anbieter-Suchantworten werden ebenfalls höchstens dreimal mit
30/120/600 Sekunden Abstand geprüft. Der Status `provider_retry` unterscheidet
sie von TMDB-Ausfällen. Der Wiederholungszähler gilt gemeinsam je Anfrage;
wechselnde Fehlerquellen starten keine endlose neue Retry-Serie. Eine unvollständige
Suche wird nicht als eindeutiger Treffer interpretiert. Dauerhaft nicht verfügbare
Quellen führen danach zu `provider_failed` und manueller Prüfung.


Discord-Benachrichtigungen sind Best-Effort mit dauerhafter Ereignis-Deduplizierung:
Auch parallele Wiederholungen derselben Anfrage/des gleichen Ereignisses planen
höchstens eine Nachricht ein. Unterschiedliche Episodenabschlüsse bleiben getrennte
Ereignisse. Ist Discord deaktiviert oder schlägt der Transport fehl, läuft die
Download-Verarbeitung unverändert weiter. Verpasste Nachrichten werden nicht durch
Webhook-Replays erneut gesendet; der persistente Anfragestatus in der WebUI bleibt
die maßgebliche Informationsquelle, nicht eine Zustellbestätigung von Discord.


Die Seerr-Anfrageübersicht lädt 50 Einträge je Seite. **Weitere Anfragen laden**
zeigt ältere Einträge, auch offene manuelle Prüfungen jenseits der ersten 200
Anfragen. **Aktualisieren** beginnt wieder bei den neuesten Einträgen. Der
Admin-Endpunkt unterstützt `offset` und `limit` (1–200) und liefert `next_offset`.


Ein Zielwechsel während eines Transfers verschiebt keine begonnene Datei: Ihr
bereits geprüfter Ausgabepfad bleibt bis zum Abschluss gebunden. Die nächste
Episode löst das dann aktuelle zentrale Serienziel erneut auf. Fertige Dateien
und ihre Identitätsbelege bleiben am bisherigen Ort erhalten; ein Zielwechsel
kopiert oder verschiebt keinen Bestand automatisch.


Existiert der geplante Dateiname bereits ohne passenden Identitätsbeleg, pausiert
der Seerr-Auftrag mit `manual_review`. Die Datei bleibt unverändert. Ein Retry
gibt keine Erlaubnis zum Überschreiben; vor einem erneuten Versuch muss der
Bestandskonflikt geklärt sein.


### Jellyfin: passende Bibliothek nach Download aktualisieren

Unter **Einstellungen → Integrationen → Jellyfin-Bibliotheksscan** die aus dem
Downloader erreichbare Server-URL (einschließlich eines optionalen Base-Paths),
einen Jellyfin-API-Key und die beiden Bibliothek-IDs speichern. Im Container ist
`localhost` der Downloader selbst; verwende z. B. `http://jellyfin:8096` im gemeinsamen
Docker-Netz. In Jellyfin einen API-Key unter Dashboard → API-Schlüssel erstellen.

Einfacher: **Bibliotheken laden** anklicken (funktioniert auch vor dem Speichern mit
dem gerade eingegebenen API-Key), Film- und Serienbibliothek nach Namen auswählen
und **Jellyfin speichern**. Ein leeres Key-Feld nutzt den bereits gespeicherten Key.
Der Abruf allein speichert keine Einstellungen und zeigt nur passende Bibliothekstypen.

Für manuelle Umgebungskonfiguration: Die IDs sind `ItemId` aus `GET /Library/VirtualFolders` (Header
`X-Emby-Token: <API-Key>`), **keine Bibliotheknamen oder Dateipfade**. Die
Filmbibliothek muss `CollectionType: movies`, die Serienbibliothek `tvshows` haben.
Beide Bibliotheken müssen die entsprechenden Medienmounts in Jellyfin sehen.

Nach jeder neu erfolgreich fertiggestellten Filmdatei oder Serienepisode im
bestehenden Worker (native Downloads und Seerr) fordert der Downloader ausschließlich
für die passende konfigurierte Bibliothek `POST /Items/{ItemId}/Refresh` an.
Es erfolgt **kein globaler Bibliotheksscan**, kein Ersetzen bestehender Bilder oder
Metadaten und kein Scan für übersprungene Bestandsdateien, fehlgeschlagene Downloads
oder unbekannte Medientypen. Bei einem teilweise erfolgreichen Serienauftrag werden
bereits fertiggestellte Folgen trotzdem sichtbar. Jellyfin führt den Scan asynchron aus;
eine akzeptierte Anfrage ist noch keine Bestätigung der fertigen Indizierung.

Die Bibliothekszuordnung wird vor jedem Scan mit `/Library/VirtualFolders` geprüft.
HTTP-Weiterleitungen werden nicht verfolgt; korrigiere bei Redirects die Server-URL.
Die beiden HTTP-Aufrufe haben jeweils 10 Sekunden Timeout. Scanfehler werden ohne
Token/URL protokolliert und verändern den Downloadstatus nicht. Es gibt keine
persistente Wiederholungsqueue für Scans: bei einem Jellyfin-Ausfall später manuell
in Jellyfin scannen. Leere Server-URL deaktiviert die Integration; der API-Key kann
über die separate Löschoption entfernt werden und wird weder zurückgegeben noch exportiert.

Alternativ per Umgebung konfigurieren:
```dotenv
H0MELAB_JELLYFIN_URL=http://jellyfin:8096
H0MELAB_JELLYFIN_API_KEY=
H0MELAB_JELLYFIN_MOVIE_LIBRARY_ID=
H0MELAB_JELLYFIN_SERIES_LIBRARY_ID=
```

API-Referenz: [Jellyfin ItemRefreshController](https://github.com/jellyfin/jellyfin/blob/v10.11.0/Jellyfin.Api/Controllers/ItemRefreshController.cs).

### Produktionsabnahme nach dem Upgrade

Die lokalen Regressionen verwenden isolierte SQLite-Datenbanken, Dateien und
kontrollierte Providerantworten. Die Tests auf deinen Produktionssystemen führst du
wie vereinbart selbst durch:
1. Konfigurationsvolume und SQLite sichern; Medienmounts und Schreibrechte prüfen.
2. Seerr-Testbenachrichtigung senden; dann je eine genehmigte Film-/Serienanfrage.
3. Zielpfade, Identitätsstatus und Dateinamen prüfen; denselben Webhook erneut senden
   und sicherstellen, dass keine zweite Arbeit entsteht.
4. Jellyfin-IDs konfigurieren: Film muss nur die Filmbibliothek, Episode nur die
   Serienbibliothek aktualisieren. Bei Jellyfin-Ausfall muss der Download erfolgreich bleiben.
5. Discord-Meldungen, manuelle Prüfung und Retry testen. Erst danach regulär nutzen.
Docker-Build/-Start, echte Provider-/Seerr-/Jellyfin-/Discord-Kompatibilität und
Browserrendering sind nicht als hier durchgeführte Live-Abnahme ausgewiesen.


### Seerr-Secret automatisch erzeugen und austauschen
Beim Öffnen der Integrationseinstellungen wird nur dann ein Secret-Entwurf erzeugt,
wenn noch keines gespeichert ist. Der Browser nutzt `crypto.getRandomValues` für
32 zufällige Bytes (64 Hex-Zeichen). Bestehende Secrets bleiben unverändert.
**Secret kopieren**, danach **Speichern** und in Seerr als `Bearer <Secret>` eintragen.
**Neu generieren** erzeugt einen neuen Entwurf; eigene Secrets bleiben ebenfalls möglich.
Beim Speichern eines Ersatzes verliert das alte Secret seine Gültigkeit. Aktualisiere
dann den Header in Seerr. Gespeicherte Secrets werden nicht wieder ausgelesen.
Bei HTTP ohne Clipboard-Unterstützung markiert die Kopierfunktion den Entwurf für Strg+C.
