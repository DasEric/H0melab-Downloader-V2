"""Lossless return of the Jellyfin connector integration.

Version 2.4.0 archived the connector tables into ``migration_archive`` and parked
active connector jobs as ``legacy_review``. The connector is supported again, so
this migration restores exactly what was archived instead of retiring it on every
start. The archive itself is kept. Nothing is restored over newer data: an
existing table or a job that is no longer parked stays untouched.
"""

import json
import re

_TABLES = ("connector_state", "connector_artifacts")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def restore_connector_integration(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS migration_archive (
        name TEXT PRIMARY KEY, schema_sql TEXT NOT NULL, rows_json TEXT NOT NULL,
        archived_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    for table in _TABLES:
        _restore_table(conn, table)
    _restore_parked_requests(conn)


def _restore_table(conn, table):
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return
    archived = conn.execute(
        "SELECT schema_sql, rows_json FROM migration_archive WHERE name=?", (table,)
    ).fetchone()
    if archived is None:
        return
    schema = archived[0]
    # Only the original definition of this very table is replayed.
    if not re.match(rf'\s*CREATE TABLE\s+"?{table}"?\s*\(', schema, re.IGNORECASE):
        raise ValueError(f"Archived schema for {table} is not a table definition")
    rows = json.loads(archived[1])
    if not isinstance(rows, list):
        raise TypeError(f"Archived rows for {table} are invalid")
    conn.execute(schema)
    for row in rows:
        if not isinstance(row, dict) or not row:
            raise ValueError(f"Archived row for {table} is invalid")
        columns = list(row)
        if any(not _IDENTIFIER.fullmatch(column) for column in columns):
            raise ValueError(f"Archived column for {table} is invalid")
        conn.execute(
            f'INSERT INTO "{table}" ({",".join(columns)}) '
            f"VALUES ({','.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )


def _restore_parked_requests(conn):
    parked = conn.execute(
        "SELECT id FROM download_queue WHERE source='legacy_review'"
    ).fetchall()
    for (queue_id,) in parked:
        archived = conn.execute(
            "SELECT rows_json FROM migration_archive WHERE name=?",
            (f"retired-request:{queue_id}",),
        ).fetchone()
        if archived is None:
            continue
        try:
            original = json.loads(archived[0])
            entries = json.loads(original["episodes"])
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(entries, list) or not any(
            isinstance(entry, dict) and entry.get("connector_protocol") == 1
            for entry in entries
        ):
            continue
        status = original.get("status")
        # A job that was running when parked restarts from its saved progress.
        if status == "running":
            status = "queued"
        conn.execute(
            "UPDATE download_queue SET episodes=?, source=?, status=?, "
            "pause_requested=?, errors=? WHERE id=? AND source='legacy_review'",
            (
                original["episodes"],
                original.get("source") or "manual",
                status or "queued",
                int(original.get("pause_requested") or 0),
                original.get("errors") or "[]",
                queue_id,
            ),
        )
