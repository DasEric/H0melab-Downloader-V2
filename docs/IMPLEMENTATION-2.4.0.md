# Version 2.4.0: Bestandsaufnahme und Implementierungsplan

## Auftrag und Ausgangspunkt
Der vollständige Nutzerauftrag ist aktives Ziel. Vorrangige Präzisierung: BurningSeries, Kinox und Cineby vollständig entfernen; alle übrigen Anbieter einschließlich vorhandener ZDF/DMAX-Arbeit erhalten. Keine neue Engine oder Download-Queue.

Vor Änderungen wurden 294 getrackte/ungetrackte Arbeitsdateien einschließlich vorhandener Nutzeränderungen in `../H0melab-Downloader-V2-evidence-240/BASELINE.zip` gesichert; SHA256-Manifest liegt daneben. Der vollständige Baseline-Test läuft separat.

## Bestandsaufnahme
- Python/Flask, SQLite, vorhandener Single-Worker mit `download_queue` und providerabhängigem `episode.download()`; native Katalog-/Manga-/Videoverarbeitung bleibt.
- `providers.py`, `search.py`, `web/sitesearch.py`, `views/api_media.py` bilden Suche, Providerauflösung und Episodenlisten ab. AniWorld/SerienStream liefern bereits IMDb und Jahr; Normalisierung verwirft teils externe IDs.
- `web/tmdb.py` ist der zentrale Fixed-Origin-Client, bisher ohne externe IDs/Find/Staffeln. Vorhandene TMDB-Einstellung und Secretmaskierung bleiben.
- `web/media_targets.py` verwaltet bestehende getrennte Film-/Serienmounts mit Revision und Validierung. Seerr bildet tv explizit auf internes series ab und liest aktuelle Ziele, unabhängig von Site-Defaults.
- `web/video_library.py` speichert providerbezogene Seriengruppen und Dateireservierungen. Seerr benötigt kanonische TMDB-ID, stabile Serienordner und eigenes verbindliches Layout ohne bestehende Dateien zu verschieben.
- `connector_files.py` enthält neben exklusiven Connector-Receipts gemeinsame Locks und sichere Dateipfade, die fast alle Videoprovider verwenden. Gemeinsame Funktionen neutral extrahieren. `api_connector.py` enthält zudem benötigte media-targets-Endpunkte.
- Queue-Status CHECK erlaubt queued/running/completed/failed/cancelled. Identitäts-/Anfragestatus separat speichern. Queue request_key hat UNIQUE/BEGIN IMMEDIATE, reicht wegen History-Löschung nicht allein für dauerhafte Webhook-Idempotenz.
- API-Auth prüft Bearer bereits global: Seerr-Webhook gezielt ausnehmen und eigenes Secret prüfen; Adminschutz für Konfiguration/Review bewahren.
- Discord ist momentan auch Anfragebot. Benachrichtigungen erhalten, Request-Kommandos entfernen, damit Seerr einzige Benutzer-Anfrageplattform ist. Native administrative Downloads/Watchlist bleiben bestehen.
- Docker verwendet persistente Konfiguration/SQLite und bestehende Mounts; kein weiterer Container nötig.
- Umfangreiche isolierte pytest-Suite mit Netzwerkblockade; aktuelle Nutzeränderungen betreffen Provider/Suche/UI/Worker und werden nicht zurückgesetzt.

## Technische Umsetzung in Reihenfolge
1. Bestandsaufnahme und externe Schnittstellen prüfen (dieses Dokument).
2. Alt-Connector-Endpunkte und exklusives Request-Protokoll entfernen; gemeinsame Dateihilfen/Medienziele neutral erhalten. Historische Download-Daten erhalten; exklusive Receipt-Daten bei Migration sichern statt unkontrolliert verwerfen.
3. Seerr-Webhook mit konstantzeitlichem Secretvergleich, Größen-/Typvalidierung, Approval-Allowlist, Testevent und dauerhafter Idempotenz. Schnelle Annahme; Identitätsarbeit im existierenden Worker.
4. TMDB-Client erweitern: externe IDs, IMDb-Find, alternative Titel, Staffel-/Episodenmetadaten, begrenzte Retries. Typ/IDs niemals ungeprüft zusammenführen.
5. Nachvollziehbarer Matcher: widersprechende IDs harter Ausschluss, ID vor Alias/Jahr, Mehrdeutigkeit manuell. Anbieter- und Episodenmetadaten über vorhandene Modelle/Listen.
6. Aktuelle bestehende Ziele verwenden. Identitätsregistrierung und Kollisionsprüfung vor jedem Download; Film direkt im Root, Serie in stabilem TMDB-zugeordnetem Ordner/Season XX/Titel SXXEXX.ext.
7. Vorhandenen Worker für Prüfung/Downloads erweitern, kein zweiter Downloadprozess. Wiederanlauf, Teilfehler, Pause/Abbruch und Dubletten berücksichtigen.
8. Einstellungen > Integrationen > Seerr und Admin-Anfrageübersicht mit Metadaten, Status, Kandidaten, Freigabe/Retry und Fehlerdetails.
9. Bestehende Discord-Benachrichtigung wiederverwenden, ereignisbasierte Dubletten vermeiden. Optionale Seerr-Verfügbarkeit nur nach dokumentierter API-Prüfung, nicht blockierend.
10. Tests für Nutzerfälle 1–34 (abzüglich ausdrücklich entfernter Anbieter), Regressionen/Migration, README/Docker/Versionsstellen 2.4.0; Abschlusspaket und Rollback auf separater Kopie verifizieren.

## Offizielle Schnittstellen (geprüft 2026-11-21)
- https://docs.seerr.dev/using-seerr/notifications/webhook/
- https://github.com/seerr-team/seerr/blob/develop/server/lib/notifications/index.ts
- https://github.com/seerr-team/seerr/blob/develop/server/lib/notifications/agents/webhook.ts
- https://developer.themoviedb.org/reference/find-by-id
- https://developer.themoviedb.org/reference/movie-external-ids
- https://developer.themoviedb.org/reference/tv-series-external-ids
- https://developer.themoviedb.org/reference/tv-season-details
- https://data.imdb.com/documentation/api-documentation/getting-access/
- https://jellyfin.org/docs/general/server/media/shows/

IMDb-Abgleich erfolgt offiziell über TMDB external_ids/find; eine separate kostenpflichtige AWS-IMDb-Graph-API ist dafür nicht erforderlich. Seerr liefert konfigurierbare Payloads, Authorization und extra-Daten; MEDIA_APPROVED sowie MEDIA_AUTO_APPROVED starten Verarbeitung, nicht PENDING/DECLINED. Unklare Staffel-/Episodenzählung bleibt manuelle Prüfung.

## Abschlussstatus
Noch in Umsetzung. Dieses Dokument ist kein Abschlussnachweis.

## Verifizierter Zwischenstand nach Wiederaufnahme
- Der ursprüngliche vollständige Baseline-Lauf endete mit `1854 passed, 3 skipped in 147.32s (0:02:27)`.
- Neu ergänzt: `seerr_matching.py` mit strikt typisiertem ID-/Typvergleich, Alias+Jahr-Regel, harten Widersprüchen, mehrdeutiger Kandidatenprüfung und konservativer Episodenprüfung.
- Neue Tests: `test_seerr.py`, `test_seerr_matching.py`, `test_tmdb_identity.py`. Zusammen mit `test_media_files.py`: `59 passed in 3.08s`, Exit 0. Befehl: `python -m pytest tests/test_seerr.py tests/test_seerr_matching.py tests/test_tmdb_identity.py tests/test_media_files.py -q`.
- Vollständige Testcollection ist noch rot: vier Collectionfehler in alten BurningSeries-/Kinox- und Mischtests (`test_bs_genre_search.py`, `test_genre_limits.py`, `test_h0melab_models.py`, `test_mirror_extraction.py`). Die Anbieterregistrierungen wurden bereits teilweise entfernt, aber Modelle/Beispiele/Tests sind noch nicht konsistent bereinigt. Ein gebündelter Entfernungsschritt wurde vom Ausführungstool abgewiesen und hat keine Änderungen bewirkt.
- Seerr HTTP-Endpunkt, Worker-Vorbereitung, fertiges Speicherlayout, UI und Discord-Umstellung sind noch nicht verbunden. Insbesondere dürfen die bestehenden leeren Seerr-Platzhalter nicht vom normalen Worker als erfolgreicher Download behandelt werden; bei Worker-Anbindung explizit verarbeiten.
- Nächste Schritte: Entfernung konsistent abschließen, Metadaten-Enrichment und Worker-Vorbereitung implementieren, Webhook/Auth/Settings anbinden, identitätsgebundene Dateibelege und Pfade, manuelle Auswahl mit erneuter Prüfung, UI/Discord, vollständige Regressionen und Abschluss-/Rollback-Paket.
- Nachweise liegen außerhalb des Repos in `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/SEERR_COMPONENT_TESTS.txt` und `CURRENT_COLLECTION.txt`. Das Gesamtziel bleibt aktiv; keine Releasefreigabe.

## Verifizierter Zwischenstand: Webhook, UI und Queue
- POST `/api/integrations/seerr/webhook` ist registriert, vom allgemeinen API-Key-Gate getrennt und mit eigenem Secret geschützt. Approval-/Testevents, Payloadgröße, Idempotenz und Konfliktantworten sind getestet.
- Seerr-Konfiguration wird über die bestehende Settings-Persistenz gespeichert; Secret fehlt in Leseantwort und Export. Integrationsstatus und lokale Konfigurationsprüfung sind vorhanden.
- Einstellungen und Admin-Anfrageübersicht sind implementiert, mit sicherer Textausgabe, Kandidatenansicht, begründeter Freigabe, explizitem Mapping und erneutem Prüfen nach Metadatenfehlern. Review/Retry dürfen laufende oder abgeschlossene Requests nicht neu starten.
- Bestehender Worker verwendet `seerr_processing` und `seerr_files`; erste Film-/Serien-/Pfad-/Dateikollisionstests bestehen. Noch keine vollständige Abdeckung aller 34 Anforderungen: alternative Episodenzählung, Recovery/Concurrency, vorhandene externe Medien und vollständige UI-Laufzeitprüfung weiter auditieren.
- Discord wurde auf Notification-Transport reduziert. Windows-Testeventloop wurde so eingerichtet, dass nur lokale Wakeup-Sockets bei Loop-Erzeugung gestattet werden; Netzwerksperre bleibt während Testausführung aktiv.
- Gezielter Lauf: `python -m pytest tests/test_api_seerr.py tests/test_seerr_processing.py tests/test_discord_notifications.py tests/test_seerr.py tests/test_seerr_matching.py tests/test_tmdb_identity.py -q`: `102 passed in 8.55s`, Exit 0. Danach zusätzlich Review-Retry/UI-Render-Tests: `27 passed in 2.63s` in test_api_seerr.py.
- Vollsuite ergab `8 failed, 1881 passed, 1 skipped in 155.32s`. Alle acht Fehler waren veraltete Testannahmen: sechs `_bs_index_cache`-Stubs und zwei positive alte Connector-Vertragstests. Entfernten Cache-Stub gelöscht, Connector-Tests auf Ablehnung ohne Download/Dateierzeugung umgestellt. Erneuter betroffener Lauf: `68 passed in 7.96s`, Exit 0. Vollsuite nach diesen Änderungen noch erneut ausführen.
- README enthält Seerr-Einrichtung, offizielle Payload-Vorlage, Identifikationsregeln, vorhandene Ziele, Layout und Fehlerdiagnose; `.env.example` enthält Seerr-Variablen. Abschließende Dokumentationsprüfung, Changelog und Abschluss-/Rollback-Paket stehen weiter aus.

## Verifizierter Zwischenstand: Nebenläufigkeit und abweichende Zählung
- `db.claim_next_queued()` übernimmt Busy-Prüfung und Queue-Claim in einer SQLite-Transaktion (`BEGIN IMMEDIATE`). Acht gleichzeitige Verbindungen beanspruchen genau einen Auftrag; bestehende Reihenfolge/Pausen bleiben erhalten. Worker-/DB-/Seerr-Regression: `129 passed in 15.51s`.
- Manuell bestätigte alternative Episodennummerierung speichert nun getrennte Quell- und TMDB-Nummern. Die frische Provider-Episode wird gegen die Quellnummern geprüft; Dateinamen/Belege verwenden die kanonische Nummer. Fremde Serien-IDs und mehrfach verwendete Provider-URLs bleiben blockiert. Gezielte Pipeline-/Worker-Prüfung nach Erweiterung: `45 passed in 5.20s`.
- Deaktivierung von Seerr hält bereits angenommene, noch nicht gestartete Aufträge zur Prüfung an. Neue genehmigte Anfragen erzeugen ein dedupliziertes Discord-Ereignis beim Beginn der Verarbeitung. Abbruchstatus wird auch in Seerr-Metadaten fortgeschrieben.
- Anbieter-Suchnormalisierung bewahrt jetzt IMDb-ID, externe IDs und Erscheinungsdatum; Matching-Testlauf: `26 passed in 1.51s`.
- Legacy-Migration archiviert Originaldaten einschließlich ungültiger historischer Fehlerfelder und bleibt wiederholbar. Neue Tests: `5 passed in 0.47s`.
- Vollsuite `python -m pytest -q`: **1896 passed, 1 skipped in 171.01s (0:02:51)**, TEST_EXIT=0, Prozess terminal. Log: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/CURRENT_FULL_TEST_2.txt`. Der Lauf wurde vor dem zuletzt ergänzten Search-Passthrough-Test und Migrationstestmodul gestartet; diese Änderungen haben separate bestandene Tests. Noch kein endgültiger Release-Nachweis.
- Veraltete positive Connector-Einrichtung und Discord-Anfragebot-Aussagen aus README entfernt; Changelog 2.4.0 ausdrücklich als in verification ergänzt.

### Weiter offene Abschlussprüfungen
- Vollständige Anforderungsmatrix 1–34 gegen reale Coverage, nicht nur Summenzahl; aktuelle Codeänderungen in einem abschließenden Vollsuite-Lauf prüfen.
- UI-Laufzeit/Bedienung einschließlich sinnvoller Episodenkandidatenanzeige, nicht nur HTML-Render und JavaScript-Syntax.
- Quellstaffeln mit vollständig abweichender Staffelzählung, partielle Verfügbarkeit, automatische begrenzte Wiederholungen sowie Restart-/Mehrprozess-Lifecycle weiter prüfen.
- Vorhandene Medien außerhalb neuer Seerr-Belege, Fehler-/Statusdetails, keine Secret-Logs, alle verbleibenden Provideradapter und Dokumentation auditieren.
- Optionalen offiziellen Seerr-Verfügbarkeitsrückkanal bewerten, Docker-/Paketprüfung und abschließendes modifiziertes Archiv/Diff/Verifikation/ausführbares Rollback mit separatem Wiederherstellungstest erstellen.

## Verifizierter Zwischenstand: informierte manuelle Episodenprüfung
- Wiederverarbeitete automatische Kandidaten müssen weiterhin die volle automatische Evidenzprüfung bestehen. Ein gespeicherter selected_url allein ist keine manuelle Freigabe; echte manuelle Auswahl benötigt review_reason und bestätigt weiterhin den Medientyp. Regressionen decken verlorene Metadaten und fehlenden Typ ab.
- Das additive JSON-Feld episode_review speichert aktuelle Anbieter-Episoden und kanonische TMDB-Folgen vor einer Zurückstellung. Die Admin-Oberfläche zeigt für jede kanonische Folge eine Auswahl der tatsächlichen Anbieterfolgen statt ausschließlich eines leeren JSON-Eingabefelds. Die Quelle bleibt an den ausgewählten Anbieter gebunden und wird beim Workerstart erneut gelesen.
- Explizite alternative Quellstaffeln über source_season_number sind unterstützt. Zusätzliche Quellstaffeln werden nur zur Verifikation enumeriert; ausschließlich angefragte kanonische Episoden werden heruntergeladen und nach TMDB-Zählung gespeichert. Ein Test bestätigt Quelle S03E13 → angefragt S01E01 ohne Season-03-Ausgabe.
- Befehl `python -m pytest tests/test_seerr.py tests/test_seerr_matching.py tests/test_seerr_processing.py tests/test_api_seerr.py -q`: `82 passed in 6.75s`, TEST_EXIT=0. Log: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/SEERR_REVIEW_TESTS.txt`. `node --check src/h0melab/web/static/seerr.js` ebenfalls Exit 0.
- README beschreibt die neue Zuordnungsoberfläche und optionale Quellstaffel. UI-Bedienung im Browser, partielle Verfügbarkeit/Wiederholungen, bestehende externe Medien und Abschlussartefakte bleiben offene Auditpunkte. Gesamtziel weiterhin aktiv.

## Verifizierter Zwischenstand: Abbruch während Metadatenprüfung
- Die Seerr-Vorbereitung prüft nun Queue-Abbruch/Pause vor und nach blockierenden Metadaten-/Anbieteroperationen. Abbruch während eines fehlgeschlagenen TMDB-Aufrufs wird nicht mehr durch die Review-Behandlung in einen pausierten Auftrag zurückverwandelt.
- Der Worker prüft dieselben Kontrollen vor Erstellung und vor Download einer Seerr-Episode. Tests decken normalen/erzwungenen Abbruch während TMDB, Abbruch plus TMDB-Ausnahme und Pause während Suche ab.
- Provider-Ausnahmen aus Seerr-Downloads werden nicht mehr ungefiltert geloggt oder in Queuefehler übernommen, da sie signierte URLs/Secrets enthalten können. Der persistente Anfragestatus enthält nun eine fehlgeschlagene Teilverarbeitung als lesbare Fehlermeldung; fertige Belege bleiben für Retry erhalten.
- Gezielter Lauf `python -m pytest tests/test_seerr_processing.py tests/test_worker.py -q`: `54 passed in 7.13s`, Exit 0. Neuer Regressionstest prüft ausdrücklich, dass ein künstliches Token weder in Logs noch Queue-/Seerr-Daten landet.
- Die vorherigen Fortschritte bleiben bestehen. Vollständiger Abschlussaudit einschließlich Browserlaufzeit, vorhandener Medien ohne neue Belege, begrenzter automatischer Wiederholungen und Rollback-Paket weiterhin offen.

## Verifizierter Zwischenstand: begrenzte persistente Wiederholungen
- TMDBTransientError unterscheidet vorübergehende Transport-/429-/5xx-Fehler von ungültigen Zugangsdaten/IDs. Kompatible bisherige Fehlertexte bleiben erhalten.
- `download_queue.not_before` wird additiv migriert. Bestehender atomarer Claim überspringt noch nicht fällige Einträge und verarbeitet andere Aufträge weiter; keine neue Queue, kein zusätzlicher Scheduler.
- Seerr speichert retry_count und plant höchstens drei erneute Prüfungen nach 30/120/600 Sekunden. Pause/Abbruch werden vor Planung berücksichtigt; danach manuelle Prüfung. Expliziter Admin-Retry setzt Backoff/Zähler zurück.
- Anfrageübersicht zeigt Wiederholungszahl und nächsten Prüftermin. README erklärt die Abgrenzung zwischen temporären Ausfällen und Identitätsfehlern.
- Verifikation: `python -m pytest tests/test_seerr_processing.py tests/test_db_queue.py tests/test_worker.py tests/test_tmdb_identity.py tests/test_upcoming.py -q`: **175 passed in 20.69s**, TEST_EXIT=0. Nachweis `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/SEERR_RETRY_TESTS.txt`. Zusätzlicher API/TMDB-Lauf: 52 passed, JavaScript-Syntaxprüfung Exit 0.
- Optionalen Seerr-Rückkanal anhand offizieller OpenAPI geprüft: https://raw.githubusercontent.com/seerr-team/seerr/develop/seerr-api.yml dokumentiert POST /api/v1/media/{mediaId}/{status}, unter anderem available/partial sowie is4k. mediaId ist eine Seerr-Medien-ID, keine TMDB-ID; Implementierung müsste diese gesondert auflösen und partielle Staffeln korrekt behandeln. Noch nicht implementiert; optionaler Rückkanal blockiert Kernintegration nicht.
- Weiter offene Hauptpunkte: Browserlaufzeit, bestehende externe Medienbelege, partielle Verfügbarkeit, gesamter Anforderungsaudit sowie finale Artefakte/Rollback. Gesamtziel bleibt aktiv.

## Verifizierter Zwischenstand: vorhandene Episoden vor Providerauflösung
- Kanonische Serienfolgen mit gültigem Seerr-Dateibeleg benötigen beim erneuten Auftrag keine aktuell gelistete Provider-Episode. Die Vorbereitung trägt sie als reine Beleg-Einträge in denselben Queueauftrag ein; der Worker prüft den Beleg erneut und überspringt Providerkonstruktion/Download.
- Fehlende Folgen werden weiterhin regulär gegen Quelle und TMDB geprüft. Ein neuer Regressionstest weist nach: bereits vorhandene E01 fehlt beim Anbieter, nur E02 wird konstruiert/heruntergeladen, E01 bleibt bytegleich und der Auftrag wird vollständig abgeschlossen.
- Verschwindet eine belegte Datei zwischen Vorbereitung und Ausführung, wird der Auftrag zur erneuten Quellprüfung pausiert statt mit leerer URL oder geratenem Mapping fortgesetzt. Ein weiterer Test deckt dies ab.
- Belegte Dateien über Symlinks oder symlinkende Eltern werden nicht als bestätigte vorhandene Medien übernommen.
- Gezielte Pipeline-/Workerprüfung: `57 passed in 8.32s`, Exit 0. Der zusätzliche kombinierte Seerr/API/Worker-Lauf liegt unter `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/SEERR_EXISTING_TESTS.txt`.
- Weiterhin offen: sichere Identifizierung bereits vorhandener Medien ohne Seerr-Beleg; noch nicht erschienene/fehlende Folgen und partielle Verfügbarkeit; Browserlaufzeit, umfassender Abschlussaudit sowie Artefakt-/Rollback-Verifikation. Gesamtziel aktiv.


## Verifizierter Zwischenstand: NFO-Bestand und Fehlergrenzen
- Der lokale Bestand wird ausschließlich im konfigurierten Ziel anhand expliziter, widerspruchsfreier TMDB-/IMDb-Identitäten aus Jellyfin-NFOs übernommen. Episoden benötigen zusätzlich eine passende tvshow.nfo und explizite Staffel-/Episodennummern. Titel und Dateinamen sind keine Identitätsbeweise.
- Belegte Filme benötigen keine Anbieterauflösung mehr. Der Worker überspringt sie unverändert mit already_present. Bestehende Serienfolgen verwenden weiterhin die bereits implementierten Beleg-Einträge.
- NFO-Lesen und Bestandsumfang sind begrenzt. XML-DTDs werden unabhängig von UTF-8/16/32 über den Parser abgewiesen; Links/Junctions/Hardlinks werden nicht als Belege übernommen. Belegzugriffe behandeln verschwundene/unzugängliche Dateien als nicht vorhanden.
- Nur eigens erzeugte ReviewRequired-Fehlertexte werden angezeigt. Auch externe ValueError-Meldungen gelangen nicht mehr ungefiltert in Anfrage-/Queuedaten; Regression prüft ein künstliches signiertes Token.
- Befehl `python -m pytest tests/test_seerr_inventory.py tests/test_seerr_processing.py tests/test_api_seerr.py tests/test_media_files.py -q`: `69 passed in 8.36s`, Exit 0. Nachweis: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/SEERR_NFO_TESTS.txt`.
- Dokumentierte Grundlage: https://jellyfin.org/docs/general/server/metadata/nfo/ . Es werden keine Videos umbenannt, verschoben oder überschrieben.
- Offen bleiben insbesondere partielle/neue Episoden, Browserlaufzeit, Lifecycle-/Anforderungsaudit, Docker-/Paketprüfung und finale Artefakte samt unabhängig verifiziertem Rollback. Gesamtziel weiterhin aktiv.


## Paket-/Compose-Zwischenprüfung
- `python -m pip wheel . --no-deps --wheel-dir ../H0melab-Downloader-V2-evidence-240/wheels`: Exit 0; Wheel h0melab_downloader-2.4.0-py3-none-any.whl erzeugt. Zuvor schlug der Versuch ohne Build-Isolation wegen lokal fehlendem setuptools.build_meta fehl; isolierter Build funktioniert.
- Wheel geöffnet und Pflichtmodule/-assets sowie METADATA und version.py geprüft: `WHEEL_VERIFY: PASS; Seerr modules/assets present; removed integrations absent; version 2.4.0`, Exit 0. Ergebnis in PACKAGE_VERIFY.txt neben PACKAGE_BUILD.txt im Nachweisverzeichnis. Die entfernten Providerpakete und Connector-Module sind nicht im Wheel enthalten.
- `docker compose config --quiet`: Exit 0. `docker info --format '{{.ServerVersion}}'`: Exit 1, lokaler dockerDesktopLinuxEngine-Pipe fehlt. Container-Build/-Laufzeit ist noch offen; Konfigurationsprüfung ist kein Ersatz dafür.
- Doppelte CR-Zeilenenden in settings.html normalisiert; `git diff --check` anschließend Exit 0. Beide Seerr-JavaScript-Dateien bestehen `node --check` mit Exit 0.

- Aktueller vollständiger Regressionstest: `python -m pytest -q` → `1934 passed, 1 skipped in 179.60s (0:02:59)`, Exit 0. Vollständiges Log: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/CURRENT_FULL_TEST_3.txt`. Dieser Teststand ist ein Zwischenstand, keine vollständige Abnahme der noch offenen Anforderungen.


## Teilweise verfügbare Staffeln und vollständiger Serienbestand
- Eindeutig verifizierte verfügbare Folgen werden auch dann verarbeitet, wenn andere kanonische Folgen beim Anbieter fehlen oder laut TMDB erst zukünftig ausgestrahlt werden. Diese offenen Folgen stehen mit source_missing/not_yet_aired im persistenten episode_review.pending und in der WebUI.
- Nach erfolgreicher Teilverarbeitung lautet der Anfragestatus partial; derselbe Queueauftrag wird pausiert und erhält keinen Abschlusszeitpunkt. Expliziter Retry prüft die Quellen erneut und überspringt fertige Belege. Es gibt keine zweite Queue und keine falsche Staffel-Abschlussmeldung.
- Widersprüchliche/mehrdeutige vorhandene Episodentreffer pausieren weiterhin vor dem Download; eine manuelle Zuordnung wird nicht durch den Teilverfügbarkeitszweig umgangen.
- Sind alle angefragten kanonischen Serienfolgen belegt, wird die Anfrage ohne Anbieter-Suche oder -Konstruktion als already_present abgeschlossen. TMDB prüft weiterhin angefragte Staffelzugehörigkeit und Episodenbestand.
- Neue Regressionen decken fehlende Quelle, zukünftige Folge trotz vorhandener Quelle, spätere Ergänzung nur der fehlenden Folge, unveränderten Dateizeitstempel vorhandener Folgen, Konfliktpause sowie vollständigen Serienbestand ohne Anbieter ab.
- README und Seerr-WebUI beschreiben/zeigen den neuen Teilstatus. Browserlaufzeit, vollständiger aktueller Gesamtregressionstest, finaler Anforderungsaudit und Abschlussartefakte/Rollback bleiben separat zu verifizieren.
- Verifiziert: `python -m pytest tests/test_seerr_processing.py tests/test_seerr.py tests/test_api_seerr.py tests/test_worker.py -q`: `106 passed in 23.62s`, Exit 0. Log: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/SEERR_PARTIAL_TESTS.txt`. `node --check src/h0melab/web/static/seerr.js` und `git diff --check`: Exit 0.


## Worker-Lifecycle und persistente Stornierung
- Aktuellen Betriebssystem-Lock für den gesamten Worker-Lebenszyklus geprüft. Standby-Prozesse setzen laufende Aufträge nicht zurück; Übernahme erfolgt nach Freigabe/Prozesstod, nicht nach einer unsicheren Zeitüberschreitung langsamer Downloads.
- Neue echte Subprozessprüfung hält den Lock in einem zweiten Python-Prozess, weist abgelehnte Recovery im ersten Prozess nach, beendet den Besitzerprozess und verifiziert anschließend Wiederaufnahme und erneuten Lockerwerb.
- Recovery respektiert nun bereits gesetzte cancel_requested/force_cancelled und setzt betroffene Aufträge auf cancelled statt sie automatisch erneut zu laden. Zugehöriger Seerr-Anfragestatus wird synchronisiert. Bestehende Pausebehandlung bleibt erhalten.
- Tests decken normale/erzwungene Stornierung, Seerr-Statussynchronisierung und normalen Crash-Requeue ab. README beschreibt gemeinsamen Datenpfad und das Verbot, eine aktive Lockdatei auszutauschen.
- Verifikation: `python -m pytest tests/test_db_queue.py tests/test_worker.py tests/test_seerr_processing.py -q`: `154 passed in 27.35s`, Exit 0. Log: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/WORKER_RECOVERY_TESTS.txt`. `git diff --check`: Exit 0.
- Erstlauf der neuen Subprozessprüfung scheiterte an fehlendem src-Pythonpfad im Kindprozess; Testumgebung explizit korrigiert und denselben Test erfolgreich wiederholt. Kein verschwiegenes Produktfehler-Ergebnis.
- Noch keine vollständige Abnahme: Browser-/Containerlaufzeit, gesamter Anforderungsaudit sowie Abschlussartefakte mit Rollback bleiben offen.


## Webhook-Konsistenz und vorhandene Serienordner
- Staffelwerte aus flachem Payload, request.seasons und sämtlichen Requested Seasons/Seasons-Extras müssen nach Normalisierung übereinstimmen. Auch widersprüchliche seasonNumber/season_number-Felder werden abgewiesen; kein Last-value-wins. API-Test bestätigt HTTP 400 ohne Queueeintrag.
- Gezielter Payload-/API-/Pipeline-Test: `77 passed in 17.33s`, Exit 0; Log SEERR_PAYLOAD_TESTS.txt im Nachweisverzeichnis.
- Anforderungsaudit deckte eine Lücke bei lokalisiert benannten bestehenden Serienordnern auf. NFO-Inventarisierung bindet jetzt einen eindeutig identifizierten direkten Serienordner an seerr_media, statt für fehlende Folgen einen zweiten Titelordner zu erzeugen. Mehrere identifizierte Ordner, verschachtelte Serienwurzeln oder widersprüchliche Bindungen erfordern Prüfung; es wird nichts verschoben.
- Neue Tests prüfen Übernahme des bestehenden Ordnernamens und Ablehnung mehrerer identifizierter Ordner. Vollständiger Audit läuft separat; keine Abschlussbehauptung.
- Ordner-/Pipelineprüfung: `python -m pytest tests/test_seerr_inventory.py tests/test_seerr_processing.py -q`: `43 passed in 13.04s`, Exit 0. Unabhängiger Audit-Probe: AUDIT_EXISTING_SHOW_FOLDER.txt bestätigt SAME_SHOW_FOLDER=True.


## Audit-Nacharbeit: Ziele und Suchfehler
- Dynamische Ziele jetzt ausdrücklich für Film und Serie mit tatsächlich konkurrierendem sto-Seitenstandard getestet. Der nach Einreihen geänderte zentrale Zielmount gewinnt, beide alten Mounts und Seitenstandard bleiben leer; Queuepfad und Seerr-Ziel stimmen überein. Gezielter Test: 2 passed, 27 deselected in 1.12s, Exit 0.
- Generische Suchfehler loggen keine ungefilterten Exception-Texte oder Suchbegriffe mehr. Seerr erkennt SearchResults.error vor dem Slicing und pausiert mit eigener Fehlermeldung statt einen Ausfall als leere Trefferliste zu behandeln. Regression provoziert einen Token in einer echten SITE_SEARCH-Ausnahme: 1 passed in 0.50s, Exit 0.
- Vollständiger Anforderungsaudit und isolierte Browserlaufzeitprüfung wurden an aktive Subagents gegeben. Zusätzliche frische Identitätsprüfung unmittelbar vor Download wird separat bearbeitet. Diese Arbeiten sind noch nicht als abgeschlossen gewertet.


## Frische Identität unmittelbar vor Download
- Den unterbrochenen Agentenstand im Worktree geprüft und getestet: seerr_matching.revalidate_selection liest die gewählte Anbieter-Detailseite ohne gespeicherte Suchmetadaten als Ersatz erneut. Automatische Freigaben benötigen weiterhin aktuelle ausreichende Evidenz; manuelle Freigaben bleiben an überprüften Titel/Jahr/IDs gebunden. Geänderte oder verlorene Evidenz verhindert Dateiausgabe.
- Neue Tests verwenden echte Detailauswertung mit kontrolliertem Providermodell und prüfen geänderten Titel, Jahr, IDs sowie verlorene Pflichtdaten vor Ausgabe/Download. Gezielter kombinierter Lauf: `100 passed in 20.31s`, Exit 0, FRESH_IDENTITY_TESTS.txt.
- Zusätzliche Root-Korrektur: Pause/Abbruch während der neuen blockierenden Detailprüfung wird danach erneut geprüft; der Worker beendet sich ohne Dateioperation. Pipeline und Identitätsgate zusammen: `40 passed in 9.96s`, Exit 0.
- Die Browserprüfung wurde vom Benutzer per Escape beendet. Keine weitere Computerbedienung durchgeführt. Isolierte Fixture auf Port 51843 anhand ihres konkreten Prozesskommandos identifiziert und beendet; anschließend kein Listener mehr. UI-Laufzeit weiterhin unverifiziert, keine erfolgreiche Browserabnahme behauptet.
- Gesamtsuite CURRENT_FULL_TEST_4.txt läuft als separater aktueller Nachweis. Gesamtziel bleibt aktiv; vollständige Anforderungsmatrix, Containerlaufzeit und finale Artefakt-/Rollbackprüfung sind offen.

- Vollständiger aktueller Lauf abgeschlossen: `python -m pytest -q` ergibt `1963 passed, 1 skipped in 207.25s (0:03:27)`, Exit 0. Log CURRENT_FULL_TEST_4.txt. Erste explizite 34-Gate-Matrix in docs/REQUIREMENTS-AUDIT-2.4.0.md angelegt; echte Mehrstaffel-/HTTP-Worker-/Bestandsordner-End-to-end-Nachweise und andere Abnahmen bleiben ausdrücklich offen.


## Durchgängige Webhook-/Mehrstaffel-/Bestandsordnerprüfung
- Neue authentifizierte HTTP-Tests führen genehmigte movie/tv-Webhooks über echte SQLite-Einreihung, atomaren Claim und Worker bis zur Datei. TV prüft zwei Staffeln in genau einem Hauptordner; movie bleibt flach im Filmziel. Erneute Webhooks nach Abschluss bleiben ohne weitere Arbeit.
- NFO-Bestandsordnerfall lädt nur E02 in den vorhandenen lokalisierten Serienordner. E01 und dessen Zeitstempel bleiben unverändert; kein zweiter Titelordner entsteht.
- Fehlender Film- bzw. Serienmount pausiert die Anfrage vor Providerkonstruktion und Download.
- Externe TMDB-/Providerantworten sowie Medienbytes sind kontrollierte Testdaten; dies ist kein Live-Provider- oder Containerlaufzeittest.
- Verifiziert: `python -m pytest tests/test_seerr_processing.py -q`: `36 passed in 10.00s`, Exit 0. Log SEERR_END_TO_END_TESTS.txt. Auditmatrix entsprechend aktualisiert; weitere Anforderungen bleiben offen.


## Identitätsfehler vor Bytes: korrekter Review-Status
- Abschnitt 6.4 verlangt bei unzureichender Identität manual_review, nicht einen gewöhnlichen Downloadfehler. Neue typisierte IdentityReviewRequired-Ausnahmen für frische Identitätsprüfung, Typ-/Nummern-/ID-Konflikte werden im Worker gezielt behandelt.
- Betroffener Queueauftrag pausiert ohne Fortschrittserhöhung/Dateiausgabe. Abbruch-/Pausenpriorität bleibt über review()/stopped() erhalten. Andere Providerexceptions bleiben generisch und werden nicht offengelegt.
- Neuer Pipelinefall beweist geänderte TMDB-ID zwischen Vorbereitung und Download: kein Byte, kein completed/failed, manual_review und pausierte Queue.
- Verifikation: `82 passed in 15.09s`, Exit 0. Nachweis SEERR_IDENTITY_REVIEW_TESTS.txt. Anforderungsaudit um Abschnitte 1–22 mit konkreten offenen Prüfungen erweitert; Gesamtziel aktiv.


## Optionale beschreibende Webhook-Metadaten (§4)
- Originaltitel, Erscheinungsjahr und Erscheinungsdatum aus flachen oder verschachtelten konfigurierten Payloads werden validiert und im bestehenden Payload-JSON gespeichert. Unterstützte Feldnamen und Datumsformat sind in README dokumentiert, ohne neue Seerr-Templatevariablen zu behaupten.
- Widersprüchliche Aliase, nichtskalare Titel, zu lange Titel, boolesche/ungültige Jahre, ungültige Kalendertage und Jahr/Datum-Konflikte werden abgewiesen.
- Diese Daten ersetzen keine verifizierte Identität: neuer Pipeline-Test mit absichtlich abweichenden beschreibenden Webhookdaten bestätigt TMDB-Titel/Jahr und kanonische Ausgabe; Originalpayload bleibt zur Nachvollziehbarkeit erhalten.
- Verifikation: `python -m pytest tests/test_seerr.py tests/test_api_seerr.py tests/test_seerr_processing.py -q`: `96 passed in 19.04s`, Exit 0. Log: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/SEERR_OPTIONAL_METADATA_TESTS.txt`.
- Gesamtziel weiterhin aktiv; offene Laufzeit-/Artefakt- und übrige Auditpunkte bleiben bestehen.


## Explizite TV-ID-Auflösung und Staffelvalidierung
- TV-spezifische Tests bestätigen TMDB-only, IMDb-only und beide IDs über typed TV-Endpunkte inklusive Originaltitel/Jahr/Alternativtitel/Specials. Ein zusätzlicher Filmfund in IMDb-find beeinflusst TV nicht; mehrdeutige TV-Funde und ID-Konflikte werden abgewiesen.
- Doppelte Staffelnummern in TMDB-Seriendetails werden nun ausdrücklich als ungültig behandelt statt später durch Mengen-/Dictionarybildung verdeckt. Negative/boolesche Nummern bleiben abgewiesen.
- Separate Matcherfälle verhindern automatische Auswahl ähnlich benannter TV-Produktionen, Spin-offs und abweichender Neuauflagen.
- Gezielte Verifikation: `python -m pytest tests/test_tmdb_identity.py tests/test_seerr_matching.py tests/test_seerr_processing.py -q`: `101 passed in 25.19s`, Exit 0. Log: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/TV_IDENTITY_TESTS.txt`. Audit aktualisiert, Gesamtziel aktiv.


## Frische Episodenprüfung vor Download (§6.4/8)
- Nach der frischen Serienidentität wird die konkrete Quellstaffel erneut über bestehende Provider-Enumeratoren gelesen. Genau eine passende URL, unveränderte Quellnummern und widerspruchsfreie Serien-IDs sind erforderlich.
- Automatische Zuordnungen müssen erneut die kanonische Episodenprüfung inklusive Titel bestehen. Manuelle abweichende Nummern bleiben zulässig, aber ein geänderter/verlorener zuvor geprüfter Quelltitel erfordert erneute Prüfung.
- Drei neue Pipelinefälle simulieren ausgetauschten Episodentitel, fremde Serien-ID und verschwundene Quelle zwischen Vorbereitung/Ausführung: kein Byte, kein Fortschritt, manual_review. Separater manueller Mappingtest bestätigt erlaubte Originalzuordnung und Ablehnung später ausgetauschter Inhalte.
- Gezielter Lauf Pipeline/Matcher/Downloadgate/Worker: `116 passed in 20.24s`, Exit 0, FRESH_EPISODE_TESTS.txt. Danach ergänzter manueller Mappingtest mit Matchersuite: `31 passed in 2.07s`, Exit 0.
- Der erste Lauf fand eine veraltete Erwartung zur Enumerator-Aufrufzahl; sie enthält nun ausdrücklich die zusätzliche frische Quellstaffelprüfung [3]. Bestehender Test bestätigt weiterhin, dass nur die angefragte kanonische Season 01 ausgegeben wird.
- Gesamtziel aktiv; Browser-/Container-/Rollback-Abnahme und weitere offene Auditpunkte bleiben bestehen.


## Erscheinungsdatum als zusätzliche Identitätsevidenz
- Matching prüft vorhandene vollständige Providerdaten auf gültigen Kalendertag, Konsistenz zum Anbieterjahr und Übereinstimmung mit bekanntem TMDB-Erscheinungsdatum. Abweichende regionale Termine führen zur Prüfung statt zu stiller Gleichsetzung; fehlende Daten erzeugen keinen Konflikt.
- Ein übereinstimmendes Datum ergänzt die nachvollziehbare Bewertung, genügt aber allein weder für Identität noch automatischen Download. Detailauswertung übernimmt release_date aus dem frischen Providermodell.
- Manuelle Auswahl bleibt auch an das zuvor geprüfte Datum gebunden; spätere Änderungen erfordern erneute Prüfung selbst ohne bekanntes TMDB-Datum.
- Verifikation Matcher/Downloadgate/Pipeline: `88 passed in 15.89s`, Exit 0, RELEASE_DATE_TESTS.txt. Zusätzlicher frischer manueller Datumsfall: Downloadgate-Suite `10 passed in 1.15s`, Exit 0.
- README und Anforderungsaudit aktualisiert; Gesamtziel noch aktiv, verbleibende Laufzeit-/Artefaktprüfungen unverändert offen.


## Begrenzte Wiederholungen fehlgeschlagener Anbietersuche
- SearchResults.error löst nun ProviderSearchUnavailable und denselben persistenten Queuebackoff wie temporäre Metadatenfehler aus. provider_retry/provider_failed unterscheiden die Fehlerquelle; drei gemeinsame Versuche je Anfrage verhindern unbegrenzte Wiederholungen bei wechselnder Ursache.
- Teilresultate einer fehlgeschlagenen Gesamtsuche werden nicht als eindeutige Auswahl verwendet. Nach 30/120/600 Sekunden und ausgeschöpftem Budget folgt manual_review. Pause/Abbruch und andere Queueeinträge bleiben über bestehende Mechanismen berücksichtigt.
- Regression prüft alle drei Termine, Status/Zähler, Review nach Ausschöpfung und keinerlei Dateiausgabe.
- Verifikation: `python -m pytest tests/test_seerr_processing.py tests/test_seerr_search_errors.py tests/test_db_queue.py -q`: `133 passed in 28.84s`, Exit 0. Log PROVIDER_RETRY_TESTS.txt im Nachweisverzeichnis.
- Das zuvor verifizierte MODIFIED_FILE.zip bleibt ein unverändertes historisches Zwischenarchiv. Es enthält diese neue Änderung noch nicht und muss vor finaler Übergabe neu erzeugt/verifiziert werden. Browserbedienung bleibt auf expliziten Nutzerwunsch ausgesetzt; Gesamtziel aktiv.


## Konkurrenzprüfung der Discord-Ereignis-Deduplizierung
- Neuer Test führt 16 gleichzeitige Ereignisaufrufe über acht Threads und unabhängige SQLite-Verbindungen aus: genau ein Transportaufruf und ein Ledger-Eintrag. Verschiedene Episodenereignisse bleiben separat.
- Zweiter Test bestätigt: nicht eingeplante Benachrichtigung verändert weder Queue noch Anfragestatus; Replay erzeugt keine zweite Nachricht. Das dokumentierte Verhalten ist Best-Effort/At-most-once, keine behauptete garantierte Zustellung.
- Verifiziert: `python -m pytest tests/test_seerr.py tests/test_discord_notifications.py -q`: `40 passed in 5.15s`, Exit 0, NOTIFICATION_DEDUP_TESTS.txt. Keine Verbindung zu einem echten Discord-Konto.
- Docker erneut per CLI geprüft: `docker compose config --quiet` Exit 0; `docker info --format '{{.ServerVersion}}'` Exit 1, Pipe dockerDesktopLinuxEngine nicht vorhanden. Kein Container-Laufzeitnachweis. Benutzerwunsch zur ausgesetzten Computerbedienung bleibt beachtet.
- Aktueller Stand geht über das archivierte Zwischenpaket hinaus; finales Archiv/Diff/Rollback muss vor Abschluss erneut erstellt werden. Gesamtziel aktiv.


## Atomare manuelle Zurückstellung und konkurrierender Abbruch
- review() setzt Queue-Pause/Fehler und Seerr-Status nun in einer gemeinsamen BEGIN-IMMEDIATE-Transaktion. Ein SQLite-Fehler beim zweiten Update rollt das erste Update zurück; kein halb pausierter Auftrag.
- Bedingtes Queue-Update respektiert cancel_requested/pause_requested und laufenden Zustand auch nach dem vorherigen Kontrollcheck. Verliert Review das Rennen gegen einen Abbruch, synchronisiert stopped() cancelled statt den Auftrag zurück auf queued zu setzen.
- Fehler-Injektion per SQLite-Trigger beweist unveränderte Queue und Anfrage nach gescheitertem Request-Update. Zweiter Test erzwingt Stornierung zwischen Vorprüfung und Transaktion und bestätigt cancelled in beiden Datensätzen.
- Verifikation: `python -m pytest tests/test_seerr_processing.py tests/test_seerr.py tests/test_api_seerr.py -q`: `104 passed in 18.52s`, Exit 0. Log ATOMIC_REVIEW_TESTS.txt. Gesamtziel aktiv; übrige Audit-/Laufzeit-/finale Artefaktprüfungen offen.


## Konkurrenz und Transaktionsintegrität administrativer Freigaben
- Drei Barrier-/Threadtests führen gleichzeitig review/review, retry/retry und review/retry auf derselben pausierten Anfrage aus. Jeweils genau eine Aktion übernimmt; die andere wird abgewiesen, die gewinnende Begründung bleibt erhalten und genau ein Queueclaim ist möglich.
- Zwei SQLite-Triggerfälle lassen das Queue-Update nach dem Seerr-Update scheitern. Für review und retry bleiben beide Datensätze vollständig unverändert; die bestehende BEGIN-IMMEDIATE-Transaktion besteht die Fehler-Injektion.
- Keine Produktänderung nötig; spezifische bisher fehlende Audit-Evidenz ergänzt.
- Verifikation: `python -m pytest tests/test_seerr.py tests/test_api_seerr.py -q`: `65 passed in 6.09s`, Exit 0. Nachweis ADMIN_CONCURRENCY_TESTS.txt im bestehenden Evidence-Verzeichnis.
- Browserbedienung bleibt ausgesetzt; Docker-Laufzeit und abschließende Gesamtprüfung/erneute Artefaktverifikation bleiben offen. Gesamtziel aktiv.


## Atomarer Abschluss und äußerer Worker-Fehlerpfad
- Seerr-Abschluss schreibt Queuezustand und Anfragestatus in einer gemeinsamen Transaktion, einschließlich partial (pausierte queued-Zeile ohne Abschlusszeitpunkt). Der Worker setzt den Seerr-Queuezustand nicht mehr vorab separat.
- Fehler-Injektion beim finalen Request-Update beweist für completed und partial: beide Datensätze bleiben vollständig im vorherigen Zustand.
- Unerwartete äußere Workerfehler synchronisieren eine noch laufende Seerr-Anfrage über denselben atomaren Abschluss auf failed. Bereits commit-tete Abschlüsse werden durch späteren Benachrichtigungsfehler nicht zurückgenommen; Pause/Abbruch bleiben priorisiert.
- Äußere Seerr-Fehler loggen nur Queue-ID und generischen Text, keine beliebigen Exception-Secrets. Neue Tests prüfen beide Fehlerzeitpunkte und Secret-Abwesenheit.
- Vorheriger Lauf endete laut Log mit 172 passed in 33.25s; sein Prozesshandle war nach Fortsetzung nicht mehr verfügbar. Aktueller vollständig beobachteter Lauf: `python -m pytest tests/test_seerr_processing.py tests/test_worker.py tests/test_db_queue.py -q`: `174 passed in 25.27s`, Exit 0. Nachweis ATOMIC_FINISH_TESTS.txt.
- Finale Artefakte bleiben gegenüber dem historischen Snapshot neu zu erstellen; Browserbedienung ausgesetzt, Docker-Laufzeit weiter offen. Gesamtziel aktiv.


## Neustartstatus der Seerr-Anfragen
- Exklusive Worker-Recovery synchronisiert unterbrochene checking/downloading-Anfragen mit ihren zuvor running-Queuezeilen: waiting ohne Pause, paused mit Pause. Kein irreführender laufender Anfragestatus nach Recovery.
- Bereits gestoppte manual_review/partial-Aufträge, abgeschlossene completed/already_present-Aufträge, failed und waiting_retry bleiben einschließlich retry_count/not_before unverändert. Bestehende Stornierungspriorität bleibt erhalten.
- Zehn neue parametrische Fälle prüfen aktive/pausierte Wiederaufnahme und unveränderte gestoppte/backoff-Zustände; echte Claims bestätigen Wiederaufnahme nur ohne Pause.
- Verifikation: `python -m pytest tests/test_seerr_processing.py tests/test_db_queue.py tests/test_worker.py -q`: `184 passed in 26.22s`, Exit 0. Log RECOVERY_STATES_TESTS.txt im Nachweisverzeichnis. Gesamtziel aktiv; ausstehende Browser-/Container-/Finalartefaktabnahme unverändert offen.


## Specials und Zielwechsel mit bestehendem Beleg
- Explizit angefragte Season 0 durchläuft TMDB-/Providerprüfung und erzeugt Example/Season 00/Example S00E01.mp4; normale Season 01 wird nicht angelegt.
- Zielwechseltest besitzt E01 mit gültigem Beleg im alten Serienmount, wechselt das zentrale Ziel und lädt nur E02 in den neuen Mount. Alte Bytes/Zeitstempel bleiben unverändert; E01 wird weder kopiert noch erneut geladen, E02 landet nicht im alten Mount. Dies folgt der Kombination aus aktuellem Ziel und ausdrücklichem Nichtverschieben bestehender Medien.
- Gezielte Verifikation: `python -m pytest tests/test_seerr_processing.py tests/test_seerr_inventory.py -q`: `75 passed in 12.65s`, Exit 0. Log SPECIALS_TARGET_CHANGE_TESTS.txt im Nachweisverzeichnis.
- Keine Produktänderung erforderlich; konkrete Audit-Lücken durch Pipeline-Evidenz geschlossen. Browserbedienung weiterhin ausgesetzt; Gesamtziel aktiv.


## Aktualisierte vollständige Regression und Paketprüfung
- `python -m pytest -q`: `2029 passed, 1 skipped in 168.16s (0:02:48)`, Exit 0. Log CURRENT_FULL_TEST_5.txt im Nachweisverzeichnis.
- Aktuelles Wheel 2.4.0 isoliert gebaut; Seerr-Kernmodule und UI-Dateien im Archiv bytegleich zum Quellstand, entfernte Provider/Connector-Module abwesend, ZIP-Integrität und METADATA geprüft. CURRENT_PACKAGE_BUILD.txt und CURRENT_PACKAGE_VERIFY.txt, Exit 0; SHA256 7ddb27942a176b51a73a613eb9e1fd5e7eec4258fda15b6be293304550be5bc0.
- Beide Seerr-JavaScript-Dateien bestehen node --check; git diff --check Exit 0. Keine behauptete Browser- oder Containerabnahme.
- Auditbericht von veralteten Offen-Markierungen bereinigt: historischer kompletter Rollbacknachweis existiert, ist aber nicht automatisch auf spätere Änderungen übertragbar. Finales Artefaktpaket weiterhin neu zu erzeugen/verifizieren.
- Gesamtziel aktiv, explizite ausstehende Laufzeitprüfungen bleiben dokumentiert; Computerbedienung weiterhin ausgesetzt.


## Vollständige Anfragehistorie statt fester 200er-Grenze
- Anfrage-API unterstützt validiertes offset/limit (1–200), deterministische Sortierung created/request_id und next_offset. Die WebUI lädt 50 Einträge und bietet Weitere Anfragen laden; Aktualisieren setzt auf die erste Seite zurück.
- Regression legt 205 Anfragen mit identischem Zeitstempel an und erreicht jede genau einmal einschließlich ältester manual_review-Anfrage. Ungültige/negative/unbegrenzte Seitenwerte werden abgewiesen.
- Verifikation: `python -m pytest tests/test_api_seerr.py tests/test_seerr.py -q`: `71 passed in 5.92s`, Exit 0. Log SEERR_PAGINATION_TESTS.txt. node --check für seerr.js Exit 0.
- Browserbedienung wurde nicht ausgeführt und bleibt ausgesetzt; die geänderte Bedienung hat bislang API-/Syntax-, nicht Browserlaufzeitnachweise. Gesamtziel aktiv; finales Paket muss diese Änderung zusätzlich aufnehmen.


## Zielwechsel während Transfer und Anfrage-Lesezugriffe
- Neuer Pipelinefall wechselt das zentrale Serienziel innerhalb des E01-Downloads. E01 bleibt bytegenau am zuvor gebundenen Pfad, E02 entsteht ausschließlich am neuen Ziel; beide Identitätsbelege zeigen auf die tatsächlichen Dateien. Queue und Anfrage enden completed.
- Anfragehistorie liest Queuezustände nun mit einem LEFT JOIN in genau einer Datenbanksitzung; fehlende Queuezeilen bleiben sichtbar, JSON-Felder werden dekodiert und interne Join-Aliase nicht ausgeliefert. REQUEST_LIST_QUERY_TESTS.txt: 72 passed in 5.19s.
- Historische Providerreferenz im Retry-Docstring entfernt; keine Änderung der Retry-Funktion.
- `python -m pytest tests/test_seerr_processing.py tests/test_media_files.py tests/test_api_seerr.py tests/test_seerr.py -q`: `139 passed in 19.19s`, Exit 0. Log MID_TRANSFER_TARGET_TESTS.txt im externen Nachweisverzeichnis.
- Formatierung mit ruff format erfolgreich; black ist in der Umgebung nicht installiert und wurde nicht nachinstalliert. git diff --check erfolgreich.
- Computerbedienung bleibt auf ausdrücklichen Nutzerwunsch ausgesetzt. Gesamtziel aktiv; keine endgültige Releasefreigabe und kein aktualisierter finaler Snapshot behauptet.


## Unbekannte Zieldatei: manuelle Prüfung statt Downloadfehler
- seerr_files.download wirft bei vorhandener Datei ohne passenden Identitätsbeleg jetzt IdentityReviewRequired. Der bestehende Worker-Reviewpfad pausiert dieselbe Queuezeile und setzt manual_review, statt die Datei als allgemeinen Downloadfehler zu behandeln.
- Parametrische Film-/Serienfälle prüfen unveränderte Bytes und mtime, keinen Aufruf von Episode.download, keinen erfundenen Beleg, unveränderten Episodenindex und synchronen Queue-/Anfragestatus.
- `python -m pytest tests/test_seerr_processing.py tests/test_seerr_download_identity.py tests/test_worker.py -q`: `108 passed in 16.53s`, Exit 0. Externes Log UNKNOWN_FILE_REVIEW_TESTS.txt.
- Finale Artefakte weiterhin gegenüber historischem Snapshot zu aktualisieren; keine Browserbedienung ausgeführt.

- Vollständige Regression nach diesen Änderungen: `python -m pytest -q`: `2038 passed, 1 skipped in 168.01s (0:02:48)`, Exit 0. Log CURRENT_FULL_TEST_6.txt. Beide Seerr-JS-Dateien bestehen node --check; Formatierungs- und Diff-Whitespace-Prüfung bestanden.


## Snapshot 6 und Anbieter-Suchfehler
- Snapshot 6 im externen Nachweisverzeichnis enthält 294 verwaltete Dateien. Archivintegrität und alle Original-/Modified-SHA256 geprüft; unabhängige Modified-Kopie: 2038 passed, 1 skipped in 236.05s, Exit 0. Zweite Kopie durch ausführbares ROLLBACK.sh bytegenau auf alle 294 Originaldateien zurückgesetzt; Zusätze entfernt; 1854 passed, 3 skipped in 213.08s, Exit 0. Worktree und Modified-Kopie blieben vor der folgenden Änderung manifestgleich.
- Anschließend Aniworld-Suche korrigiert: explizit timeout=15, raise_for_status vor JSON. HTTP-Fehler, Timeout und ungültiges JSON werden von sitesearch als Suchfehler behandelt, nicht als erfolgreicher leerer Treffer; gültiges [] bleibt erfolgreiche leere Suche. Vorhandener Seerr-Backoff kann so greifen.
- `python -m pytest tests/test_seerr_search_errors.py tests/test_seerr_processing.py tests/test_genre_limits.py tests/test_title_matching.py -q`: `182 passed in 19.11s`, Exit 0, ANIWORLD_SEARCH_FAILURE_TESTS.txt.
- Anbieter-Timeoutaudit bleibt offen: Aniworld-Detailzugriffe in models/h0melab_to/{series,season,episode}.py besitzen noch keinen expliziten Timeout. Der Suchfix behauptet keine Gesamtzeitgrenze für alle Providerzugriffe.
- Snapshot 6 bleibt ein gültiger unveränderlicher Nachweis des vorigen Standes; diese anschließende Änderung erfordert vor finaler Freigabe einen neuen Snapshot. Computerbedienung bleibt ausgesetzt.


## Aniworld-Detailprüfung mit begrenzten HTTP-Aufrufen
- Series-, Season- und Episode-_html setzen nun timeout=15 und prüfen raise_for_status vor dem Lesen/Speichern des Antworttexts. Fehlgeschlagene Antworten bleiben nicht im HTML-Cache; ein späterer erfolgreicher Aufruf funktioniert und wird danach wie bisher zwischengespeichert.
- Sechs parametrische Modelltests decken HTTP-Fehler und Timeout für alle drei Klassen ab, prüfen die tatsächlichen Aufrufparameter, verhindern Zugriff auf Fehlertexte und bestätigen genau zwei Requests trotz drei Property-Zugriffen.
- `python -m pytest tests/test_seerr_search_errors.py tests/test_seerr_matching.py tests/test_seerr_download_identity.py tests/test_mirror_extraction.py tests/test_media.py -q`: `109 passed in 6.93s`, Exit 0; ANIWORLD_DETAIL_TIMEOUT_TESTS.txt.
- Abgrenzung: timeout=15 ist ein Transporttimeout pro Aufruf, keine garantierte Gesamtzeitgrenze über sämtliche Redirects/Provider. Hoster-Redirectauflösung (provider_url) bleibt separat zu auditieren; die Änderung ersetzt weder Download-Engine noch Providerwahl.
- Snapshot 6 bleibt unverändert; diese nachfolgenden Änderungen sind dort nicht enthalten. Gesamtziel aktiv, Browserbedienung weiterhin ausgesetzt.


## Hoster-Weiterleitungen: Timeout und fehlertoleranter Cache
- Aniworld, FilmPalast und MegaKino provider_url verwenden timeout=15 und raise_for_status vor dem Zwischenspeichern des aufgelösten Ziels. HTTP-/Transportfehler hinterlassen keinen Cacheeintrag.
- Aniworld speichert die URL erst nach erfolgreicher Prüfung auf ein echtes externes Ziel. Vorher blieb bei einer ungültigen Rückleitung die fehlerhafte Adresse im Cache und wurde beim zweiten Zugriff ohne Prüfung zurückgegeben. Regression prüft zwei aufeinanderfolgende abgewiesene Zugriffe.
- Sechs weitere Fälle prüfen Timeout/HTTP-Fehler für alle drei Adapter, nachfolgenden Erfolg und unverändert funktionierenden Erfolgscache. Sie verwenden isolierte reale Property-Implementierungen ohne Netzwerk.
- `python -m pytest tests/test_mirror_extraction.py tests/test_seerr_download_identity.py tests/test_megakino_series_routing.py -q`: `35 passed in 4.46s`, Exit 0 (HOSTER_REDIRECT_TESTS.txt).
- `python -m pytest tests/test_provider_failures.py tests/test_providers_h0melab.py tests/test_providers_filmpalast.py tests/test_providers_megakino.py tests/test_worker.py -q`: `55 passed in 4.52s`, Exit 0 (HOSTER_REDIRECT_REGRESSION.txt).
- Erster Testaufruf referenzierte eine nicht vorhandene test_provider_attempts.py und endete vor Testausführung mit Exit 4; korrigierte Befehle oben vollständig erfolgreich. Keine Live-Providerabnahme behauptet.
- Snapshot 6 ist weiterhin der vorige unveränderte Stand; finale Gesamtregression und Paketaktualisierung müssen die neueren Änderungen aufnehmen. Computerbedienung bleibt ausgesetzt.


## Gesamtregression und Paketstand nach Timeoutkorrekturen
- Erster vollständiger Lauf CURRENT_FULL_TEST_7.txt: 3 failed, 2052 passed, 1 skipped. Drei One-Piece-Bestandstests verwendeten dieselbe vereinfachte HTTP-Antwort ohne raise_for_status. Diese simulierte Erfolgsantwort wurde um die Methode ergänzt; produktive Statusprüfung unverändert.
- Erneute vollständige Suite `python -m pytest -q`: `2055 passed, 1 skipped in 167.17s (0:02:47)`, Exit 0, CURRENT_FULL_TEST_8.txt.
- `python -m pip wheel . --no-deps --wheel-dir ../H0melab-Downloader-V2-evidence-240/wheels-7`: erfolgreich, Exit 0. Archivintegrität, METADATA-Version 2.4.0, 165 enthaltene Quell-/Assetdateien bytegleich, entfernte Provider/Connector abwesend, Seerr und Broadcaster vorhanden. PACKAGE_BUILD_7.txt / PACKAGE_VERIFY_7.txt.
- Wheel SHA256 faf525e5bfebdc4e612ca96aca10cfd1c906bfdb10ca9f234379cce7b5d36fa3. Erster Paketprüfer erwartete LF statt der zulässigen CRLF-Metadaten; Korrektur auf splitlines validiert dieselbe Version ohne Paketänderung.
- docker compose config --quiet Exit 0; kein Containerlauf behauptet. Browserbedienung bleibt ausgesetzt. Snapshot 6 enthält die neueren Timeoutänderungen nicht; endgültige Artefaktaktualisierung bleibt offen.


## Verfügbarer Byte-Fortschritt in der Anfrageübersicht
- Gegen §13 des Originalauftrags geprüft: Episodenfortschritt war sichtbar, verfügbare Queue-Bytewerte noch nicht. Anfragekarte zeigt jetzt current_bytes / total_bytes; unbekannte Gesamtlänge wird ausdrücklich als unbekannt angezeigt statt als erfundener Prozentwert. Fehlender Queueeintrag bleibt als solcher erkennbar.
- Drei HTTP-API-Fälle prüfen keine Bytes, Bytes bei unbekannter Gesamtlänge und bekannte Gesamtlänge; Werte werden unverändert aus der bestehenden Queue gelesen.
- `python -m pytest tests/test_api_seerr.py tests/test_seerr.py -q`: `75 passed in 5.35s`, Exit 0, SEERR_BYTE_PROGRESS_TESTS.txt. `node --check src/h0melab/web/static/seerr.js` bestanden. Kein Browserlaufzeitnachweis.
- Docker-Engine erneut abgefragt: npipe dockerDesktopLinuxEngine fehlt weiterhin. Kein GUI-Start versucht, Computerbedienung auf Nutzerwunsch weiter ausgesetzt.
- Wheel-7 und Snapshot 6 enthalten diese nachfolgende UI-Änderung nicht. Endgültiges Paket muss sie aufnehmen; Gesamtziel aktiv.


## Erneuter offizieller Seerr-Vertragsabgleich
- Offizielle Dokumentation https://docs.seerr.dev/using-seerr/notifications/webhook/ und Implementierung https://github.com/seerr-team/seerr/blob/develop/server/lib/notifications/agents/webhook.ts erneut gelesen. README nutzt dokumentierte Variablen; extra wird als besonderer Objektschlüssel in ein Array überführt. Authorization wird als Header gesendet; fehlende skalare Werte werden als leere Zeichenfolge ersetzt.
- Ereignisnamen MEDIA_APPROVED / MEDIA_AUTO_APPROVED gegen https://github.com/seerr-team/seerr/blob/develop/server/lib/notifications/index.ts bestätigt; keine Ausweitung auf Pending/Auto-Requested/Available.
- Neue Regression liest die tatsächliche JSON-Vorlage aus README, bildet die dokumentierte Ersetzung im bereits geparsten Objekt nach und sendet sie an den Flask-Endpunkt: movie/tv × beide Approval-Ereignisse × IMDb vorhanden/leer. Request-ID, Typ, IDs, Staffeln und Titel mit Anführungszeichen werden gespeichert wie erwartet.
- `python -m pytest tests/test_api_seerr.py -q`: `45 passed in 5.55s`, Exit 0; README_WEBHOOK_CONTRACT_TESTS.txt. Dies prüft unseren dokumentierten HTTP-Vertrag, nicht eine laufende Seerr-Instanz oder ihren Transport.
- Vorlagenabgleich damit belegt; Browserbedienung bleibt ausgesetzt, finale Paket-/Snapshotaktualisierung und übrige Laufzeitabnahmen offen.


## Transaktionsabbruch der Altintegration-Migration
- Zwei neue Fehler-Injektionen führen echte SQL-Änderungen aus und werfen danach OperationalError: nach DROP der ersten Connector-Tabelle sowie nach UPDATE der alten Queuezeile. Die vorhandene db.session rollt beide Transaktionen zurück.
- Nach Öffnen einer neuen Sitzung sind beide Originaltabellen mit ihren Zeilen und die vollständige ursprüngliche Queuezeile unverändert; kein Teilarchiv bleibt zurück. Wiederholung ohne Injektion archiviert drei Originaldatensätze, entfernt exklusive Tabellen und pausiert den alten Auftrag erfolgreich.
- `python -m pytest tests/test_legacy_request_migration.py tests/test_db_queue.py -q`: `97 passed in 13.44s`, Exit 0; MIGRATION_FAILURE_ROLLBACK_TESTS.txt. Keine Produktcodeänderung nötig; die vorhandene Transaktionsgrenze ist durch Fehler nach tatsächlichen Schreiboperationen geprüft.
- Evidenz gilt für die isolierte SQLite-Migrationsfixture, nicht für eine manipulierte produktive Datenbank. Snapshot-/Releasefinalisierung und externe Laufzeitabnahmen bleiben offen; Browserbedienung weiterhin ausgesetzt.


## Recovery nach fehlgeschlagenem Fehlerstatus-Commit
- Echter Zuverlässigkeitsfehler behoben: Wenn Verarbeitung und anschließendes Speichern von failed beide scheiterten, wurde der zweite Fehler verschluckt. Die running-Zeile blockierte danach dauerhaft den Claim weiterer Aufträge.
- Der zweite Fehler verlässt jetzt _run_owned; die bestehende äußere Eigentümer-/Recovery-Schleife gibt den OS-Lock frei, wartet und führt nach erneuter Übernahme die etablierte Wiederanlaufprüfung aus. Keine zusätzliche Engine und kein zweiter Worker.
- Regression durchläuft die echte äußere Schleife und SQLite-Claims: erster Seerr-Auftrag scheitert, Statusschreiben fällt einmal aus, Recovery nimmt denselben Auftrag erneut auf, Abschluss gelingt, danach wird der zweite Auftrag verarbeitet. Genau eine Wartephase, keine hängenbleibende running-Zeile.
- Äußeres Recovery-Logging gibt keinen Exception-Trace mit möglicherweise signierter Provider-URL aus; ein markiertes Secret aus der ursprünglichen Exception bleibt im Test vollständig aus dem Log.
- `python -m pytest tests/test_seerr_processing.py tests/test_worker.py tests/test_db_queue.py -q`: `189 passed in 28.10s`, Exit 0; WORKER_STORAGE_RECOVERY_TESTS.txt. Ein vorheriger Lauf ohne zusätzliche Logprüfung bestand ebenfalls; maßgeblich ist der aktuelle Lauf.
- Gesamtziel aktiv; neuer Code noch nicht im historischen Snapshot 6/Wheel-7. Computerbedienung weiterhin ausgesetzt.


## Atomarer Pausenstatus während Transfer
- db.finish_pause setzt Queuepause und Seerr-Anfragestatus paused jetzt innerhalb derselben BEGIN-IMMEDIATE-Transaktion. Dadurch bleiben direkte Worker-Pausen (auch DownloadPaused während Transfer) nicht mehr als downloading in der Anfrageübersicht stehen. Native Queueaufträge erhalten keine Seerr-Schreiboperation.
- Redundantes nachträgliches Seerr-Update im Vorbereitungs-Pausenpfad entfernt. Test mit echtem Queueclaim und DownloadPaused bestätigt queued/pause_requested sowie paused. SQLite-Triggerfehler beim Request-Update beweist gemeinsamen Rollback auf running/downloading.
- `python -m pytest tests/test_seerr_processing.py tests/test_db_queue.py tests/test_worker.py -q`: `191 passed in 45.89s`, Exit 0, ATOMIC_PAUSE_TESTS.txt. Aktueller Lauf umfasst auch die Entfernung des redundanten Updates.
- Gesamtziel aktiv; finale Paket-/Snapshotaktualisierung und Laufzeitabnahmen bleiben offen. Computerbedienung weiterhin ausgesetzt.


## Atomarer Status beim Fortsetzen
- resume_queue_item aktualisiert bei zuvor paused markierten Seerr-Anfragen den Status auf waiting gemeinsam mit dem Entfernen der Queuepause. BEGIN IMMEDIATE serialisiert Lesen und Schreiben. Fortschritt und bestehende Pausenzeiterfassung bleiben erhalten.
- Andere Anfragezustände (manual_review/partial/waiting_retry) werden nicht als manuell genehmigt umgedeutet; die bestehende erneute Workerprüfung bleibt erforderlich.
- Triggerbasierte Fehlerinjektion beim Request-Update beweist: Anfrage bleibt paused und Queuepause bleibt gesetzt; danach erfolgreicher Resume und echter Queueclaim. Drei zusätzliche Zustandsfälle prüfen unveränderte Review-/Retryinformationen.
- `python -m pytest tests/test_seerr_processing.py tests/test_db_queue.py tests/test_worker.py -q`: `195 passed in 28.02s`, Exit 0; ATOMIC_RESUME_TESTS.txt. Gesamtziel aktiv, Computerbedienung ausgesetzt, finale Artefakte und externe Abnahmen weiterhin offen.


## Sofortiger Abbruch synchronisiert Seerr atomar
- cancel_queue_item verwendet BEGIN IMMEDIATE und schreibt für wartende sowie erzwungen abgebrochene Seerr-Aufträge den Requeststatus cancelled in derselben Transaktion wie den Queuestatus. Wartende Aufträge bleiben nicht dauerhaft als waiting/manual_review sichtbar, wenn sie nie mehr vom Worker beansprucht werden.
- Regulärer Abbruch eines laufenden Transfers setzt weiterhin nur cancel_requested; der Worker beendet ihn gemäß vorhandener Semantik. Keine vorzeitige Behauptung eines beendeten Downloads.
- Drei triggerbasierte Fehlerinjektionen prüfen vollständigen Rollback einschließlich aller Queuefelder für queued/normal, queued/force und running/force; danach erfolgreicher Abbruch beider Datensätze. Separater Fall bestätigt unverändertes Graceful-Cancel-Verhalten.
- `python -m pytest tests/test_seerr_processing.py tests/test_db_queue.py tests/test_worker.py -q`: `199 passed in 28.37s`, Exit 0; ATOMIC_CANCEL_TESTS.txt. Gesamtziel aktiv; finale Artefakte und externe Laufzeitabnahmen offen, Computerbedienung ausgesetzt.


## Verspätete Pause überschreibt keinen Abbruch
- finish_pause aktualisiert nur queued/running-Zeilen ohne cancel_requested. Ein inzwischen gesetzter Abbruch oder terminaler Zustand bleibt unverändert; ohne erfolgreiche Queueänderung wird auch der Seerr-Status nicht mehr verändert.
- Vier Regressionen vergleichen vollständige Queue- und Requestdatensätze vor/nach verspätetem finish_pause: cancelled, completed, failed sowie running mit regulärem Abbruchwunsch. Keine Wiederbelebung als queued/paused.
- `python -m pytest tests/test_seerr_processing.py tests/test_db_queue.py tests/test_worker.py -q`: `203 passed in 29.21s`, Exit 0; LATE_PAUSE_GUARD_TESTS.txt. Gesamtziel aktiv, Computerbedienung ausgesetzt; finale Artefakte/Laufzeitabnahmen offen.


## Vollständige Regression nach Queue-Statusänderungen
- `python -m pytest -q`: `2083 passed, 1 skipped in 171.48s (0:02:51)`, Exit 0; CURRENT_FULL_TEST_9.txt. Gesamtlauf umfasst neue Vorlagen-, Bytefortschritt-, Migration-, Recovery-, Pause-/Resume-/Cancel- und verspätete-Pause-Regressionen.
- Beide Seerr-JavaScript-Dateien bestehen node --check; git diff --check Exit 0. Kein Browser bedient.
- Abnahmebericht korrigiert: aktueller Gesamttest eindeutig ausgewiesen, älterer Wheel-Nachweis als historisch markiert, Snapshot-6-Teststand statt überholter erster Snapshotzahl genannt. CHANGELOG-Entwurf ergänzt tatsächliche Status-/Timeout-/UI-Änderungen; weiterhin in verification.
- Gesamtziel aktiv. Aktuelles finales Paket/Rollback sowie offene externe Laufzeitabnahmen sind damit nicht behauptet.


## Abbruchpriorität beim verspäteten Abschluss
- Seerr-Dateiabschluss liest den aktuellen Queuezustand innerhalb seiner BEGIN-IMMEDIATE-Transaktion. Bereits stornierte/erzwungen abgebrochene Aufträge bleiben cancelled, selbst wenn ein veralteter Workerzustand completed, partial oder failed melden will. Bei inzwischen entfernter Queuezeile erfolgt kein nachträglicher Abschluss.
- Drei Regressionen übergeben absichtlich den vor force-cancel gelesenen Auftrag; Queue/Request bleiben cancelled, Forceflag erhalten, keine partial-Pause und ausschließlich cancelled-Benachrichtigung.
- `python -m pytest tests/test_seerr_processing.py tests/test_worker.py tests/test_db_queue.py -q`: `206 passed in 29.29s`, Exit 0, LATE_FINISH_CANCEL_TESTS.txt. Bestehende reguläre Abbruch-/Abschlussfälle bleiben grün.
- Gesamtziel aktiv; aktuelle Änderung nach CURRENT_FULL_TEST_9 erfordert erneute finale Gesamt-/Paketprüfung. Computerbedienung bleibt ausgesetzt.


## Manuelle Freigabe bleibt an bisherigen Kandidatenstand gebunden
- prepare prüft bei manuell ausgewähltem Anbieter die frische Detailidentität gegen den bisherigen gespeicherten Kandidaten, bevor aktuelle Suchtreffer diesen Stand ersetzen. Dadurch gilt eine alte Freigabe nicht stillschweigend für neue Metadaten an derselben URL.
- Regression injiziert eine veränderte freigegebene Seite: Prüfer sieht den alten Kandidatenstand, dieser bleibt erhalten, Request geht in manual_review und Queue pausiert. Bestehende Fresh-Detailtests prüfen die konkreten Titel-/ID-/Jahr-/Datumsvergleiche.
- `python -m pytest tests/test_seerr_processing.py tests/test_seerr_download_identity.py -q`: `91 passed in 23.37s`, Exit 0, REVIEW_SNAPSHOT_BINDING_TESTS.txt.
- Änderung folgt auf Snapshot 10/Wheel 10; beide bleiben unveränderliche historische Nachweise, nicht das aktuelle Release. Gesamtziel aktiv, Computerbedienung ausgesetzt.


## Manuelle Seitenänderung mit echtem Prüfer
- Ergänzter Workerfall verwendet echten enrich_candidate/revalidate_selection mit kontrolliertem Provider-Modell statt injizierter Prüfausnahme. Suchtreffer und Detailmodell melden eine andere lokale Titelidentität als der gespeicherte Freigabestand.
- Worker pausiert vor Episode-Konstruktion, Anfrage bleibt manual_review und bisherige Kandidatenevidenz unverändert. Kein Netzwerkzugriff und keine Medienbytes.
- `python -m pytest tests/test_seerr_processing.py tests/test_seerr_download_identity.py -q`: `92 passed in 30.71s`, Exit 0; REAL_REVIEW_REVALIDATION_TESTS.txt. Gesamtziel aktiv; Computerbedienung ausgesetzt und aktuelle Paket-/Gesamtabnahme weiterhin offen.


## Gesamtprüfung der manuellen Freigabebindung
- `python -m pytest -q`: `2088 passed, 1 skipped in 177.75s (0:02:57)`, Exit 0; CURRENT_FULL_TEST_11.txt. Keine offenen Testfehler in diesem Lauf.
- Dieser Lauf enthält die Änderung nach Snapshot 10; dessen Archiv und Wheel bleiben historische, unveränderte Nachweise. Die laufende Abnahmematrix nennt nun den aktuellen Gesamttest.
- Externe Laufzeitnachweise und vollständige Abschlussabnahme weiterhin offen; Computerbedienung auf Nutzerwunsch ausgesetzt. Gesamtziel nicht abgeschlossen.


## Wiederaufnahme nach wiederhergestellter Anbieterseite
- Pipelinefall mit echtem Identitätsprüfer: geänderter lokaler Titel pausiert vor Episode-Konstruktion. Nach Wiederherstellung des zuvor geprüften Titels und explizitem retry_request läuft derselbe Queueauftrag erfolgreich durch; genau eine synthetische Datei und weiterhin genau eine Queuezeile.
- `python -m pytest tests/test_seerr_processing.py tests/test_seerr_download_identity.py -q`: `93 passed in 21.27s`, Exit 0; REVIEW_RESTORE_RETRY_TESTS.txt. Keine weitere Produktcodeänderung nötig. Dauerhaft geänderte Anbieterinformation bleibt davon getrennt neu zu prüfen.
- Gesamtziel aktiv; externe Testumgebung angefragt, Computerbedienung ausgesetzt. Keine vollständige externe Abnahme behauptet.
