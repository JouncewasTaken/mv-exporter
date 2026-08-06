"""Landing DB access: connection, schema init, idempotent upsert, provenance.

Pure stdlib. Identical behavior on macOS and Linux.
"""
from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_payload(record: dict) -> tuple[str, str]:
    """Return (canonical_json, sha256_hex) for a raw record.

    sort_keys makes the hash stable regardless of field ordering, so re-extracting
    the same source row yields the same hash -> no spurious revision bumps.
    """
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return payload, digest


def connect(db_path: str, *, synchronous: str = "NORMAL") -> sqlite3.Connection:
    """Open a connection with WAL + FK enforcement.

    WAL: readers don't block the long writer pass (lets you query mid-load).
    synchronous=NORMAL is the deliberate default: safe against app crashes, and
    the load is idempotent+resumable so an OS-crash re-run costs nothing. Pass
    'FULL' if you want power-loss durability at ~2-3x write cost.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit txn control
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(f"PRAGMA synchronous={synchronous};")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


def start_run(conn: sqlite3.Connection, module: str) -> str:
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO ingest_run (run_id, module, host, started_at) VALUES (?,?,?,?)",
        (run_id, module, socket.gethostname(), _utcnow()),
    )
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, *, status: str,
               rows_seen: int, rows_upserted: int, error: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE ingest_run SET finished_at=?, status=?, rows_seen=?, rows_upserted=?, error=? "
        "WHERE run_id=?",
        (_utcnow(), status, rows_seen, rows_upserted, error, run_id),
    )


_UPSERT = """
INSERT INTO landing_record
  (module, record_key, external_id, source_row_hash, raw_json,
   revision, first_seen_run, last_seen_run, first_seen_at, last_seen_at)
VALUES (:module, :record_key, :external_id, :hash, :raw, 1,
        :run_id, :run_id, :now, :now)
ON CONFLICT(module, record_key) DO UPDATE SET
  last_seen_run   = excluded.last_seen_run,
  last_seen_at    = excluded.last_seen_at,
  external_id     = excluded.external_id,
  raw_json        = CASE WHEN landing_record.source_row_hash <> excluded.source_row_hash
                         THEN excluded.raw_json ELSE landing_record.raw_json END,
  revision        = landing_record.revision +
                         (landing_record.source_row_hash <> excluded.source_row_hash),
  source_row_hash = excluded.source_row_hash;
"""


def upsert_batch(conn: sqlite3.Connection, module: str, run_id: str,
                 records: Iterable[tuple[Optional[str], dict]]) -> int:
    """Idempotent upsert of (external_id, raw_record) pairs. Fails loud.

    Returns count processed. A malformed record raises immediately (no silent
    drop) with enough context to locate it; the caller marks the run failed and
    the checkpoint is preserved so a resume picks up cleanly.
    """
    now = _utcnow()
    n = 0
    for external_id, record in records:
        if not isinstance(record, dict):
            raise TypeError(f"[{module}] record #{n} is {type(record).__name__}, expected dict: {record!r}")
        raw, digest = canonical_payload(record)
        record_key = external_id if external_id is not None else digest
        conn.execute(_UPSERT, {
            "module": module, "record_key": record_key, "external_id": external_id,
            "hash": digest, "raw": raw, "run_id": run_id, "now": now,
        })
        n += 1
    return n


def get_checkpoint(conn: sqlite3.Connection, module: str, cursor_key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT cursor_value FROM ingest_checkpoint WHERE module=? AND cursor_key=?",
        (module, cursor_key),
    ).fetchone()
    return row["cursor_value"] if row else None


def set_checkpoint(conn: sqlite3.Connection, module: str, cursor_key: str,
                   cursor_value: str, run_id: str) -> None:
    conn.execute(
        "INSERT INTO ingest_checkpoint (module, cursor_key, cursor_value, run_id, updated_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(module, cursor_key) DO UPDATE SET "
        "cursor_value=excluded.cursor_value, run_id=excluded.run_id, updated_at=excluded.updated_at",
        (module, cursor_key, cursor_value, run_id, _utcnow()),
    )
