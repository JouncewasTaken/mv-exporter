#!/usr/bin/env python3
import os, sys, json, time, base64, argparse, re, zipfile, io
from pathlib import Path
from urllib.parse import quote

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def _load_dotenv(path=".env"):
    """Minimal .env loader (no dependency). KEY=VALUE lines; does not overwrite existing env."""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass

_load_dotenv()

# Tenant is supplied via .env — never hardcode it. MV_SUBDOMAIN is the part of
# https://<subdomain>.multiviewcorp.net that identifies your organization.
MV_SUBDOMAIN = os.environ.get("MV_SUBDOMAIN", "").strip()
BASE       = f"https://{MV_SUBDOMAIN}.multiviewcorp.net" if MV_SUBDOMAIN else ""
OUTPUT_DIR = Path(os.environ.get("MV_EXPORT_DIR", "")).expanduser()
MANIFEST   = None
THROTTLE_S = float(os.environ.get("MV_THROTTLE_S", "1.0"))

# ======================= MODULE REGISTRY (FORM_URL is the switch) =======================
# Set FORM_URL (or pass --form-url) to pick a module; grid table, PK id column, and the
# SF1P document class all derive from the registry below. The document subsystem
# (SF1D load -> PreloadDocViewer -> LoadDocViewer -> classify) is identical across modules.
DATE_COL    = "ACCOUNTING_DATE"     # common banding date across AP/GL/AR (GL only exposes this one)
DATE_OP     = "BETWEEN"             # Multiview range operator (inclusive both ends)
COMPANY_COL = "COMPANY_ID"          # first PK column, common to all three modules

_EQ = lambda c: {"ColumnName": c, "Operator": "EQ", "ValueFrom": "", "ValueTo": "", "CompareTo": "V", "UseRelative": "0"}
_IN = lambda c: {"ColumnName": c, "Operator": "IN", "ValueFrom": "", "ValueTo": "", "CompareTo": "V", "UseRelative": "0"}
# AP's blank-query column set (verified). GL/AR enumerate fine with an empty base query.
_AP_QUERY = [_EQ(c) for c in (
    "COMPANY_ID","ENTRY_ID","CVS_ID","ADDRESS_ID","CURRENCY_ID","BANK_ACCOUNT","BANK_ID",
    "CHEQUE_CURRENCY_ID","VOUCHER_ID","INVOICE_NO","PO_ORDER_NO","INVOICE_AMOUNT","APPROVAL_ID")] \
  + [_IN("VOUCHER_STATUS"), _IN("POSTING_STATUS"), _IN("TRANSACTION_TYPE")] \
  + [_EQ(c) for c in ("CONTROL_ACCT_ID","RECURRING_ID","VENDOR_TYPE","USER_CREATED","PROJECT_ID")]

# DOC_CLASS is read from each doc's own SF1D row at preload time; these are the per-module
# fallbacks used at load time (before individual docs are known). AR's class tracks TRANS_CLASS.
def _ap_class(row): return "APVOUCHER"
def _gl_class(row): return "GLJE"
def _ar_class(row): return "AR" + (row.get("TRANS_CLASS") or "")

MODULES = {
    "VOUCHER_F1":    {"label": "AP", "grid": "VL", "id_col": "VOUCHER_ID", "doc_class": _ap_class, "query": _AP_QUERY},
    "ENTRY_F1":      {"label": "GL", "grid": "E",  "id_col": "ENTRY_ID",   "doc_class": _gl_class, "query": []},
    "AR_INQUIRY_F1": {"label": "AR", "grid": "TL", "id_col": "TRANS_ID",   "doc_class": _ar_class, "query": []},
}

FORM_URL = "VOUCHER_F1"     # default module; override with --form-url (bare code or full URL). Tenant comes from BASE.

FORM_NAME = GRID = ID_COL = MOD = None          # set by _configure()
def _configure(form_url, require_module=True):
    global FORM_URL, FORM_NAME, GRID, ID_COL, MOD
    raw = (form_url or "").strip()
    code = raw.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
    FORM_NAME = code
    FORM_URL = raw if raw.startswith(("http://", "https://")) else f"{BASE}/prod/Multiview/FormName/{code}"
    if code in MODULES:
        MOD, GRID, ID_COL = MODULES[code], MODULES[code]["grid"], MODULES[code]["id_col"]
    elif require_module:
        sys.exit(f"unknown form code '{code}' (from {raw!r}) — known: {', '.join(MODULES)}")
    else:
        MOD = GRID = ID_COL = None      # table path discovers the grid dynamically
_configure(FORM_URL)

def _year_criterion(year):
    return {"ColumnName": DATE_COL, "Operator": DATE_OP,
            "ValueFrom": f"{year}-01-01", "ValueTo": f"{year}-12-31",
            "CompareTo": "V", "UseRelative": "0"}
# ===================== END MODULE REGISTRY =====================
# ===================== END PER-MODULE CONFIG =====================

# ---------- encoding helpers (verified: payloads are encodeURIComponent THEN form-encoded) ----------
def _e1(s):  return quote(str(s), safe="")            # single (form layer)
def _e2(obj): return quote(quote(json.dumps(obj, separators=(",", ":")), safe=""), safe="")  # double
def _body(pairs): return "&".join(f"{k}={v}" for k, v in pairs)
_HDRS = {"content-type": "application/x-www-form-urlencoded; charset=UTF-8",
         "x-requested-with": "XMLHttpRequest", "accept": "*/*"}

# ---------- auth + form-state (login VERIFIED; extraction fail-loud) ----------
def build_driver(headless=True):
    o = Options()
    prof = os.environ.get("MV_CHROME_PROFILE")
    if prof:
        os.makedirs(prof, mode=0o700, exist_ok=True)
        o.add_argument(f"--user-data-dir={prof}")
    if headless: o.add_argument("--headless=new")
    o.add_argument("--no-sandbox"); o.add_argument("--disable-dev-shm-usage")
    o.set_capability("goog:loggingPrefs", {"performance": "ALL"})   # to harvest cacheID from form traffic
    return webdriver.Chrome(options=o)

def _first_element(driver, wait, locators):
    """Return the first element matching any of the given (By, value) locators."""
    last = None
    for by, val in locators:
        try:
            return WebDriverWait(driver, wait).until(EC.presence_of_element_located((by, val)))
        except Exception as e:
            last = e
    raise RuntimeError(f"none of these login locators matched: {locators}") from last

def login(driver):
    from selenium.webdriver.common.keys import Keys
    user = os.environ.get("MV_USER") or input("MV username: ")
    pw   = os.environ.get("MV_PASS") or __import__("getpass").getpass("MV password: ")
    driver.get(f"{BASE}/prod/Multiview/Login")
    # Field identifiers have moved between name/id across UI updates — try several.
    u = _first_element(driver, 30, [
        (By.ID, "username"), (By.NAME, "username"),
        (By.CSS_SELECTOR, "input[autocomplete='username']"),
        (By.CSS_SELECTOR, "input[type='text']")])
    u.clear(); u.send_keys(user)
    p = _first_element(driver, 10, [
        (By.ID, "password"), (By.NAME, "password"),
        (By.CSS_SELECTOR, "input[type='password']"),
        (By.CSS_SELECTOR, "input[autocomplete='current-password']")])
    p.clear(); p.send_keys(pw)
    # Prefer a real submit control; fall back to pressing Enter in the password field.
    for by, val in [(By.ID, "loginButton"), (By.CSS_SELECTOR, "button[type='submit']"),
                    (By.CSS_SELECTOR, "input[type='submit']"), (By.CSS_SELECTOR, "button.field-submit")]:
        try:
            driver.find_element(by, val).click(); break
        except Exception:
            continue
    else:
        p.send_keys(Keys.RETURN)
    # Login is now AWS Cognito (client-side). If the user pool enforces MFA, a code prompt
    # appears after Sign In — complete it in the window (requires --no-headless). Wait long
    # enough for that; on success the Cognito JS redirects away from /Login.
    print("Signing in via Cognito… if an MFA/verification prompt appears, complete it in the browser window.")
    try:
        WebDriverWait(driver, 300).until(lambda d: "/Login" not in d.current_url)
    except Exception:
        raise RuntimeError("still on the login page after 300s — wrong credentials, an unmet MFA "
                           "challenge (run with --no-headless to complete it), or an SSO redirect.")

def open_form(driver):
    """Open the module form; return (cache_id, token). Fails loud if either is missing."""
    if not FORM_URL:
        sys.exit("set FORM_URL (or pass --form-url) to the module form's address-bar URL")
    driver.get_log("performance")                      # clear
    driver.get(FORM_URL)
    time.sleep(3)                                       # let the form issue its init calls
    # token: same hidden-input pattern as the login page
    token = driver.execute_script(
        "var e=document.querySelector('input[name=__RequestVerificationToken]');return e?e.value:null;")
    # cacheID: harvest from the form's own network traffic (appears in every call it makes)
    cache_id = None
    for entry in driver.get_log("performance"):
        m = json.loads(entry["message"])["message"]
        if m.get("method") == "Network.requestWillBeSent":
            pd = m["params"].get("request", {}).get("postData", "") or ""
            if "cacheID=" in pd:
                cache_id = pd.split("cacheID=")[1].split("&")[0]
                break
    if not token:    sys.exit("could not read __RequestVerificationToken from form page")
    if not cache_id: sys.exit("could not harvest cacheID from form traffic (try --discover)")
    return cache_id, token

def session_from_driver(driver):
    s = requests.Session()
    s.headers.update(_HDRS)
    s.headers["user-agent"] = driver.execute_script("return navigator.userAgent")
    s.headers["origin"] = BASE
    for c in driver.get_cookies():
        s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    return s

# ---------- response shape tolerance (INFERRED; fail-loud) ----------
def _extract_table(resp_json, table):
    """Return list-of-rows for `table` across the response shapes MV might use."""
    if isinstance(resp_json, dict):
        fds = resp_json.get("formDataSet")
        if isinstance(fds, dict) and table in fds:
            return fds[table]
        if resp_json.get("tableName") == table and isinstance(resp_json.get("data"), list):
            return resp_json["data"]
        for key in ("data", "rows", table):
            v = resp_json.get(key)
            if isinstance(v, list):
                return v
    raise ValueError(f"could not locate '{table}' rows in response; shape was {type(resp_json)} keys="
                     f"{list(resp_json)[:8] if isinstance(resp_json, dict) else 'n/a'}")

def _uid_key(row):
    for k in row:
        if k.startswith("_unique_column_id_"):
            return k
    return None

# ---------- enumeration (CONFIRMED request) ----------
def _row_year(row):
    v = row.get(DATE_COL)
    return int(v[:4]) if isinstance(v, str) and v[:4].isdigit() else None

def _verify_year(rows, year):
    """Fail loud if the server ignored the year filter (returned rows outside the requested year)."""
    seen, off = set(), []
    for r in rows:
        ry = _row_year(r)
        if ry is None:
            continue
        seen.add(ry)
        if ry != year:
            off.append((r.get(COMPANY_COL), r.get(ID_COL), ry))
    if not seen:
        print(f"[WARN] cannot verify year filter: no {DATE_COL} on returned rows", file=sys.stderr)
    elif off:
        raise RuntimeError(
            f"year filter NOT honored: asked {year}, got years {sorted(seen)} (e.g. {off[:3]}). "
            f"The {DATE_OP} operator or date format is likely wrong for this server — capture a dated "
            f"UI query (LoadDataQueryEntryTable with a date filter) so the exact wire format can be set.")

def enumerate_vouchers(sess, cache_id, token, year=None):
    criteria = MOD["query"] + ([_year_criterion(year)] if year else [])
    body = _body([
        ("formName", FORM_NAME),
        ("queryEntryTable", _e2(criteria)),
        ("pivotLayout", ""), ("actionType", "load"), ("queryID", ""), ("title", ""),
        ("formDataSet", ""), ("allTableCurrentRowData", ""),
        ("cacheID", cache_id), ("__RequestVerificationToken", token),
    ])
    r = sess.post(f"{BASE}/prod/Multiview/LoadDataQueryEntryTable", data=body, timeout=180)
    r.raise_for_status()
    rows = _extract_table(r.json(), GRID)
    if not rows:
        print(f"[WARN] 0 {MOD['label']} records"
              + (f" for year {year}" if year else "") + " — empty period or query mismatch",
              file=sys.stderr)
        return []
    if year:
        _verify_year(rows, year)
    return rows   # full grid rows; echoed back verbatim for the SF1D load

# ---------- per-voucher document metadata (CONFIRMED request; payload echoes the VL row) ----------
def load_docs(sess, cache_id, token, entry_row):
    cid, vid = str(entry_row[COMPANY_COL]), entry_row[ID_COL]
    uidk = _uid_key(entry_row) or "_unique_column_id_Wreg174kl"
    all_tables = [
        {"tableName": GRID,   "currentRowData": [entry_row]},
        {"tableName": "SF1D", "currentRowData": []},
        {"tableName": "FORM", "currentRowData": [{uidk: "0", "FORM_NAME": FORM_NAME,
            "FORM_STARTUP": "ENTER_QUERY"}]},
        {"tableName": "SF1P", "currentRowData": [{uidk: "0", "DATA_COMPANY_ID": cid,
            "DATA_ID": vid, "DOC_CLASS": MOD["doc_class"](entry_row)}]},
    ]
    body = _body([
        ("formName", FORM_NAME), ("tableName", "SF1D"),
        ("queryEntryTable", _e2(MOD["query"])),
        ("allTableCurrentRowData", _e2(all_tables)),
        ("actionType", "load"), ("cacheID", cache_id),
        ("flexColumnList", ""), ("refreshTableSingleCommand", "yes"),
        ("actionInfo", _e1(json.dumps({"requery": True}, separators=(",", ":")))),
        ("__RequestVerificationToken", token),
    ])
    r = sess.post(f"{BASE}/prod/Multiview/LoadData", data=body, timeout=120)
    r.raise_for_status()
    docs, skipped = [], []
    for d in _extract_table(r.json(), "SF1D"):
        rec = {"doc_ref": str(d["DOC_REF"]), "doc_no": d.get("DOC_NO"),
               "filename": d.get("FILENAME") or "", "doc_status": str(d.get("DOC_STATUS")),
               "doc_source": str(d.get("DOC_SOURCE")),     # 1=External, 2=Internal(note)
               "doc_type": d.get("DOC_TYPE"), "doc_format": d.get("DOC_FORMAT"),
               "notes": d.get("NOTES"), "comments": d.get("COMMENTS"), "_row": d}
        (skipped if rec["doc_status"] == "5" else docs).append(rec)   # 5 = Deleted
    return cid, vid, docs, skipped

# ---------- naming ----------
def target_name(cid, vid, doc, ext_hint, taken):
    ext = os.path.splitext(doc["filename"])[1].lower() or ext_hint or ".bin"
    base = f"{FORM_NAME}_{cid}_{vid}_{doc['doc_ref']}"
    name = base + ext
    if name in taken:
        name = f"{base}_n{doc['doc_no']}{ext}"
    return name

# ---------- byte capture: PreloadDocViewer (POST) primes cache, then LoadDocViewer (GET) returns it ----------
def viewer_url(cache_id, doc_ref):
    p = [("commandName", "LoadDocViewer"), ("allTableCurrentRowData", _e1("[]")),
         ("columnName", "DOC_REF"), ("columnValue", str(doc_ref)),
         ("tableName", "SF1D"), ("formName", FORM_NAME), ("cacheID", cache_id)]
    return f"{BASE}/prod/Multiview/ProcessCommand?" + "&".join(f"{k}={v}" for k, v in p)

def _is_file(ct, cd):
    ct = (ct or "").split(";")[0].strip().lower(); cd = (cd or "").lower()
    if "attachment" in cd:
        return True
    return bool(ct) and not (ct.startswith("text/") or "html" in ct or "json" in ct or "javascript" in ct)

def preload_doc(sess, cache_id, token, entry_row, doc_row):
    """Prime the server-side viewer cache for one doc (the row must be SF1D's current row)."""
    uidk = _uid_key(entry_row) or "_unique_column_id_Wreg174kl"
    cid, vid = str(entry_row[COMPANY_COL]), entry_row[ID_COL]
    doc_class = doc_row.get("DOC_CLASS") or MOD["doc_class"](entry_row)   # per-doc class (handles AR mix)
    all_tables = [
        {"tableName": GRID,   "currentRowData": [entry_row]},
        {"tableName": "SF1D", "currentRowData": [doc_row]},          # the target doc = current row
        {"tableName": "FORM", "currentRowData": [{uidk: "0", "FORM_NAME": FORM_NAME,
            "FORM_STARTUP": "ENTER_QUERY"}]},
        {"tableName": "SF1P", "currentRowData": [{uidk: "0", "DATA_COMPANY_ID": cid,
            "DATA_ID": vid, "DOC_CLASS": doc_class}]},
    ]
    body = _body([
        ("tableName", "SF1D"), ("formName", FORM_NAME), ("rowValue", ""),
        ("commandName", "PreloadDocViewer"), ("formDataSet", ""),
        ("allTableCurrentRowData", _e2(all_tables)),
        ("cacheID", cache_id), ("__RequestVerificationToken", token),
    ])
    r = sess.post(f"{BASE}/prod/Multiview/ProcessCommand", data=body, timeout=120)
    r.raise_for_status()
    try:
        return (r.json() or {}).get("data", "")     # e.g. "application/pdf" — reported cached type
    except Exception:
        return ""

_MAGIC = [(b"%PDF-", "pdf", ".pdf"), (b"PK\x03\x04", "file", ".zip"),
          (b"\xff\xd8\xff", "file", ".jpg"), (b"\x89PNG\r\n\x1a\n", "file", ".png"),
          (b"II*\x00", "file", ".tiff"), (b"MM\x00*", "file", ".tiff"), (b"%!PS", "file", ".ps")]

def _load_sfdt_doc(sfdt_val):
    """Return the SFDT document JSON from a note's 'sfdt' value (optimized base64-zip or raw JSON)."""
    if isinstance(sfdt_val, dict):
        return sfdt_val
    if isinstance(sfdt_val, str):
        try:
            raw = base64.b64decode(sfdt_val)
            if raw[:2] == b"PK":
                zf = zipfile.ZipFile(io.BytesIO(raw))
                return json.loads(zf.read(zf.namelist()[0]))
        except Exception:
            pass
        return json.loads(sfdt_val)                     # non-optimized: value IS the doc JSON
    raise ValueError("unrecognized sfdt value")

def _sfdt_walk(node, out):
    if isinstance(node, dict):
        t = node.get("tlp", node.get("text"))
        if isinstance(t, str):
            out.append(t)
        is_block = ("i" in node) or ("inlines" in node)
        for k in ("sec", "sections", "b", "blocks", "i", "inlines", "rw", "rows", "c", "cells"):
            if k in node:
                _sfdt_walk(node[k], out)
        if is_block:
            out.append("\n")
    elif isinstance(node, list):
        for it in node:
            _sfdt_walk(it, out)

def sfdt_to_text(resp_bytes):
    """Extract plain text from a {'sfdt': ...} note response. Returns '' if nothing extractable."""
    doc = _load_sfdt_doc(json.loads(resp_bytes.decode("utf-8", "replace"))["sfdt"])
    out = []; _sfdt_walk(doc, out)
    return "\n".join(ln.rstrip() for ln in "".join(out).split("\n")).strip()

def classify(body, ct):
    """-> (kind, ext_hint). kind in {file, note, shell, error, unknown}."""
    for magic, kind, ext in _MAGIC:
        if body.startswith(magic):
            return kind, ext
    head = body.lstrip()[:1]
    if head in (b"{", b"["):
        try:
            j = json.loads(body.decode("utf-8", "replace"))
            if isinstance(j, dict) and "sfdt" in j:
                return "note", ".sfdt.json"
            if isinstance(j, dict) and str(j.get("status", "")).lower() not in ("", "success"):
                return "error", None
        except Exception:
            pass
    low = body[:400].lower()
    if b"<embed" in low and b"about:blank" in low:
        return "shell", None                        # viewer shell only, no bytes (should not happen via requests)
    if _is_file(ct, "") and not low.startswith(b"<!doctype") and b"<html" not in low:
        return "file", None
    return "unknown", None

def _clean_row(row):
    return {k: v for k, v in row.items() if not k.startswith("_") and v is not None}

def write_meta(name, entry_row, doc):
    """Write a {name}.meta.json sidecar with the parent entry's fields + this doc's metadata."""
    dr = doc.get("_row", {})
    meta = {
        "module": MOD["label"], "form_name": FORM_NAME,
        "company_id": str(entry_row[COMPANY_COL]), "record_id": entry_row[ID_COL], "id_column": ID_COL,
        "file": name,
        "doc": {"doc_ref": doc["doc_ref"], "doc_no": doc.get("doc_no"),
                "filename": doc.get("filename"), "doc_type": doc.get("doc_type"),
                "doc_source": doc.get("doc_source"), "doc_status": doc.get("doc_status"),
                "doc_class": dr.get("DOC_CLASS")},
        "entry": _clean_row(entry_row),
        "exported_ts": time.time(),
    }
    (OUTPUT_DIR / (name + ".meta.json")).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

def download_doc(sess, cid, vid, doc, cache_id, token, vl_row, taken, verbose=False):
    """Preload + fetch one SF1D row. Returns list of saved filenames (usually one)."""
    preload_doc(sess, cache_id, token, vl_row, doc["_row"])
    r = sess.get(viewer_url(cache_id, doc["doc_ref"]), headers=_VIEWER_HDRS, timeout=180)
    r.raise_for_status()
    ct = r.headers.get("Content-Type", "")
    kind, ext = classify(r.content, ct)
    if verbose:
        print(f"    doc {doc['doc_ref']} src={doc.get('doc_source')} ct={ct!r} "
              f"bytes={len(r.content)} -> {kind}")
    base = f"{FORM_NAME}_{cid}_{vid}_{doc['doc_ref']}"
    if kind in ("file", "pdf"):
        nm = target_name(cid, vid, doc, ext, taken); taken.add(nm)
        (OUTPUT_DIR / nm).write_bytes(r.content)
        write_meta(nm, vl_row, doc)
        return [nm]
    if kind == "note":
        try:
            text = sfdt_to_text(r.content)
        except Exception:
            text = ""
        if text:
            nm = base + "_note.txt"
            (OUTPUT_DIR / nm).write_text(text, encoding="utf-8")
        else:                                           # nothing extracted -> preserve raw, don't lose it
            nm = base + "_note.sfdt.json"
            (OUTPUT_DIR / nm).write_bytes(r.content)
            print(f"[WARN] {doc['doc_ref']}: note had no extractable text; saved raw {nm}", file=sys.stderr)
        taken.add(nm)
        write_meta(nm, vl_row, doc)
        return [nm]
    # shell / error / unknown -> fail loud with evidence
    ev = OUTPUT_DIR / f"_unresolved_{base}.txt"
    ev.write_text(r.content[:40000].decode("utf-8", "replace"), encoding="utf-8")
    raise RuntimeError(f"{doc['doc_ref']}: unhandled response kind={kind} ct={ct!r}; saved {ev.name}")

_VIEWER_HDRS = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8", "sec-fetch-dest": "iframe"}

# ---------- manifest ----------
def load_done():
    done = set()
    if MANIFEST.exists():
        for line in MANIFEST.open():
            rec = json.loads(line)
            if rec["status"] == "ok":
                done.add((rec.get("form_name", FORM_NAME), rec["company_id"], rec["voucher_id"]))
    return done

def record(**rec):
    rec["ts"] = time.time()
    with MANIFEST.open("a") as f:
        f.write(json.dumps(rec) + "\n")

# ---------- main ----------
def _http_status(e):
    return getattr(getattr(e, "response", None), "status_code", None)

def _reauth(driver):
    # mint a fresh cacheID/token; if bounced to the login page, log in first
    try:
        cache_id, token = open_form(driver)
    except SystemExit:
        login(driver)
        cache_id, token = open_form(driver)
    return cache_id, token, session_from_driver(driver)

def main():
    global MANIFEST
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--retries", type=int, default=4,
                    help="per-record attempts; re-auths on 401/403, backs off on 5xx")
    ap.add_argument("--form-url", help="override FORM_URL to select the module (AP/GL/AR)")
    ap.add_argument("--one", metavar="CID/VID",
                    help="fully process ONE voucher (enumerate->load->preload->fetch) verbosely, then exit")
    ap.add_argument("--dump-docs", metavar="CID/VID",
                    help="print the full SF1D rows for one voucher (e.g. 10/33), then exit")
    ap.add_argument("--year", type=int, metavar="YYYY",
                    help=f"band the run to one {DATE_COL} year (recommended for full exports)")
    ap.add_argument("--count-only", action="store_true",
                    help="enumerate (respecting --year) and print count + date span, download nothing")
    args = ap.parse_args()
    if args.form_url:
        _configure(args.form_url)

    if not MV_SUBDOMAIN or not BASE:
        sys.exit("set MV_SUBDOMAIN in your .env (the '<subdomain>' in https://<subdomain>.multiviewcorp.net)")
    if not os.environ.get("MV_USER") and not sys.stdin.isatty():
        sys.exit("set MV_USER / MV_PASS in your .env (or run interactively to be prompted)")
    if not OUTPUT_DIR or not OUTPUT_DIR.is_dir():
        sys.exit("set MV_EXPORT_DIR to an existing, access-controlled directory (store on encrypted media if data is sensitive)")
    MANIFEST = OUTPUT_DIR / f"_manifest_{FORM_NAME}.jsonl"

    driver = build_driver(headless=not args.no_headless)
    try:
        login(driver)
        cache_id, token = open_form(driver)
        sess = session_from_driver(driver)

        def _find_voucher(spec):
            c, v = spec.split("/"); v = int(v)
            for vl in enumerate_vouchers(sess, cache_id, token, args.year):
                if str(vl[COMPANY_COL]) == c and int(vl[ID_COL]) == v:
                    return vl
            sys.exit(f"voucher {spec} not found in enumeration")

        if args.count_only:
            rows = enumerate_vouchers(sess, cache_id, token, args.year)
            dates = sorted(r[DATE_COL] for r in rows if r.get(DATE_COL))
            span = f"{dates[0][:10]} .. {dates[-1][:10]}" if dates else "no dates on rows"
            print(f"[count] year={args.year or 'ALL'}: {len(rows)} vouchers | {DATE_COL} span {span}")
            return

        if args.one:
            vl = _find_voucher(args.one)
            cid, vid = str(vl[COMPANY_COL]), vl[ID_COL]
            _, _, docs, skipped = load_docs(sess, cache_id, token, vl)
            print(f"[one] {cid}/{vid}: {len(docs)} live docs, {len(skipped)} deleted")
            taken, saved = set(), []
            for d in docs:
                saved.extend(download_doc(sess, cid, vid, d, cache_id, token, vl, taken, verbose=True))
                time.sleep(THROTTLE_S)
            print(f"[one] saved {len(saved)} file(s): {saved}")
            return

        if args.dump_docs:
            vl = _find_voucher(args.dump_docs)
            cid, vid, docs, skipped = load_docs(sess, cache_id, token, vl)
            print(f"[dump-docs] {cid}/{vid}: {len(docs)} live, {len(skipped)} deleted")
            for d in docs + skipped:
                print(json.dumps({k: v for k, v in d.items() if k != "_row"}, ensure_ascii=False))
            return

        done = load_done()
        for vl in enumerate_vouchers(sess, cache_id, token, args.year):
            cid, vid = str(vl[COMPANY_COL]), vl[ID_COL]
            if (FORM_NAME, cid, vid) in done:
                continue
            for attempt in range(1, args.retries + 1):
                try:
                    _, _, docs, skipped = load_docs(sess, cache_id, token, vl)
                    taken, saved = set(), []
                    for d in docs:
                        saved.extend(download_doc(sess, cid, vid, d, cache_id, token, vl, taken))
                        time.sleep(THROTTLE_S)
                    record(status="ok", form_name=FORM_NAME, company_id=cid, voucher_id=vid, files=saved,
                           docs_seen=len(docs), year=args.year, skipped_deleted=[s["doc_ref"] for s in skipped])
                    break
                except Exception as e:
                    st = _http_status(e)
                    if attempt < args.retries and st in (401, 403):
                        print(f"    [reauth] {cid}/{vid}: session expired, re-authenticating", file=sys.stderr)
                        cache_id, token, sess = _reauth(driver)   # if this fails, aborts the year (resumable)
                        continue
                    if attempt < args.retries and st in (500, 502, 503, 504):
                        back = min(120, 10 * attempt)
                        print(f"    [retry {attempt}/{args.retries}] {cid}/{vid}: {st}; backoff {back}s", file=sys.stderr)
                        time.sleep(back)
                        continue
                    record(status="error", form_name=FORM_NAME, company_id=cid, voucher_id=vid, note=repr(e))
                    print(f"[ERROR] {cid}/{vid}: {e!r}", file=sys.stderr)
                    break
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
