# mv-exporter

Extract your data out of Multiview ERP into a portable, self-verifying SQLite archive —
before a migration, a vendor change, or a contract lapse. Drives the web UI the way you
would, so it works without database access or a vendor export feature.

Assumes some coding comfort (Python, a shell, SQL). Not a one-click tool.

## What it does
- **Tables/grids** (`mv_tables.py`): any form's grid → a SQLite table, banded by
  `ACCOUNTING_DATE`, resumable and idempotent.
- **Documents** (`mv_exporter.py`): the attached files behind AP/GL/AR transactions →
  disk + a manifest.
- **Governance** (`init_archive.py`): archive metadata, a per-window extraction manifest,
  and a schema registry recording every column seen (including dropped ones), so
  completeness stays provable.

## Requirements
- Python 3.10+, `pip install selenium requests`
- Chrome/Chromium. Selenium 4.6+ resolves a matching driver automatically; otherwise put a
  matching `chromedriver` on `PATH`.
- Your Multiview login. If SSO/MFA is enabled, the first run is interactive.

## Setup
Copy `env.example` to `.env` and fill it in (`.env` is gitignored; never commit it):

```
MV_SUBDOMAIN=yourcompany          # https://<subdomain>.multiviewcorp.net
MV_USER=...
MV_PASS=...
MV_EXPORT_DIR=/path/to/documents  # keep outside the repo
MV_LANDING_DB=/path/to/archive.db # keep outside the repo
MV_THROTTLE_S=1.0                 # politeness delay between requests
MV_CHROME_PROFILE=/path/to/profile  # persists the session so headless runs skip re-login
```

Initialize the archive, then seat an authenticated session once (interactive, for MFA):

```bash
python init_archive.py "$MV_LANDING_DB"
python mv_tables.py --db "$MV_LANDING_DB" --forms forms.txt --from 2025-01-01 --to 2025-01-31 --no-headless
```

After that, headless runs reuse the profile until the session expires.

## Pick your forms
Multiview exposes hundreds of forms; most are config you don't need. Enumerate the catalog
(see `forms.example.txt`), triage to the transactional/master/audit forms that matter, and
list them one per line in a `forms.txt`.

**Before a full run:** the API returns whatever columns the querying account's *saved grid
layout* specifies. For each form, open its grid in the web UI, show ALL columns, save it as
the default, then `--probe` to confirm the column count matches a manual grid export.

```bash
python mv_tables.py --db "$MV_LANDING_DB" --probe VOUCHER_DIST_INQUIRY_F1 --year 2025
```

## Extract tables
```bash
# one year, or an explicit range (band long histories by month to bound memory + checkpoint)
python mv_tables.py --db "$MV_LANDING_DB" --forms forms.txt --year 2025
python mv_tables.py --db "$MV_LANDING_DB" --forms forms.txt --from 2016-01-01 --to 2025-12-31
```

- **Resumable/self-healing:** re-run the same command — completed `(form, window)` pairs skip,
  errors and gaps retry. Runs to convergence without hand-maintained re-pull lists.
- **Idempotent:** rows land insert-or-ignore on a per-row content hash. Retries, overlaps, and
  re-runs never duplicate; no per-form primary key needed.
- **All panes captured:** multi-pane forms (e.g. ownership: tree + version history + header)
  land each pane as `{form}` and `{form}__{pane}`.
- Some forms ignore the date band and return their whole table each call — pull those once
  over a wide range rather than month-by-month. The manifest makes them easy to spot
  (summed rows ≫ landed rows).

`--ownership-versions` additionally captures each version's change log for forms with a
version pane (read-only; never triggers a restore).

## Extract documents
The file payloads come from `mv_exporter.py`, scoped to the three document-bearing modules:

```bash
python mv_exporter.py --form-url VOUCHER_F1    --year 2025   # AP
python mv_exporter.py --form-url ENTRY_F1      --year 2025   # GL
python mv_exporter.py --form-url AR_INQUIRY_F1 --year 2025   # AR
```

- Files land as `{FORM_NAME}_{company}_{record}_{doc_ref}.ext` in `MV_EXPORT_DIR`, each with a
  `.meta.json` sidecar linking it to its parent record — so a filename identifies its module
  and record for clickthrough.
- Resumes via a per-module manifest keyed on `(form, company, record)`: re-running a year
  skips records already pulled.
- Downloads are **per record**, with no content-level dedup — the same document attached to
  several records (or surfacing under multiple modules) is stored once per record. That's
  intentional: it preserves each record's own copy + metadata and supports working backward
  from any module (e.g. GL → source invoice).
- `--count-only` reports the record count and date span without downloading; `--one CID/VID`
  and `--dump-docs CID/VID` inspect a single record.

## Design
Fail-loud (ambiguity or an unknown response shape raises, never silently drops), faithful
(every column kept; dropped ones still registered), and idempotent throughout. The archive
is a queryable copy — verify it against source before you retire the live system.

Built by reverse-engineering the web UI. Login, enumeration, subform loads, payload
encoding, naming, and manifests are confirmed against a live tenant; the JSON *response*
shapes of the data calls are inferred and parsed defensively (unexpected shapes raise). The
document byte URL is discovered at runtime from the browser's network log, not hardcoded.

## Not archiving?
If you're mapping into another ERP rather than keeping an archive, call
`mv_tables.fetch_form_tables()` for the rows and ignore the SQLite sink. (`run.py`,
`adapters.py`, `db.py`, `schema.sql` are an earlier stdlib landing-loader kept as a
reference for that path.)
