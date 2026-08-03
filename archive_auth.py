#!/usr/bin/env python3
"""
archive_auth.py — per-user access control + at-rest encryption for the Multiview archive.

Design (envelope / key-wrapping encryption; standard pattern, vetted primitives only):
  - One random 256-bit MASTER KEY encrypts the archive file (AES-256-GCM).
  - The master key is never stored in the clear. For each authorized user it is stored
    WRAPPED under a key derived from that user's password (Scrypt KDF) with AES-256-GCM.
  - To open the archive: username + password -> derive KEK -> unwrap master key.
    A wrong password fails GCM authentication, so unwrapping *is* the authentication.
  - The keystore (usernames, salts, wrapped keys) contains NO PHI and is safe to sit
    unencrypted next to the archive; security rests on the KDF + the wrapped master key.

HONEST LIMITATION: this protects the file AT REST and gives per-user attribution +
audit on unlock. Once a session is unlocked, the plaintext exists in that process; a
technical user on the machine could bypass the app-level audit. This is a deterrent/
attribution control for a non-technical audience, not a guarantee against a technical
insider. It is NOT a substitute for a qualified compliance sign-off.

Dependencies: cryptography (pip install cryptography). Stdlib otherwise.
"""
import os, json, base64, socket, getpass, subprocess, datetime, argparse, sys
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1          # ~interactive cost; raise N for stronger
def _utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
def _b64(b): return base64.b64encode(b).decode()
def _ub64(s): return base64.b64decode(s)

def _derive(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(password.encode())

def _wrap(master: bytes, kek: bytes):
    nonce = os.urandom(12)
    return nonce, AESGCM(kek).encrypt(nonce, master, None)

def _unwrap(wrapped: bytes, nonce: bytes, kek: bytes) -> bytes:
    return AESGCM(kek).decrypt(nonce, wrapped, None)     # raises on wrong password

# ---------- keystore management ----------
def init_keystore(path):
    if os.path.exists(path):
        sys.exit(f"keystore already exists: {path}")
    master = AESGCM.generate_key(bit_length=256)
    json.dump({"created_utc": _utc(), "users": {}}, open(path, "w"), indent=2)
    os.chmod(path, 0o600)
    print(f"created keystore {path}. Master key generated (held in memory only for this call).")
    return master   # caller must add at least one user with this master before it's usable

def _load(path): return json.load(open(path))
def _save(path, ks): json.dump(ks, open(path, "w"), indent=2); os.chmod(path, 0o600)

def add_user(path, username, password, master: bytes):
    ks = _load(path)
    if username in ks["users"]:
        sys.exit(f"user exists: {username}")
    salt = os.urandom(16); nonce, wrapped = _wrap(master, _derive(password, salt))
    ks["users"][username] = {"salt": _b64(salt), "nonce": _b64(nonce),
                             "wrapped_master": _b64(wrapped), "added_utc": _utc()}
    _save(path, ks); print(f"added user {username}")

def remove_user(path, username):
    ks = _load(path)
    if ks["users"].pop(username, None) is None:
        sys.exit(f"no such user: {username}")
    _save(path, ks); print(f"removed user {username}")

def unlock(path, username, password) -> bytes:
    """Return the master key, or raise on bad credentials."""
    ks = _load(path); u = ks["users"].get(username)
    if not u:
        raise PermissionError("unknown user")
    try:
        return _unwrap(_ub64(u["wrapped_master"]), _ub64(u["nonce"]),
                       _derive(password, _ub64(u["salt"])))
    except Exception:
        raise PermissionError("bad password")

# ---------- archive file encryption (AES-256-GCM over the whole .db) ----------
def encrypt_db(plain_path, enc_path, master: bytes):
    data = open(plain_path, "rb").read()
    nonce = os.urandom(12); ct = AESGCM(master).encrypt(nonce, data, None)
    open(enc_path, "wb").write(nonce + ct); os.chmod(enc_path, 0o600)

def decrypt_db(enc_path, plain_path, master: bytes):
    blob = open(enc_path, "rb").read()
    data = AESGCM(master).decrypt(blob[:12], blob[12:], None)
    open(plain_path, "wb").write(data); os.chmod(plain_path, 0o600)

# ---------- tamper-EVIDENT audit (append-only hash chain) ----------
# Each record commits to the previous record's hash, so any edit/deletion/reordering
# breaks the chain and is detectable by verify_chain(). This is tamper-EVIDENT locally;
# it becomes tamper-PROOF once the tip hash is periodically anchored somewhere the user
# can't rewrite (Entra logs / a SharePoint list). See anchor_tip().
import hashlib
_GENESIS = "0" * 64

def _os_identity():
    user = getpass.getuser()
    try: full = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception: full = ""
    return user, full, socket.gethostname()

def _record_hash(rec: dict) -> str:
    # hash over canonical JSON of the record INCLUDING its prev_hash field
    return hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _last_hash(audit_path) -> str:
    if not os.path.exists(audit_path):
        return _GENESIS
    last = _GENESIS
    with open(audit_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = json.loads(line)["hash"]
    return last

def append_audit(audit_path, archive_user, action="unlock", detail=""):
    """Append one chained, tamper-evident audit record. Returns the new tip hash."""
    u, f, h = _os_identity()
    rec = {"ts_utc": _utc(), "archive_user": archive_user, "os_user": u, "os_fullname": f,
           "hostname": h, "action": action, "detail": detail, "prev_hash": _last_hash(audit_path)}
    rec["hash"] = _record_hash({k: v for k, v in rec.items()})   # hash over all fields incl prev_hash
    with open(audit_path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec["hash"]

def log_unlock(audit_path, archive_user):     # kept for call-site compatibility
    return append_audit(audit_path, archive_user, "unlock")

def verify_chain(audit_path):
    """Walk the log; return (ok, count, first_bad_line_or_None). Detects edits/deletions/reordering."""
    if not os.path.exists(audit_path):
        return True, 0, None
    prev = _GENESIS
    with open(audit_path) as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            stored = rec.pop("hash", None)
            if rec.get("prev_hash") != prev:            # broken link (deletion/reorder)
                return False, i, i
            if _record_hash(rec) != stored:             # edited contents
                return False, i, i
            prev = stored
    return True, i, None

def tip_hash(audit_path) -> str:
    """The latest chain hash — this is the ~64-char value to anchor off-box periodically."""
    return _last_hash(audit_path)

def main():
    ap = argparse.ArgumentParser(description="archive per-user access control")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init");      s.add_argument("keystore"); s.add_argument("--first-user", required=True)
    s = sub.add_parser("add-user");  s.add_argument("keystore"); s.add_argument("username")
    s = sub.add_parser("remove-user"); s.add_argument("keystore"); s.add_argument("username")
    s = sub.add_parser("unlock");    s.add_argument("keystore"); s.add_argument("username")
    s = sub.add_parser("verify-audit"); s.add_argument("audit_log")
    args = ap.parse_args()
    if args.cmd == "init":
        master = init_keystore(args.keystore)
        pw = getpass.getpass(f"set password for first user '{args.first_user}': ")
        add_user(args.keystore, args.first_user, pw, master)
        print("keystore ready. To encrypt an existing plaintext DB, unlock and call encrypt_db().")
    elif args.cmd == "add-user":
        admin = input("existing username to authorize this change: ")
        master = unlock(args.keystore, admin, getpass.getpass(f"{admin} password: "))
        add_user(args.keystore, args.username, getpass.getpass(f"new password for {args.username}: "), master)
    elif args.cmd == "remove-user":
        remove_user(args.keystore, args.username)
    elif args.cmd == "unlock":
        try:
            unlock(args.keystore, args.username, getpass.getpass("password: "))
            print("OK — credentials valid, master key unwrapped.")
        except PermissionError as e:
            sys.exit(f"denied: {e}")
    elif args.cmd == "verify-audit":
        ok, count, bad = verify_chain(args.audit_log)
        if ok:
            print(f"audit chain intact: {count} records, tip {tip_hash(args.audit_log)[:16]}…")
        else:
            sys.exit(f"AUDIT CHAIN BROKEN at record {bad} — tampering or corruption detected")

if __name__ == "__main__":
    main()
