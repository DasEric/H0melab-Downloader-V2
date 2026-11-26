"""Lossless return of connector data that version 2.4.0 archived or parked."""

import json

import pytest

from h0melab.web import connector_files, db, legacy_migration

ARTIFACT_SCHEMA = """CREATE TABLE connector_artifacts (
        id TEXT PRIMARY KEY, request_key TEXT NOT NULL, queue_id INTEGER NOT NULL,
        item_index INTEGER NOT NULL, root TEXT NOT NULL, relative_path TEXT NOT NULL,
        custom_path_id INTEGER, media_type TEXT NOT NULL, language TEXT NOT NULL,
        fingerprint TEXT NOT NULL, size INTEGER NOT NULL, state TEXT NOT NULL,
        streams TEXT NOT NULL DEFAULT '[]', UNIQUE(request_key, item_index, relative_path))"""
STATE_SCHEMA = (
    "CREATE TABLE connector_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
)
ARTIFACT = {
    "id": "b" * 32,
    "request_key": "a" * 32,
    "queue_id": 7,
    "item_index": 0,
    "root": "/media/shows",
    "relative_path": "Show/Season 01/Show - S01E01 - german-dub.mkv",
    "custom_path_id": 2,
    "media_type": "series",
    "language": "German Dub",
    "fingerprint": "1:2:3:4:abc",
    "size": 3,
    "state": "owned",
    "streams": "[]",
}


def archive(conn, name, schema, rows):
    conn.execute("""CREATE TABLE IF NOT EXISTS migration_archive (
        name TEXT PRIMARY KEY, schema_sql TEXT NOT NULL, rows_json TEXT NOT NULL,
        archived_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    conn.execute(
        "INSERT INTO migration_archive(name,schema_sql,rows_json) VALUES (?,?,?)",
        (name, schema, json.dumps(rows)),
    )


def drop_connector_tables(conn):
    conn.execute("DROP TABLE IF EXISTS connector_state")
    conn.execute("DROP TABLE IF EXISTS connector_artifacts")


def test_archived_receipts_and_instance_identity_come_back(queue_item):
    queue_item()
    with db.session() as conn:
        drop_connector_tables(conn)
        archive(
            conn,
            "connector_state",
            STATE_SCHEMA,
            [{"key": "instance_id", "value": "c" * 32}],
        )
        archive(conn, "connector_artifacts", ARTIFACT_SCHEMA, [ARTIFACT])
        legacy_migration.restore_connector_integration(conn)
    # The connector recognises the same downloader instance and its receipts.
    assert connector_files.capabilities()["instance_id"] == "c" * 32
    rows = connector_files.query(["a" * 32])
    assert [(row["id"], row["media_type"]) for row in rows] == [("b" * 32, "series")]


def test_restore_is_repeatable_and_never_overwrites_newer_data(queue_item):
    queue_item()
    with db.session() as conn:
        drop_connector_tables(conn)
        archive(
            conn,
            "connector_state",
            STATE_SCHEMA,
            [{"key": "instance_id", "value": "c" * 32}],
        )
        conn.execute(STATE_SCHEMA)
        conn.execute(
            "INSERT INTO connector_state VALUES ('instance_id', ?)", ("d" * 32,)
        )
        legacy_migration.restore_connector_integration(conn)
        legacy_migration.restore_connector_integration(conn)
        assert [
            tuple(row) for row in conn.execute("SELECT value FROM connector_state")
        ] == [("d" * 32,)]
        # The archive itself is kept as the lossless record.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM migration_archive WHERE name='connector_state'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "original_status,expected", [("queued", "queued"), ("running", "queued")]
)
def test_parked_connector_job_is_restored_exactly(
    queue_item, original_status, expected
):
    qid = queue_item()
    entries = [
        {
            "url": "https://example.test/episode",
            "connector_protocol": 1,
            "custom_path_id": 3,
        }
    ]
    with db.session() as conn:
        conn.execute(
            "UPDATE download_queue SET episodes=?, status=?, errors='[]' WHERE id=?",
            (json.dumps(entries), original_status, qid),
        )
        original = dict(
            conn.execute("SELECT * FROM download_queue WHERE id=?", (qid,)).fetchone()
        )
        archive(conn, f"retired-request:{qid}", "download_queue", original)
        # What 2.4.0 left behind: marker removed, job parked for review.
        parked = [
            {
                key: value
                for key, value in entries[0].items()
                if key != "connector_protocol"
            }
        ]
        conn.execute(
            "UPDATE download_queue SET episodes=?, source='legacy_review', status='queued', "
            "pause_requested=1, errors=? WHERE id=?",
            (json.dumps(parked), json.dumps([{"error": "Retired"}]), qid),
        )
        legacy_migration.restore_connector_integration(conn)
    item = db.get_queue_item(qid)
    assert item["source"] == original["source"]
    assert item["status"] == expected
    assert item["pause_requested"] == original["pause_requested"]
    assert json.loads(item["episodes"]) == entries
    assert json.loads(item["errors"]) == []


def test_parked_job_without_archive_stays_for_review(queue_item):
    qid = queue_item()
    with db.session() as conn:
        conn.execute(
            "UPDATE download_queue SET source='legacy_review', pause_requested=1 WHERE id=?",
            (qid,),
        )
        legacy_migration.restore_connector_integration(conn)
    assert db.get_queue_item(qid)["source"] == "legacy_review"


def test_a_job_the_user_already_handled_is_not_restored(queue_item):
    qid = queue_item()
    entries = [{"url": "https://example.test/episode", "connector_protocol": 1}]
    with db.session() as conn:
        conn.execute(
            "UPDATE download_queue SET episodes=? WHERE id=?",
            (json.dumps(entries), qid),
        )
        original = dict(
            conn.execute("SELECT * FROM download_queue WHERE id=?", (qid,)).fetchone()
        )
        archive(conn, f"retired-request:{qid}", "download_queue", original)
        conn.execute(
            "UPDATE download_queue SET episodes='[]', status='cancelled' WHERE id=?",
            (qid,),
        )
        legacy_migration.restore_connector_integration(conn)
    item = db.get_queue_item(qid)
    assert item["status"] == "cancelled" and json.loads(item["episodes"]) == []


def test_tampered_archive_schema_is_refused(queue_item):
    queue_item()
    with db.session() as conn:
        drop_connector_tables(conn)
        archive(conn, "connector_state", "CREATE TABLE users_copy (x)", [])
        with pytest.raises(ValueError):
            legacy_migration.restore_connector_integration(conn)


def test_database_start_runs_the_restore(queue_item, monkeypatch):
    queue_item()
    with db.session() as conn:
        drop_connector_tables(conn)
        archive(conn, "connector_artifacts", ARTIFACT_SCHEMA, [ARTIFACT])
    monkeypatch.setattr(db, "_initialized", False)
    db.init_db()
    assert [row["id"] for row in connector_files.query(["a" * 32])] == ["b" * 32]
