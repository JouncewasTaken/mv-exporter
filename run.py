#!/usr/bin/env python3
"""mv-exporter landing loader: extract a module and dump into SQLite.

Portable (pure stdlib). Idempotent, resumable, fail-loud. Run --demo to
exercise end-to-end without touching Multiview.

Examples:
  python3 run.py --demo --module AP --db ./mv_landing.db
  python3 run.py --demo --module all --db ./mv_landing.db --batch-size 5
  MV_LANDING_DB=/Volumes/MVARCHIVE/db/mv_landing.db python3 run.py --demo --module GL
"""
from __future__ import annotations

import argparse
import os
import sys

import db as dbm
from adapters import CURSOR_KEY, MODULES, DemoAdapter, MultiviewAdapter


def run_module(conn, adapter, module: str, *, batch_size: int, resume: bool) -> tuple[int, int]:
    since = dbm.get_checkpoint(conn, module, CURSOR_KEY) if resume else None
    run_id = dbm.start_run(conn, module)
    seen = upserted = 0
    batch: list[tuple] = []
    last_cursor = since
    try:
        conn.execute("BEGIN")
        for ext_id, record, cursor in adapter.extract(module, since=since):
            batch.append((ext_id, record))
            last_cursor = cursor
            seen += 1
            if len(batch) >= batch_size:
                upserted += dbm.upsert_batch(conn, module, run_id, batch)
                dbm.set_checkpoint(conn, module, CURSOR_KEY, last_cursor, run_id)
                conn.execute("COMMIT")          # durable per batch => safe resume
                conn.execute("BEGIN")
                batch.clear()
        if batch:
            upserted += dbm.upsert_batch(conn, module, run_id, batch)
        if last_cursor is not None:
            dbm.set_checkpoint(conn, module, CURSOR_KEY, last_cursor, run_id)
        dbm.finish_run(conn, run_id, status="complete", rows_seen=seen, rows_upserted=upserted)
        conn.execute("COMMIT")
    except BaseException as exc:                 # fail loud; keep last good checkpoint
        conn.execute("ROLLBACK")
        dbm.finish_run(conn, run_id, status="failed", rows_seen=seen,
                       rows_upserted=upserted, error=f"{type(exc).__name__}: {exc}")
        raise
    return seen, upserted


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="mv-exporter landing loader")
    p.add_argument("--db", default=os.environ.get("MV_LANDING_DB"),
                   help="SQLite path (or set MV_LANDING_DB). No default: paths are never hardcoded.")
    p.add_argument("--module", default="all", help="AP|GL|AR|all")
    p.add_argument("--demo", action="store_true", help="use synthetic data instead of Multiview")
    p.add_argument("--demo-rows", type=int, default=25)
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--no-resume", action="store_true", help="ignore checkpoint; re-scan from start")
    p.add_argument("--synchronous", default="NORMAL", choices=["NORMAL", "FULL"])
    args = p.parse_args(argv)

    if not args.db:
        p.error("no DB path: pass --db or set MV_LANDING_DB")
    modules = list(MODULES) if args.module == "all" else [args.module.upper()]
    for m in modules:
        if m not in MODULES:
            p.error(f"unknown module {m!r}; expected one of {MODULES} or 'all'")

    adapter = DemoAdapter(rows=args.demo_rows) if args.demo else MultiviewAdapter()

    conn = dbm.connect(args.db, synchronous=args.synchronous)
    try:
        dbm.init_db(conn)
        for m in modules:
            seen, up = run_module(conn, adapter, m, batch_size=args.batch_size,
                                  resume=not args.no_resume)
            print(f"[{m}] seen={seen} upserted={up}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
