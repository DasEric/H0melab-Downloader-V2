"""Lossless retirement of the old external request contract.

Archive rows and original schemas before dropping its exclusive tables. No
media files or queue history are removed. Active legacy requests require review.
"""

import json


def retire_request_integration(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS migration_archive (
        name TEXT PRIMARY KEY, schema_sql TEXT NOT NULL, rows_json TEXT NOT NULL,
        archived_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    for table in ("connector_state", "connector_artifacts"):
        definition = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if definition is None:
            continue
        rows = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
        conn.execute(
            "INSERT INTO migration_archive(name,schema_sql,rows_json) VALUES (?,?,?)",
            (table, definition[0], json.dumps(rows)),
        )
        conn.execute(f'DROP TABLE "{table}"')

    for row in conn.execute("SELECT * FROM download_queue").fetchall():
        try:
            entries = json.loads(row["episodes"])
        except (ValueError, TypeError):
            continue
        if not isinstance(entries, list) or not any(
            isinstance(entry, dict) and "connector_protocol" in entry
            for entry in entries
        ):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO migration_archive(name,schema_sql,rows_json) VALUES (?, ?, ?)",
            (f"retired-request:{row['id']}", "download_queue", json.dumps(dict(row))),
        )
        for entry in entries:
            if isinstance(entry, dict):
                entry.pop("connector_protocol", None)
        if row["status"] in ("queued", "running"):
            try:
                errors = json.loads(row["errors"] or "[]")
            except (ValueError, TypeError):
                errors = []
            if not isinstance(errors, list):
                errors = []
            errors.append({"error": "Retired request integration: review and resubmit through Seerr"})
            conn.execute(
                "UPDATE download_queue SET episodes=?, source='legacy_review', "
                "status='queued',pause_requested=1,errors=? WHERE id=?",
                (json.dumps(entries), json.dumps(errors), row["id"]),
            )
        else:
            conn.execute(
                "UPDATE download_queue SET episodes=? WHERE id=?",
                (json.dumps(entries), row["id"]),
            )
