# mv-exporter — landing loader

Portable (pure Python stdlib, no third-party deps) ingest scaffolding that dumps
raw Multiview extracts into a SQLite landing DB for later refinement/verification.
Runs identically on macOS (M1) and Linux.

## Files
- `schema.sql` — landing schema (raw capture + run manifest + resume checkpoints)
- `db.py` — connection, schema init, idempotent upsert, provenance, checkpoints
- `adapters.py` — `ExtractAdapter` interface, `MultiviewAdapter` (seam), `DemoAdapter`
- `run.py` — CLI orchestrator (batched, resumable, fail-loud)
- `mv_exporter.py` — Selenium login + form-open/session plumbing; document exporter (3 core modules)
- `mv_tables.py` — table/grid extraction into the SQLite archive (any form)
- `init_archive.py` — governance scaffolding (metadata, extraction manifest, schema registry)
- `archive_auth.py` — optional archive auth/encryption

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

## `mv_tables.py` — table/grid extraction

Sibling to `mv_exporter.py`: reuses its login / form-open / session plumbing, so the
document exporter stays untouched. Extraction is separate from the SQLite sink.

- Each form is one bulk query, enumerated via `LoadDataQueryEntryTable`, filtered by
  `ACCOUNTING_DATE` between a range. The grid table is auto-discovered; ambiguity fails loud.
- All non-noise panes are captured, not just the primary grid — multi-pane forms
  (ownerships: `OD` tree + `OV` versions + `O` header) land as `{form}` and `{form}__{pane}`.
- Ranges that time out split in half recursively, down to a one-day floor.
- Rows land insert-or-ignore on a per-row content hash: retries/overlaps/re-runs never
  duplicate; no per-form key needed. Dropped RAD columns are recorded in the schema registry.

**Before a full run:** the API returns whatever columns the querying account's *saved grid
layout* specifies. Set each form's grid to show ALL columns and save it as the default, then
`--probe` to confirm the column count before trusting a pull.

**`--ownership-versions`** also pulls each version's change log (read-only `GetVersionHTML`;
never the "Restore Old Version" write path) into `{form}__version_changes`. Point-in-time tree
shapes aren't snapshotted — reconstruct by replaying moves from the current `OD`.

```bash
python mv_tables.py --db archive.db --probe VOUCHER_DIST_INQUIRY_F1 --year 2025
python mv_tables.py --db archive.db --forms forms.txt --year 2025
python mv_tables.py --db archive.db --forms forms.txt --from 2016-01-01 --to 2025-12-31 --ownership-versions
```
