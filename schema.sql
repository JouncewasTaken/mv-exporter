-- mv-exporter landing schema (bronze layer): faithful raw capture, refine later.
-- Pure SQLite; portable across macOS and Linux. DDL only; PRAGMAs set per-connection in db.py.

CREATE TABLE IF NOT EXISTS ingest_run (
    run_id        TEXT PRIMARY KEY,
    module        TEXT NOT NULL,
    host          TEXT NOT NULL,
    started_at    TEXT NOT NULL,          -- ISO-8601 UTC
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','complete','failed')),
    rows_seen     INTEGER NOT NULL DEFAULT 0,
    rows_upserted INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);

-- Resumability for long, chunked passes (the GL-timeout problem).
CREATE TABLE IF NOT EXISTS ingest_checkpoint (
    module       TEXT NOT NULL,
    cursor_key   TEXT NOT NULL,           -- e.g. 'date_posted' or 'seq'
    cursor_value TEXT NOT NULL,
    run_id       TEXT NOT NULL REFERENCES ingest_run(run_id),
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (module, cursor_key)
);

-- Faithful raw landing. record_key = external_id if the source provides one,
-- else the payload hash. Upsert on (module, record_key) => idempotent loads.
CREATE TABLE IF NOT EXISTS landing_record (
    module          TEXT NOT NULL,
    record_key      TEXT NOT NULL,
    external_id     TEXT,                 -- natural key from source, NULL if none
    source_row_hash TEXT NOT NULL,        -- sha256 of canonical raw payload
    raw_json        TEXT NOT NULL,        -- record exactly as extracted
    revision        INTEGER NOT NULL DEFAULT 1,   -- bumps when the hash changes
    first_seen_run  TEXT NOT NULL,
    last_seen_run   TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    PRIMARY KEY (module, record_key)
);

CREATE INDEX IF NOT EXISTS idx_landing_module_extid ON landing_record (module, external_id);
CREATE INDEX IF NOT EXISTS idx_landing_hash         ON landing_record (module, source_row_hash);
