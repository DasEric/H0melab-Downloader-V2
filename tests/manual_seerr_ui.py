"""Isolated browser fixture: python tests/manual_seerr_ui.py (localhost:8766).

Uses disposable settings/database, no background services and no provider traffic.
Only request acceptance/review/retry and settings APIs run for real.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SANDBOX = Path(tempfile.mkdtemp(prefix="h0melab-seerr-ui-"))
for key in list(os.environ):
    if key.startswith(("H0MELAB_", "ANIWORLD_", "MANGAFIRE_")):
        del os.environ[key]
os.environ["H0MELAB_INSTALL_FOLDER"] = str(SANDBOX / "config")
os.environ["H0MELAB_DOWNLOAD_PATH"] = str(SANDBOX / "downloads")
os.environ["H0MELAB_UI_LANGUAGE"] = "de"

from h0melab.web import app as web_app, db, media_targets, seerr

web_app._start_background_services = lambda: None
application = web_app.create_app()
mounts = []
for kind in ("movie", "series"):
    folder = SANDBOX / kind
    folder.mkdir()
    mounts.append(db.add_custom_path(kind, str(folder)))
media_targets.save(*mounts, 0)
for rid, status in ((9101, "manual_review"), (9102, "partial")):
    payload = seerr.parse_payload({
        "notification_type": "MEDIA_APPROVED", "request_id": rid,
        "media_type": "tv", "media_tmdbid": 1405, "subject": f"UI Fixture {rid}",
        "seasons": [1],
    })
    payload["seasons"] = [1]
    qid, _ = seerr.accept(payload)
    url = "https://serienstream.to/serie/ui-fixture"
    seerr.update(rid, status=status, identity_status="manual_review", identity={
        "title": f"UI Fixture {rid}", "media_type": "tv", "tmdb_id": 1405,
        "imdb_id": "tt0773262", "release_year": 2006,
    }, candidates=[{"url":url,"title":"UI Fixture","source":"sto"}],
        episode_review={"provider_url":url,
            "canonical":[{"season_number":1,"episode_number":1,"name":"Pilot"}],
            "source":[{"url":url+"/staffel-1/episode-1","season_number":1,"episode_number":1,"title_de":"Pilot"}],
            "pending":[{"season_number":1,"episode_number":2,"name":"Future episode","reason":"not_yet_aired","air_date":"2099-01-01"}] if status=="partial" else []})
    with db.session() as conn:
        conn.execute("UPDATE download_queue SET status='failed' WHERE id=?", (qid,))
print(f"SANDBOX={SANDBOX}", flush=True)
application.run(host="127.0.0.1", port=8766, use_reloader=False)
