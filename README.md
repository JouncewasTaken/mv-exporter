# mv-exporter — landing loader

Portable (pure Python stdlib, no third-party deps) ingest scaffolding that dumps
raw Multiview extracts into a SQLite landing DB for later refinement/verification.
Runs identically on macOS (M1) and Linux.

## Files
- `schema.sql` — landing schema (raw capture + run manifest + resume checkpoints)
- `db.py` — connection, schema init, idempotent upsert, provenance, checkpoints
- `adapters.py` — `ExtractAdapter` interface, `MultiviewAdapter` (seam), `DemoAdapter`
- `run.py` — CLI orchestrator (batched, resumable, fail-loud)

## Design properties (all verified)
- **Idempotent** — re-running upserts on `(module, record_key)`; unchanged rows do not bump `revision`.
- **Resumable** — per-batch commit + checkpoint; interrupted long passes resume from the last committed cursor.
- **Mutation-aware** — when a source row's payload hash changes, `revision` increments (hook for the mutable GL-JE exception report).
- **Fail-loud** — a malformed record raises with context, the run is marked `failed`, the batch rolls back; no silent drops.
- **Faithful** — landing stores the record as-extracted (`raw_json`) + `sha256`; typing/normalization is the later refinement layer.

## Quick start (demo — synthetic, PHI-free)
```bash
python3 run.py --demo --module all --db ./mv_landing.db
```

## Intended workflow (two machines)
1. **Long passes on the Linux server** (always-on, off your Mac's day-to-day):
   ```bash
   MV_LANDING_DB=/srv/mv/mv_landing.db python3 run.py --module GL --batch-size 1000
   ```
2. Verify/refine against source (mutable mode — do NOT seal yet).
3. Copy the sealed `.db` to `/Volumes/MVARCHIVE/db/` (Mac primary) and to the Linux backup partition.
4. Serve read-only via Datasette; add `-i` once reconciled.

DB path is never hardcoded: pass `--db` or set `MV_LANDING_DB`.

## Wiring the real extractor
Implement `MultiviewAdapter.extract(module, *, since)` in `adapters.py` to yield
`(external_id, raw_record_dict, cursor_value)` from your proven document exporter.
Keep it a **generator** (stream rows) and advance `cursor_value` monotonically —
that is what makes the GL pass chunk/resume instead of timing out on a full pull.
