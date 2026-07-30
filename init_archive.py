#!/usr/bin/env python3
"""
init_archive.py — create/govern the Multiview archive SQLite database.

This sets up the *container* for the archive: governance/compliance scaffolding
tables (all prefixed with "_") that sit alongside the data tables the extractor
will create on ingest. It does NOT define the fact/dimension tables — those are
created schema-light by the extractor as each form's columns are first seen.

What this gives you:
  _archive_metadata   one-row-per-key facts about the archive (PHI flag, retention,
                      custodian, source system, "not a system of record" disclaimer)
  _extraction_manifest per (form, year) run: rows + columns captured, when, by whom
                      — the completeness/reconciliation record an audit relies on
  _schema_registry    table -> column -> first_seen -> source form
                      — proves which columns were captured (so an absent column is
                        demonstrably "not present in source", not a silent drop)
  _access_log         best-effort attribution: who opened/queried, when, from where
                      (NOTE: attribution, NOT authentication — see module docstring)
  _drive_custody      manual log of physical drive hand-offs (the real access control
                      in the offline encrypted-drive model)

Access-control reality (read this): on a passed-around, APFS-encrypted drive the
encryption passphrase IS the access boundary — anyone who unlocks the disk can open
this .sqlite directly, bypassing any app-level logging. _access_log is therefore
supplementary attribution, not an access control. If you need authenticated per-user
audit, serve the archive (Datasette behind an authenticating proxy) instead.

Usage:
  python3 init_archive.py /path/to/archive.db          # create/upgrade scaffolding
  python3 init_archive.py /path/to/archive.db --whoami # show what attribution is captured here
The helpers (record_extraction, register_columns, log_access) are importable by the extractor.
"""
import sqlite3, sys, os, socket, subprocess, datetime, getpass, argparse, json

SCHEMA = """
CREATE TABLE IF NOT EXISTS _archive_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS _extraction_manifest (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    module        TEXT,          -- AP / GL / AR / DIM
    form_name     TEXT,          -- e.g. VOUCHER_DIST_INQUIRY_F1
    target_table  TEXT,          -- SQLite table the rows landed in
    year          INTEGER,       -- ACCOUNTING_DATE band, NULL for dimensions
    rows_extracted INTEGER,
    columns_captured INTEGER,
    extracted_utc TEXT,
    extracted_by  TEXT,          -- Multiview account used for the pull
    status        TEXT,          -- ok / error
    note          TEXT
);
CREATE TABLE IF NOT EXISTS _schema_registry (
    target_table  TEXT,
    column_name   TEXT,
    source_form   TEXT,
    first_seen_utc TEXT,
    PRIMARY KEY (target_table, column_name)
);
CREATE TABLE IF NOT EXISTS _access_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT,
    os_user    TEXT,
    os_fullname TEXT,
    hostname   TEXT,
    action     TEXT,             -- open / query / export
    detail     TEXT
);
CREATE TABLE IF NOT EXISTS _drive_custody (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT,
    person     TEXT,             -- who received/returned the drive
    direction  TEXT,             -- out / in
    note       TEXT
);
"""

def _utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def mac_identity():
    """Best-effort local attribution on macOS: (short_user, full_name, hostname).
    This is spoofable and is NOT authentication — it records who the OS session claims to be."""
    user = getpass.getuser() or os.environ.get("USER", "?")
    full = ""
    try:  # macOS: real name from the directory service
        full = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        try:
            out = subprocess.run(["dscl", ".", "-read", f"/Users/{user}", "RealName"],
                                 capture_output=True, text=True, timeout=5).stdout
            full = out.split("RealName:", 1)[-1].strip()
        except Exception:
            full = ""
    return user, full, socket.gethostname()

def connect(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    return con

def init(path):
    new = not os.path.exists(path)
    con = connect(path)
    seed = {
        "archive_name":     "Multiview Financial Archive",
        "source_system":    "Multiview (screen-scrape export)",
        "contains_phi":     "1",
        "phi_note":         "Contains patient-identifying detail on financial records; treat as PHI.",
        "custodian_note":   "Custodianship retained by organization per purchase agreement (confirm annually).",
        "retention_floor":  "10 years (verify against AZ A.R.S. and CMS/Medicare hospice requirements).",
        "system_of_record": "NetSuite — this archive is a queryable COPY, not the system of record.",
        "encryption_note":  "Store only on APFS-Encrypted volumes; encryption passphrase is the access control.",
    }
    if new:
        seed["created_utc"] = _utc()
    cur = con.cursor()
    for k, v in seed.items():
        cur.execute("INSERT INTO _archive_metadata(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                    "WHERE _archive_metadata.value IS NULL OR _archive_metadata.value=''", (k, v))
    con.commit()
    log_access(con, "open", "init_archive.py " + ("created" if new else "upgraded") + " scaffolding")
    con.commit()
    print(f"{'Created' if new else 'Upgraded'} archive scaffolding at {path}")
    print("Governance tables:", ", ".join(t for t in
          ("_archive_metadata","_extraction_manifest","_schema_registry","_access_log","_drive_custody")))
    con.close()

# ---- helpers importable by the extractor ----
def log_access(con, action, detail=""):
    u, f, h = mac_identity()
    con.execute("INSERT INTO _access_log(ts_utc,os_user,os_fullname,hostname,action,detail) "
                "VALUES(?,?,?,?,?,?)", (_utc(), u, f, h, action, detail))

def register_columns(con, target_table, columns, source_form):
    ts = _utc()
    con.executemany(
        "INSERT OR IGNORE INTO _schema_registry(target_table,column_name,source_form,first_seen_utc) "
        "VALUES(?,?,?,?)", [(target_table, c, source_form, ts) for c in columns])

def record_extraction(con, module, form_name, target_table, year,
                      rows_extracted, columns_captured, extracted_by, status="ok", note=""):
    con.execute("INSERT INTO _extraction_manifest(module,form_name,target_table,year,"
                "rows_extracted,columns_captured,extracted_utc,extracted_by,status,note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (module, form_name, target_table, year, rows_extracted, columns_captured,
                 _utc(), extracted_by, status, note))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="path to the archive .sqlite file")
    ap.add_argument("--whoami", action="store_true", help="print the attribution captured on this machine")
    args = ap.parse_args()
    if args.whoami:
        u, f, h = mac_identity()
        print(json.dumps({"os_user": u, "os_fullname": f, "hostname": h,
                          "note": "attribution only — spoofable, not authentication"}, indent=2))
        return
    init(args.db)

if __name__ == "__main__":
    main()
