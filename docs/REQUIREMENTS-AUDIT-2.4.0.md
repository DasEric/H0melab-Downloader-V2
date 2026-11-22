# Aktueller Übergabestand

Die jüngste Nutzeranweisung übernimmt die Produktionsabnahme ausdrücklich selbst.
Externe Live-Tests und Browserbedienung sind daher kein ausstehender lokaler Auftrag;
sie bleiben als nicht durchgeführt gekennzeichnet. Der nachfolgende frühere Audit
ist historisch, inklusive seiner damaligen offenen Live-Gates und Testzahlen.
Aktuelle ausführbare Nachweise befinden sich im externen Evidence-Verzeichnis,
Snapshot 15. Neu hinzugekommen: typgebundener Jellyfin-Scan mit Einstellungen,
Secret-Schutz, begrenzten Requests und Fehlerisolation. README enthält den Ablauf
für die Produktionsabnahme. Keine Behauptung einer bereits erfolgreichen Live-Abnahme.

---

# Anforderungsaudit 2.4.0 — laufende Abnahme

Quelle: vollständiger Nutzerauftrag Eingefügter Text.txt; vorrangige Präzisierung entfernt BurningSeries, Kinox und Cineby. Dieser Audit ist nicht vollständig und stellt keine Freigabe dar. Testnamen allein beweisen keine Laufzeitintegration.

## Explizite 34 Testgates (§20)

| Gate | Aktuelle konkrete Evidenz | Bewertung / zusätzliche Prüfung |
|---|---|---|
| 1 Film genehmigt | test_api_seerr.py approval + test_seerr_processing.py movie_pipeline | Authentifizierter Flask-HTTP→atomarer Queueclaim→Worker→Datei-Test [movie] bestanden; externe Provider/Metadaten kontrolliert |
| 2 Serie genehmigt | approval parser und series_only_requested_season | Gleicher durchgängiger Fall [tv] mit zwei Staffeln bestanden |
| 3 movie Filmziel | movie_pipeline_uses_existing_queue_and_flat_movie_target | Geprüfter Pipelinefall |
| 4 tv Serienziel | series_only_requested_season_and_shared_folder | Geprüfter Pipelinefall |
| 5 ungenehmigt kein Download | test_unapproved_events_are_ignored, API ignored events | API/Parser; kein Einreihen |
| 6 ungültiger Typ | test_payload_rejects_invalid_identity | Parser/API |
| 7 doppelte Webhooks | test_duplicate_and_conflict, queue_request_atomic_idempotency | Persistente Deduplizierung |
| 8 aktuelles Filmziel | test_targets_are_dynamic_and_site_defaults_are_irrelevant[movie] | Zieländerung nach Einreihen geprüft |
| 9 aktuelles Serienziel | gleicher Test [tv] | Zieländerung nach Einreihen geprüft |
| 10 Zieländerung | beide vorigen Fälle | test_destination_change_during_transfer_pins_current_file_only: E01 bleibt am begonnenen Ziel, E02 nutzt neues Ziel; beide Belege geprüft |
| 11 Seitenstandard irrelevant | beide vorigen Fälle mit realem konkurrierendem sto-default | Falscher Mount bleibt leer |
| 12 fehlendes Ziel | media_targets.root/resolve, bestehende Zieltests | test_missing_destination_pauses_before_provider_download [movie/tv]: pausiert ohne Providerkonstruktion |
| 13 TMDB/IMDb-Abgleich | test_tmdb_identity.py verified_identity / ids_never_linked_without_proof | Beide IDs gegengeprüft |
| 14 nur TMDB | verified_identity parametrisiert | Unit geprüft |
| 15 nur IMDb | verified_identity parametrisiert | Unit geprüft |
| 16 falsche TMDB | invalid_ids, wrong_response_identity | Syntax und falsche API-Antwort |
| 17 falsche IMDb | invalid_ids, ids_never_linked_without_proof | Syntax und Konflikt |
| 18 ähnliche Filmtitel | test_seerr_matching.py no_fuzzy_alias_or_remake_selection | Matcher geprüft |
| 19 ähnliche Serientitel | generischer Matcher | test_similar_tv_titles_and_remakes_are_not_automatic_matches: Spin-offs, abweichendes Jahr und ähnlicher Titel abgewiesen |
| 20 Remakes | no_fuzzy_alias_or_remake_selection, conflicts_override_title_score | Jahrkonflikt geprüft |
| 21 movie/tv-Verwechslung | conflicts_override_title_score, wrong_type_is_not_resolved | Matcher/TMDB |
| 22 unklare Treffer | multiple_matching_sources_require_review | Unit; Pipeline Review vorhanden |
| 23 mehrere Staffeln ein Ordner | series_only_requested_season_and_shared_folder prüft nur eine Staffel plus Titeländerung | test_authenticated_webhook_through_worker_to_files[tv] erzeugt S01 und S02 im selben Hauptordner |
| 24 Staffelordner | series_only_requested_season_and_shared_folder | Season 02 geprüft |
| 25 Dateinamen | Film-/Serienpipeline + alternate numbering | Kanonische Ausgabe geprüft |
| 26 Nummernzuordnung | explicit_alternate_episode_numbering_uses_canonical_filename | Quelle vs kanonische Nummer |
| 27 vorhandene Folgen | missing_source_for_completed_episode, all_existing_series | Belegprüfung und Skip |
| 28 fehlende Folgen ergänzen | partial_season_downloads_only_verified_available_episodes_then_retry | Nur zweite Folge geladen; erste unverändert |
| 29 angefragte Staffeln | series_only_requested_season, explicit_source_season | Keine zusätzliche kanonische Staffel |
| 30 falsche Episoden | manual_mapping_does_not_override_foreign_series, partial_season_does_not_bypass_conflicting_episode_review | Konflikte vor Ausgabe |
| 31 bestehende Anbieter | Regressionen/provider_check vorhanden | Live-Funktion aller verbleibenden Anbieter nicht nachgewiesen; drei entfernte explizit ausgenommen |
| 32 alte Integration entfernt | test_removed_providers.py, legacy migration, Wheel-Inspektion | Quelltext-Restaudit durchgeführt: verbleibende aktive Connector-Begriffe sind Migration/Protokollabweisung; obsolete Provider-Docstring entfernt; ignorierte Bytecode-Caches separat vermerkt |
| 33 Daten erhalten | BASELINE.zip + Migrationstests | Historischer Snapshot: 294 Dateien bytegenau restauriert, Baseline-Suite 1854 passed/3 skipped; aktueller finaler Snapshot erneut zu prüfen |
| 34 Downloadfunktion erhalten | Gesamtsuite + bestehende Worker-/Routingtests | Kein Live-Gesamtnachweis allein aus Testanzahl |

## Weitere ausdrücklich erforderliche Abnahmen

- Abschnitte 1–22 sind inzwischen in der folgenden Abschnittsmatrix zugeordnet; explizite Restabnahmen bleiben offen.
- Browserlaufzeit von Settings, Anfrageübersicht, Review-Mapping und Retry: unverifiziert. Benutzer hat Computerbedienung per Escape beendet und ausdrücklich weiter ausgesetzt; Fixture beendet. Keine erneute Browserbedienung.
- Docker: compose config gültig; Engine war nicht verfügbar, kein erfolgreicher Container-Build/-Lauf.
- Releaseversion: aktive Paketversion 2.4.0; vollständiger Quellen-/Assetcheck vor Abschluss wiederholen.
- Discord: notification-only, Deduplizierungstests vorhanden; Live-Zustellung nicht nachgewiesen.
- Frische Identität vor Download: separate Detailauswertung ergänzt; 100 gezielte Tests bestanden. Pause/Abbruch während Prüfung: weitere 40 Tests bestanden.
- Bestand: NFO-identifizierter Serienordner wird jetzt gebunden; mehrere/widersprüchliche Ordner pausieren. test_missing_episode_joins_nfo_identified_existing_folder ergänzt nur E02 im bestehenden lokalisierten Ordner; E01 byte-/zeitstempelgleich, kein zweiter Ordner.
- Artefakte MODIFIED_FILE, DIFF_FILE, VERIFICATION.txt und ausführbares ROLLBACK.sh existieren mit unabhängig verifiziertem historischem Snapshot 6 (2038 passed/1 skipped; Rollback 1854 passed/3 skipped). Nach späteren Änderungen ist ihre Aktualisierung vor Abschluss weiterhin offen.
- Aktuelle Gesamtsuite CURRENT_FULL_TEST_11.txt: `2088 passed, 1 skipped in 177.75s (0:02:57)`, Exit 0. Dies ersetzt keine fehlenden Laufzeit-/Anforderungsnachweise.


### Nachtrag: durchgängige Pipelinefälle
`python -m pytest tests/test_seerr_processing.py -q`: `36 passed in 10.00s`, Exit 0. Log: `C:/Users/Eric/Documents/GitHub/H0melab-Downloader-V2-evidence-240/SEERR_END_TO_END_TESTS.txt`.
Die HTTP-Fälle benutzen Flask-Testclient mit aktivierter App-Authentifizierung und eigenem Webhook-Secret, echte SQLite-Queue/Claim, Worker und Dateirouting. Externe Metadaten-/Providerantworten und Medienbytes sind kontrollierte Fixtures, kein behaupteter Live-Providerdownload. Doppelte Webhooks nach Abschluss erzeugen keine weitere Arbeit.


## Abschnittsübergreifender Audit (keine Freigabe)

| Abschnitt | Implementierung/Evidenz | Offene Abnahme |
|---|---|---|
| 1 Gesamtfluss / bestehende Engine | api_seerr → seerr.accept → download_queue → worker → bestehendes Episode.download; HTTP-Pipelinefälle | Live-Kompatibilität aller Provider nicht aus Fixtures ableiten |
| 2 Bestandsaufnahme/Plan | IMPLEMENTATION-2.4.0.md mit initialer Inventarisierung/Plan; BASELINE.zip enthält Nutzerstand | Vollständigkeit der Inventarisierung gegen ursprüngliche Datei noch abgleichen |
| 3 alte Anfrageintegration entfernen | api_connector/connector_files entfernt, media_files extrahiert, legacy_migration archiviert exklusive Daten, API-Registrierung geändert | Restkonfiguration/Übersetzungen/Abhängigkeiten systematisch durchsuchen; ursprüngliche Nutzeränderungen erhalten |
| 4 Webhook | api_seerr + seerr.parse_payload; konstante Secret-Prüfung, 64-KiB-Grenze, approved-only, Konfliktprüfung, persistente Idempotenz | Optionale Originaltitel/Jahr/Datum werden validiert im Payload gespeichert; offizielle Dokumentation/KeyMap erneut abgeglichen; acht HTTP-Fälle laden echte README-Vorlage und prüfen movie/tv, beide Approval-Ereignisse, leere/vorhandene IMDb-ID. Kein Live-Seerr-Transportnachweis |
| 5 Ziele | media_targets.resolve, zentrale eigene Pfade, keine zweite Pfadkonfiguration, output() löst Typziel frisch auf | Transferwechsel jetzt im Worker-Pipelinefall geprüft und dokumentiert; laufende Datei gebunden, nächste Episode aktuelles Ziel, vorhandene Belege bleiben gültig |
| 6 TMDB/IMDb | vorhandenes tmdb-Modul erweitert; find/external_ids/details/season; ID-Konflikte, Typ, Alias/Jahr; frische Providerdetails vor Ausgabe | TV-only-TMDB, TV-only-IMDb und beide IDs jetzt explizit geprüft; Erscheinungsdatum jetzt mit Kalender-/Jahr-/TMDB-Konsistenz und frischer Review-Bindung geprüft; Identitätsfehler vor Download nun manual_review statt failed |
| 7 Anbieterabgleich | assess_candidate: IDs hoch gewichtet, Konflikte hart, Titel allein unzureichend, eindeutige Auswahl | Providerweite Auswertung/Feldverfügbarkeit/Timeouts auditieren |
| 8 Episoden | kanonische TMDB-Staffeln, assess_episode, explizite alternative Quellnummern, source season, ID-Konflikte unverhandelbar | Frische Quellenenumeration prüft URL-Eindeutigkeit, Nummern, Serien-IDs und Episodentitel unmittelbar vor Ausgabe; manuelle Mappings bleiben an geprüfte Quelltitel gebunden |
| 9 Queue | einzige bestehende Queue, atomarer Claim, request-ID Dedupe, OS-Workerlock, Belege, persistenter Backoff | Atomare Pausen-/Resume-/Abbruch-/Review-/Abschlusstransaktionen; verspätete Pause erhält Abbruch/Terminalzustand, verspäteter Abschluss erhält force-cancel ohne Erfolgsmeldung; äußerer Fehlerpfad per Fehler-Injektion geprüft; Recovery von checking/downloading mit/ohne Pause und Erhalt von completed/already_present/manual_review/partial/waiting_retry/failed parametrisch geprüft |
| 10 Filme | flach unter aktuellem Filmziel, Originalendung, Belege, unbekannte Datei nicht überschreiben | Unbekannte Zieldateien bei Film/Serie jetzt explizit manual_review, unveränderte Bytes/mtime, kein Download/Beleg; Provider-Ausgabeformat/Pfadadapter weiter prüfen |
| 11 Serien | TMDB-Ordnerbindung, Season XX, SXXEXX, nur angefragte Staffeln, vorhandene Belege/NFOs | Season 00 und Zielwechsel mit vorhandenem Beleg explizit geprüft; mehrdeutiger Altbestand wird pausiert, Live-Jellyfinprüfung offen |
| 12 Jellyfin | kanonische Struktur und lokale NFO-Erkennung | Kein tatsächlicher Jellyfin-Bibliotheksscan nachgewiesen |
| 13 WebUI | Settings enable/secret/endpoint/status/local-test, /seerr Felder und Mapping/Retry | Browserbedienung ausdrücklich ausgesetzt; Laufzeit unverifiziert. Pagination erreicht ältere Einträge; Episoden- und Bytefortschritt implementiert, API-Werte für bekannte/unbekannte Gesamtlänge geprüft. Fehler-/Statustexte weiter auditieren |
| 14 Review | gespeicherte Kandidaten, Begründung, one-to-one Mapping, erneute Verifikation, keine ID-Überstimmung | Browserbedienung offen; Review-Transaktion mit DB-Fehler-Injektion und konkurrierendem Abbruch geprüft, konkurrierende review/review, retry/retry und review/retry geprüft: genau eine Aktion übernimmt; Fehler-Injektion beweist gemeinsamen Rollback |
| 15 Discord | vorhandener Bot notification-only, seerr_events Dedupe, definierte Statusereignisse | Transportfehler und 16 konkurrierende Replays getestet; Best-Effort/At-most-once dokumentiert. Live-Ausgabe weiterhin nicht nachgewiesen |
| 16 optionaler Rückkanal | offizielle API zuvor ausgewertet, Seerr mediaId vs TMDB-ID dokumentiert | Nicht implementiert; ausdrücklich optional, kein Kerngate |
| 17 SQLite/Migration | additive Seerr-Daten, Belege, folder nach Typ/TMDB, archive legacy, bestehende db.session | Snapshot 6: 294 Originaldateien bytegenau restauriert, Baseline-Suite 1854 passed/3 skipped; SQL-Archivmigration zusätzlich mit Abbruch nach DROP/Queue-UPDATE geprüft: Originaltabellen und volle Queuezeile wiederhergestellt, erneute Migration erfolgreich. Kein behaupteter Rollback einer produktiven SQLite-Datei |
| 18 Fehler | begrenzte TMDB-Wiederholungen, SearchResults.error erkennbar, generische Providerfehler ohne Geheimnisse, Review/Cancel/Pause | Aniworld-Suche jetzt timeout=15 und HTTP-Statusprüfung; Timeout/HTTP/JSON-Fehler getestet. Aniworld-Serien-/Staffel-/Episoden-HTML jetzt timeout=15 und HTTP-Statusprüfung, Fehlercache/Recovery getestet; Hoster-Redirects Aniworld/FilmPalast/MegaKino jetzt Timeout/HTTP-geprüft; fehlerhafte URLs werden nicht gecacht (7 Regressionen). Fehlschlag auch des Fehlerstatus-Schreibens über echte äußere Recovery-Schleife getestet: derselbe Auftrag und Folgeauftrag laufen nach DB-Erholung weiter, keine Secret-Traceausgabe. Übrige Provider/Logpfade weiter auditieren |
| 19 Docker | Compose validiert, bestehende Volumes erhalten, env-Beispiele aktualisiert | Kein Containerbuild/-Start: Docker-Engine war nicht aktiv |
| 20 Tests | obige 34-Gate-Matrix | CURRENT_FULL_TEST_11 nach manueller Freigabebindung bestanden (2088/1 skipped); externe Laufzeitnachweise bleiben separat offen |
| 21 Dokumentation | README Seerr-Setup/Template/Pfade/Matching/Retry/Migration; Implementierungsprotokoll | Endgültigen Stand gegen Verhalten prüfen, Releasehinweise finalisieren |
| 22 Implementierungsphasen/Endzustand | Phasen im Implementierungsprotokoll verfolgt; Version 2.4.0 | Keine Abschlussfreigabe solange notwendige Gaps/Artefakte offen |

### Korrektur Identitätsstatus
`IdentityReviewRequired` trennt eigene Identitätsfehler vor Bytes vom Downloadfehler. Worker hält current_episode unverändert, pausiert denselben Auftrag und setzt manual_review. `python -m pytest tests/test_seerr_download_identity.py tests/test_seerr_processing.py tests/test_worker.py -q`: `82 passed in 15.09s`, Exit 0; Log SEERR_IDENTITY_REVIEW_TESTS.txt. Externe Ausnahmetexte werden dadurch nicht pauschal freigegeben.


### TV-spezifische Identitätsnachweise
`test_tv_identity_uses_typed_endpoints_and_external_ids` deckt drei ID-Konstellationen mit TV-Details/external_ids/alternative_titles ab. IMDb-find enthält absichtlich zusätzlich ein fremdes movie_results-Objekt; nur tv_results entscheidet. Konflikt/Mehrdeutigkeit sowie doppelte, boolesche und negative Staffelnummern werden abgewiesen. `python -m pytest tests/test_tmdb_identity.py tests/test_seerr_matching.py tests/test_seerr_processing.py -q`: `101 passed in 25.19s`, Exit 0, TV_IDENTITY_TESTS.txt.


### Historischer Paketnachweis (vor späteren Änderungen)
Erneuter isolierter Wheel-Build per `python -m pip wheel . --no-deps --wheel-dir ../H0melab-Downloader-V2-evidence-240/current-wheels` erfolgreich (Exit 0). ZIP-Integrität, Paketversion 2.4.0, Abwesenheit der entfernten Provider/Connector-Module und Bytegleichheit aller Seerr-Kernmodule/Assets zum Worktree geprüft: CURRENT_WHEEL_VERIFY=PASS, Exit 0. SHA256 `7ddb27942a176b51a73a613eb9e1fd5e7eec4258fda15b6be293304550be5bc0`. Dies ist kein Docker-Laufzeitnachweis. CURRENT_FULL_TEST_5.txt abgeschlossen: `2029 passed, 1 skipped in 168.16s (0:02:48)`, Exit 0.
