#!/usr/bin/env python3
import argparse, sys, json, re, hashlib, datetime, time
import requests

import mv_exporter as mx          # reuse plumbing (runs _load_dotenv at import; no network)
import init_archive as gov        # governance scaffolding + helpers

# tables in an enumeration response that are never the grid (support/scaffolding/lookup)
_NOISE_TABLES = {"FORM", "PARM", "Q", "CTRL", "REV", "CCR", "PIVOT", "ADD"}

# ---------- row hygiene ----------
# Column policy: KEEP ALL columns. "Unused RAD" slots are opaque P_N fields whose
# unused-ness is only knowable from the form's label map, not the API field names.
# Keeping every column (empty ones included) is the defensible audit posture — an
# empty column present in the archive is provably "existed but unused", never a silent
# drop. Hide empties at the view layer (Datasette), not by destroying data here.
def clean_row(row):
    """Return the row minus only internal grid plumbing keys (_unique_column_id_*)."""
    return {k: v for k, v in row.items() if not k.startswith("_")}

def row_hash(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()

# ---------- enumeration (generic, any form) ----------
def _date_criterion(lo, hi):
    return {"ColumnName": mx.DATE_COL, "Operator": mx.DATE_OP,
            "ValueFrom": lo, "ValueTo": hi, "CompareTo": "V", "UseRelative": "0"}

def _enumerate(sess, cache_id, token, form_name, lo, hi):
    body = mx._body([
        ("formName", form_name),
        ("queryEntryTable", mx._e2([_date_criterion(lo, hi)])),
        ("pivotLayout", ""), ("actionType", "load"), ("queryID", ""), ("title", ""),
        ("formDataSet", ""), ("allTableCurrentRowData", ""),
        ("cacheID", cache_id), ("__RequestVerificationToken", token),
    ])
    r = sess.post(f"{mx.BASE}/prod/Multiview/LoadDataQueryEntryTable", data=body, timeout=180)
    r.raise_for_status()
    return r.json()

def discover_grid_table(resp_json):
    """Pick the table holding the enumerated grid rows. Prefer tables carrying the
    date column, then the most rows. Fails loud if nothing plausible is found."""
    fds = resp_json.get("formDataSet", resp_json)
    cands = []
    for name, rows in (fds or {}).items():
        if name.startswith("ENUM_") or name in _NOISE_TABLES:
            continue
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            cands.append((name, len(rows), mx.DATE_COL in rows[0]))
    if not cands:
        raise RuntimeError("no grid table found in enumeration response "
                           f"(tables seen: {list((fds or {}).keys())})")
    cands.sort(key=lambda c: (c[2], c[1]), reverse=True)   # has-date first, then row count
    return cands[0][0]

def _nonnoise_tables(resp_json):
    """Every non-noise data pane, not just the primary grid. Keeps OV/O on multi-pane forms."""
    fds = resp_json.get("formDataSet", resp_json)
    out = {}
    for name, rows in (fds or {}).items():
        if name.startswith("ENUM_") or name in _NOISE_TABLES:
            continue
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            out[name] = rows
    return out

def _is_timeout(exc):
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    r = getattr(exc, "response", None)
    return r is not None and r.status_code in (500, 502, 503, 504)

# ---------- ownership version history ----------
# read-only: GetVersionHTML per OV row; never the Restore/ProcessCommand write path
import html as _htmlmod

_VER_SECTIONS  = {"Items Moved from one Area to Another": "Moved"}   # extend if new classes appear
_VER_STANDALONE = {"Initial Version Created"}
_RE_VER  = re.compile(r'^Version\s+(\d+)\s+Changes$', re.I)
_RE_BY   = re.compile(r'^Changes made by\s+(.+)$', re.I)
_RE_ON   = re.compile(r'^Made on\s+(.+)$', re.I)
_RE_MOVE = re.compile(r'^(.+?)\s+was moved from\s+(.+?)\s+to\s+(.+)$', re.I)

def _version_html(sess, cache_id, token, form_name, ov_row):
    """GetVersionHTML for one OV row -> HTML body."""
    body = mx._body([
        ("command", "GetVersionHTML"),
        ("formName", form_name),
        ("tableName", "OV"),
        ("key", mx._e2([ov_row])),          # OV row selects which version
        ("allTableCurrentRowData", ""), ("formDataSet", ""),
        ("cacheID", cache_id), ("__RequestVerificationToken", token),
    ])
    r = sess.post(f"{mx.BASE}/prod/Multiview/RunCommand", data=body, timeout=180)
    r.raise_for_status()
    return r.text

def _parse_version_html(html_text, ownership_id, version_no):
    """GetVersionHTML body -> change rows. Fails loud on unknown sections (extend _VER_SECTIONS)."""
    h = re.sub(r'(?is)<style.*?</style>', ' ', html_text)
    h = re.sub(r'(?is)<head.*?</head>', ' ', h)
    h = re.sub(r'(?s)<[^>]+>', '\n', h)
    lines = [ln.strip() for ln in _htmlmod.unescape(h).splitlines() if ln.strip()]
    changed_by = changed_on = None
    section = None
    rows = []
    def row(**kw):
        base = dict(OWNERSHIP_ID=ownership_id, VERSION_NO=version_no,
                    CHANGED_BY=changed_by, CHANGED_ON=changed_on,
                    CHANGE_TYPE=None, KEY_MOVED=None, MOVED_FROM=None, MOVED_TO=None)
        base.update(kw); return base
    for ln in lines:
        m = _RE_VER.match(ln)
        if m:
            if int(m.group(1)) != int(version_no):
                raise ValueError(f"version mismatch: html v{m.group(1)} != expected v{version_no}")
            section = None; continue
        m = _RE_BY.match(ln)
        if m: changed_by = m.group(1).strip(); continue
        m = _RE_ON.match(ln)
        if m: changed_on = m.group(1).strip(); continue
        if ln in _VER_SECTIONS:
            section = _VER_SECTIONS[ln]; continue
        if ln in _VER_STANDALONE:
            rows.append(row(CHANGE_TYPE=ln)); section = None; continue
        if section == "Moved":
            m = _RE_MOVE.match(ln)
            if m:
                rows.append(row(CHANGE_TYPE="Moved", KEY_MOVED=m.group(1).strip(),
                                MOVED_FROM=m.group(2).strip(), MOVED_TO=m.group(3).strip()))
                continue
        raise ValueError(f"unrecognized GetVersionHTML line (extend _VER_SECTIONS): {ln!r}")
    return rows

def fetch_version_changes(sess, cache_id, token, form_name, ov_rows, verbose=False):
    """Read every version's change log for a form's OV rows. Read-only (GetVersionHTML)."""
    out = []
    for ov in ov_rows:
        ver = ov.get("VERSION_NO"); own = ov.get("OWNERSHIP_ID")
        changes = _parse_version_html(_version_html(sess, cache_id, token, form_name, ov), own, ver)
        out.extend(changes)
        if verbose:
            print(f"      v{ver}: {len(changes)} change row(s)")
        time.sleep(mx.THROTTLE_S)
    return out

def fetch_form_tables(sess, cache_id, token, form_name, lo, hi, verbose=False):
    """All non-noise panes over [lo,hi], bisecting on timeout. Repeated static panes dedup at land."""
    try:
        resp = _enumerate(sess, cache_id, token, form_name, lo, hi)
        tables = _nonnoise_tables(resp)
        if verbose:
            summary = ", ".join(f"{n}={len(r)}" for n, r in tables.items())
            print(f"    [{lo}..{hi}] {summary or '(no data panes)'}")
        return tables
    except Exception as e:
        d0 = datetime.date.fromisoformat(lo); d1 = datetime.date.fromisoformat(hi)
        if _is_timeout(e) and (d1 - d0).days >= 1:
            mid = d0 + (d1 - d0) // 2
            if verbose:
                print(f"    [{lo}..{hi}] timed out — splitting at {mid}")
            merged = fetch_form_tables(sess, cache_id, token, form_name, lo, mid.isoformat(), verbose)
            right = fetch_form_tables(sess, cache_id, token, form_name,
                                      (mid + datetime.timedelta(days=1)).isoformat(), hi, verbose)
            for n, r in right.items():
                merged.setdefault(n, []).extend(r)
            return merged
        raise

# ---------- SQLite sink ----------
def _ensure_table(con, table, columns):
    exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        # typeless columns keep native affinity (numbers stay numeric, text stays text)
        cols = ", ".join(f'"{c}"' for c in columns)
        con.execute(f'CREATE TABLE "{table}" ("_row_hash" TEXT PRIMARY KEY, {cols}, "_raw_json" TEXT)')
    else:
        have = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
        for c in columns:
            if c not in have:
                con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}"')

def land_rows(con, table, rows, source_form):
    """Insert rows (content-hashed, all columns kept) idempotently. Returns (inserted, columns)."""
    all_cols, cleaned = [], []
    seen = set()
    for r in rows:
        kept = clean_row(r)
        cleaned.append(kept)
        for c in kept:
            if c not in seen:
                seen.add(c); all_cols.append(c)
    if not cleaned:
        return 0, []
    _ensure_table(con, table, all_cols)
    gov.register_columns(con, table, all_cols, source_form)
    inserted = 0
    for kept in cleaned:
        h = row_hash(kept)
        cols = list(kept.keys())
        placeholders = ", ".join(["?"] * (len(cols) + 2))
        vals = [h] + [kept[c] if not isinstance(kept[c], (dict, list)) else json.dumps(kept[c]) for c in cols]
        vals.append(json.dumps(kept, default=str))
        colnames = '"_row_hash", ' + ", ".join(f'"{c}"' for c in cols) + ', "_raw_json"'
        cur = con.execute(f'INSERT OR IGNORE INTO "{table}" ({colnames}) VALUES ({placeholders})', vals)
        inserted += cur.rowcount
    return inserted, all_cols

# ---------- driver ----------
def _open(driver, form_code):
    """Configure for a form, open it (mints a fresh cacheID/token), return (cache_id, token)."""
    mx._configure(form_code, require_module=False)
    return mx.open_form(driver)

def _done(con, code, window):
    """True if this (form, window) already landed ok — for resume."""
    return con.execute("SELECT 1 FROM _extraction_manifest WHERE form_name=? AND note=? "
                       "AND status='ok' LIMIT 1", (code, window)).fetchone() is not None

def _pull_retry(driver, sess, code, lo, hi, attempts):
    """Open + enumerate with retry on transient failures. Empty result returns {} (not an error)."""
    for i in range(1, attempts + 1):
        try:
            cache_id, token = _open(driver, code)
            return cache_id, token, fetch_form_tables(sess, cache_id, token, code, lo, hi, verbose=True)
        except (Exception, SystemExit) as e:
            if i == attempts:
                raise
            print(f"    retry {i}/{attempts} {code}: {e!r}", file=sys.stderr)
            time.sleep(min(30, 5 * i))

def main():
    ap = argparse.ArgumentParser(description="extract Multiview grid/table data to SQLite")
    ap.add_argument("--db", required=True, help="archive .sqlite path")
    ap.add_argument("--forms", help="text file of form codes to pull (one per line, # comments)")
    ap.add_argument("--probe", metavar="FORM", help="enumerate one form, report rows/columns/grid-table, then exit")
    ap.add_argument("--year", type=int, help="pull one ACCOUNTING_DATE year")
    ap.add_argument("--from", dest="d_from", help="range start YYYY-MM-DD (with --to)")
    ap.add_argument("--to", dest="d_to", help="range end YYYY-MM-DD (with --from)")
    ap.add_argument("--ownership-versions", action="store_true",
                    help="also capture per-version change history (read-only GetVersionHTML) "
                         "for any form that returns an OV version pane")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--no-resume", action="store_true", help="re-pull windows already logged ok")
    ap.add_argument("--retries", type=int, default=3, help="attempts per form-window on transient failure")
    args = ap.parse_args()

    if not mx.MV_SUBDOMAIN or not mx.BASE:
        sys.exit("set MV_SUBDOMAIN in your .env")
    if args.year:
        lo, hi = f"{args.year}-01-01", f"{args.year}-12-31"
    elif args.d_from and args.d_to:
        lo, hi = args.d_from, args.d_to
    else:
        sys.exit("specify --year YYYY or --from/--to")

    con = gov.connect(args.db)                          # ensure governance scaffolding exists
    driver = mx.build_driver(headless=not args.no_headless)
    try:
        mx.login(driver)
        sess = mx.session_from_driver(driver)

        if args.probe:
            cache_id, token = _open(driver, args.probe)
            resp = _enumerate(sess, cache_id, token, args.probe, lo, hi)
            table = discover_grid_table(resp)
            rows = mx._extract_table(resp, table)
            cols = [c for c in (rows[0].keys() if rows else []) if not c.startswith("_")]
            print(f"[probe] {args.probe}  grid table='{table}'  rows={len(rows)}  columns={len(cols)}")
            print(f"[probe] columns: {cols}")
            print("[probe] compare column count to your manual grid export; if short, the saved "
                  "grid layout isn't returning all columns via the API.")
            return

        forms = []
        for line in open(args.forms):
            line = line.split("#", 1)[0].strip()
            if line:
                forms.append(line)
        if not forms:
            sys.exit("no form codes in --forms file")

        window = f"{lo}..{hi}"      # manifest note; year col is null under --from/--to
        for code in forms:
            table = code.lower()
            if not args.no_resume and _done(con, code, window):
                print(f"[skip] {code}: {window}")
                continue
            try:
                cache_id, token, tables = _pull_retry(driver, sess, code, lo, hi, args.retries)
                if not tables:
                    # valid response, no rows this window — empty, not a failure
                    gov.record_extraction(con, mx.MODULES.get(code, {}).get("label", "?"),
                                          code, table, args.year, 0, 0,
                                          mx.MV_SUBDOMAIN, status="ok", note=window)
                    con.commit()
                    print(f"[ok] {code}: {window} empty")
                    time.sleep(mx.THROTTLE_S)
                    continue
                primary = discover_grid_table(tables)      # primary grid -> {code}
                total_rows, landed = 0, []
                for tname, rws in tables.items():
                    sqlt = table if tname == primary else f"{table}__{tname.lower()}"
                    inserted, cols = land_rows(con, sqlt, rws, code)
                    con.commit()
                    total_rows += len(rws)
                    landed.append(f"{sqlt}(+{inserted}/{len(rws)})")
                if args.ownership_versions and "OV" in tables:
                    changes = fetch_version_changes(sess, cache_id, token, code,
                                                    tables["OV"], verbose=True)
                    if changes:
                        vins, _ = land_rows(con, f"{table}__version_changes", changes, code)
                        con.commit()
                        landed.append(f"{table}__version_changes(+{vins}/{len(changes)})")
                gov.record_extraction(con, mx.MODULES.get(code, {}).get("label", "?"),
                                      code, table, args.year, total_rows, len(tables),
                                      mx.MV_SUBDOMAIN, status="ok", note=window)
                con.commit()
                print(f"[ok] {code}: {len(tables)} pane(s) -> {', '.join(landed)}")
            except (Exception, SystemExit) as e:
                gov.record_extraction(con, "?", code, table, args.year, 0, 0,
                                      mx.MV_SUBDOMAIN, status="error",
                                      note=f"{window} | {repr(e)[:250]}")
                con.commit()
                print(f"[ERROR] {code}: {e!r}", file=sys.stderr)
            time.sleep(mx.THROTTLE_S)
    finally:
        driver.quit(); con.close()

if __name__ == "__main__":
    main()

