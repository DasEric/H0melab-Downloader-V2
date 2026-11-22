"""Lossless retirement fixtures, including malformed historical status data."""

import json

import pytest

from h0melab.web import db, legacy_migration


@pytest.mark.parametrize("errors", ["[]", "not-json", "{}", "null"])
def test_legacy_request_is_archived_paused_and_repeatable(queue_item, errors):
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
            "UPDATE download_queue SET episodes=?,errors=? WHERE id=?",
            (json.dumps(entries), errors, qid),
        )
        original = dict(
            conn.execute("SELECT * FROM download_queue WHERE id=?", (qid,)).fetchone()
        )
        legacy_migration.retire_request_integration(conn)
    item = db.get_queue_item(qid)
    assert item["source"] == "legacy_review"
    assert item["pause_requested"] == 1
    assert item["status"] == "queued"
    assert "connector_protocol" not in json.loads(item["episodes"])[0]
    with db.session() as conn:
        archived = conn.execute(
            "SELECT rows_json FROM migration_archive WHERE name=?",
            (f"retired-request:{qid}",),
        ).fetchone()
        assert json.loads(archived[0]) == original
        legacy_migration.retire_request_integration(conn)
        assert conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0] == 1


def test_connector_tables_archived_without_losing_other_tables(queue_item):
    qid = queue_item()
    with db.session() as conn:
        conn.execute("CREATE TABLE connector_state (key TEXT PRIMARY KEY,value TEXT)")
        conn.execute(
            "INSERT INTO connector_state VALUES ('instance_id','old-instance')"
        )
        conn.execute("CREATE TABLE connector_artifacts (id TEXT,relative_path TEXT)")
        conn.execute(
            "INSERT INTO connector_artifacts VALUES ('receipt','Example/file.mkv')"
        )
        legacy_migration.retire_request_integration(conn)
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='connector_artifacts'"
        ).fetchone()
        archived = conn.execute(
            "SELECT rows_json FROM migration_archive WHERE name='connector_artifacts'"
        ).fetchone()
        assert json.loads(archived[0]) == [
            {"id": "receipt", "relative_path": "Example/file.mkv"}
        ]
        legacy_migration.retire_request_integration(conn)
    assert db.get_queue_item(qid)["source"] != "legacy_review"


@pytest.mark.parametrize("failure_point", ["drop", "queue"])
def test_migration_failure_rolls_back_original_tables_and_queue(
    queue_item, failure_point
):
    import sqlite3

    qid = queue_item()
    entries = [{"url": "https://example.test/episode", "connector_protocol": 1}]
    with db.session() as conn:
        conn.execute("CREATE TABLE connector_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO connector_state VALUES ('instance_id','preserved-instance')"
        )
        conn.execute("CREATE TABLE connector_artifacts (id TEXT, relative_path TEXT)")
        conn.execute(
            "INSERT INTO connector_artifacts VALUES ('receipt','Existing/file.mkv')"
        )
        conn.execute(
            "UPDATE download_queue SET episodes=? WHERE id=?",
            (json.dumps(entries), qid),
        )
        original = dict(
            conn.execute("SELECT * FROM download_queue WHERE id=?", (qid,)).fetchone()
        )

    class InterruptedConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if (
                failure_point == "drop"
                and sql.startswith('DROP TABLE "connector_state"')
            ) or (
                failure_point == "queue"
                and sql.startswith("UPDATE download_queue SET episodes=")
            ):
                raise sqlite3.OperationalError("injected migration failure")
            return result

    with (
        pytest.raises(sqlite3.OperationalError, match="injected migration failure"),
        db.session() as conn,
    ):
        legacy_migration.retire_request_integration(InterruptedConnection(conn))
    with db.session() as conn:
        assert (
            dict(
                conn.execute(
                    "SELECT * FROM download_queue WHERE id=?", (qid,)
                ).fetchone()
            )
            == original
        )
        assert tuple(conn.execute("SELECT * FROM connector_state").fetchone()) == (
            "instance_id",
            "preserved-instance",
        )
        assert tuple(conn.execute("SELECT * FROM connector_artifacts").fetchone()) == (
            "receipt",
            "Existing/file.mkv",
        )
        assert conn.execute("SELECT COUNT(*) FROM migration_archive").fetchone()[0] == 0
        legacy_migration.retire_request_integration(conn)
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM migration_archive").fetchone()[0] == 3
        assert (
            json.loads(
                conn.execute(
                    "SELECT rows_json FROM migration_archive WHERE name=?",
                    (f"retired-request:{qid}",),
                ).fetchone()[0]
            )
            == original
        )
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('connector_state','connector_artifacts')"
        ).fetchall()
    assert db.get_queue_item(qid)["pause_requested"] == 1
