# mv-exporter

A configuration-driven tool for exporting **your own organization's data** out of
[Multiview](https://www.multiviewcorp.com/) ERP ahead of a migration; both the
**attached documents** (PDFs, spreadsheets, images, notes) and the **ledger/detail
tables** into a queryable **SQLite** archive.

It works by driving Multiview's own web UI to establish an authenticated session,
then replaying the application's internal data and document endpoints to pull records
in bulk. Everything is keyed off config: point it at your tenant, list the forms you
want, and run.

> **This is a data-portability tool for organizations leaving Multiview.** Use it only
> against a Multiview instance you are authorized to access, on data you have the right
> to export. See [Legal & scope](#legal--scope).

---

## What it does

- **Authenticates** through the standard Multiview login (including AWS Cognito–backed
  logins), with support for interactive MFA.
- **Auto-discovers** each form's structure at runtime (grid table, primary key, document
  class) from the form definition, so you can target any Multiview form by name — not just
  a hard-coded set. Forms whose structure can't be resolved are skipped loudly rather than
  exported incorrectly.
- **Extracts documents** attached to records — external files by original type, and internal
  notes (Syncfusion SFDT) decoded to plain text — named by their record's primary key, with
  a `.meta.json` sidecar carrying the parent record's fields.
- **Extracts tables** (headers, detail/line tables, distribution lines, dimensions) into a
  SQLite archive, preserving the foreign keys Multiview already carries so records link for
  drill-through.
- **Bands large pulls by year** (on `ACCOUNTING_DATE`) with a self-verifying guard that fails
  loudly if the server ignores the filter, so a year's export is provably that year.
- Is **idempotent and resumable** via a per-run manifest — stop and restart without
  re-pulling or duplicating.

## How it works

Multiview's web client keeps a stateful, per-session form context (a `cacheID`). The tool
uses a real browser (Selenium) only to log in and open a form — minting that `cacheID` and
the request-verification token — then hands the authenticated session to a plain HTTP client
(`requests`) that does all the bulk work. Documents are fetched via the app's own
preload/viewer endpoints; tables via the query/enumeration endpoint. Nothing is loaded into
the target system; the output is standalone files plus a SQLite database.

## Requirements

- Python 3.9+
- Google Chrome + a matching `chromedriver` on your `PATH`
- `pip install -r requirements.txt` (Selenium, requests; `cryptography` only if you use the
  optional encryption module)

## Setup

Copy `.env.example` to `.env` and fill it in:

```
MV_SUBDOMAIN=yourcompany            # the tenant part of https://<subdomain>.multiviewcorp.net
MV_USER=your.username
MV_PASS=your.password
MV_EXPORT_DIR=/path/to/output       # must exist; store on encrypted media if data is sensitive
```

`.env` is gitignored and must never be committed. Credentials live only on the machine
running the export.

## Selecting what to pull

List the forms to export in a plain text file (one form code per line; blank lines and
`#` comments ignored). This mirrors the form catalog Multiview exposes:

```
# core ledgers
VOUCHER_F1
ENTRY_F1
AR_INQUIRY_F1
# detail / distribution
VOUCHER_DIST_INQUIRY_F1
ENTRY_DETAIL_F1
```

The tool resolves each form's structure at runtime. Anything it can't profile is reported
and skipped, never guessed.

## Usage

```bash
# validate one record end-to-end before a full run
python mv_exporter.py --forms forms.txt --one <COMPANY>/<RECORD_ID> --no-headless

# dry-run: count records for a year without downloading
python mv_exporter.py --forms forms.txt --year 2025 --count-only

# full export of a year
python mv_exporter.py --forms forms.txt --year 2025
```

Run the first login of a session with `--no-headless` so you can complete any MFA prompt.

## Output

- Documents as files named `{company}_{record}_{docref}.{ext}`, each with a `{name}.meta.json`.
- A SQLite archive with one table per form (columns as returned by the source), plus governance
  tables recording extraction provenance and the columns captured, so completeness is auditable.
- A JSONL manifest for resumability.

## Optional: encryption & audit

For organizations handling regulated or sensitive data, an optional module (`archive_auth.py`)
provides at-rest encryption of the SQLite archive (AES-256-GCM), per-user access via
password-wrapped keys, and a tamper-evident (hash-chained) access log. It is **not** wired in
by default — the core exporter produces a plain archive. See [`docs/encryption.md`](docs/encryption.md).
This module is a starting point, not a compliance guarantee; if your data is regulated, get a
qualified sign-off on your controls.

## Project status

- Document export (external files + notes): working against live Multiview, incl. Cognito login.
- Table extraction into SQLite: in progress.
- Runtime form auto-discovery (Tier 2): in progress.
- Optional encryption/audit module: available, standalone.

See [issues](../../issues) for the roadmap.

## Legal & scope

This tool automates access to data through your organization's existing, authorized Multiview
login. It does not bypass authentication or access controls. **You are responsible** for
ensuring your use complies with your Multiview license/agreement and any laws or regulations
governing the data you export (including privacy and record-retention rules). Provided **as is,
without warranty of any kind** (see [LICENSE](LICENSE)). Not affiliated with or endorsed by
Multiview Corporation; "Multiview" is used only to describe the software this tool interoperates
with.

## License

[MIT](LICENSE)
