# Optional: archive encryption & access audit

`archive_auth.py` is an **optional** module for organizations whose archive contains
regulated or sensitive data (e.g. PHI/PII). The core exporter (`mv_exporter.py`,
`mv_tables.py`) produces a plain SQLite archive; this module adds at-rest encryption,
per-user access, and a tamper-evident access log. It is **not** wired into the exporter
by default — you opt in.

> **This module is a starting point, not a compliance guarantee.** It gives you strong,
> standard controls, but it does not make your organization HIPAA/GDPR/etc. compliant.
> If your data is regulated, have a qualified person accept the residual risk and sign
> off on your controls. See [Honest limitations](#honest-limitations).

## Dependencies

```
pip install cryptography
```

Everything else is Python standard library.

## How it works (envelope encryption)

- A single random 256-bit **master key** encrypts the archive file with AES-256-GCM.
- The master key is never stored in the clear. For each authorized user it is stored
  **wrapped** under a key derived from that user's password (Scrypt KDF), again with
  AES-256-GCM.
- To open the archive: `username` + `password` → derive the wrapping key → unwrap the
  master key. A wrong password fails the GCM authentication, so **unwrapping is the
  authentication** — there's no separate password hash to manage.
- The **keystore** (`keystore.json`) holds only usernames, salts, and wrapped keys —
  **no PHI** — so it can sit unencrypted next to the archive. Security rests on the KDF
  and the fact that the master key is only ever stored wrapped.

Adding a user wraps the master key under their password; removing a user deletes their
wrapped entry. No re-keying of the archive is needed to add or revoke access.

## Usage

```bash
# create the keystore and the first user (generates the master key)
python archive_auth.py init keystore.json --first-user alice

# authorize additional users (an existing user must approve)
python archive_auth.py add-user keystore.json bob
python archive_auth.py remove-user keystore.json bob

# test a credential (unwraps the master key, prints OK/denied)
python archive_auth.py unlock keystore.json alice

# verify the audit chain is intact
python archive_auth.py verify-audit audit.log
```

Programmatically (e.g. from a launcher that gates the archive):

```python
import archive_auth as A
master = A.unlock("keystore.json", username, password)   # raises PermissionError on bad creds
A.decrypt_db("archive.db.enc", "/secure/tmp/archive.db", master)  # for a read-only session
A.append_audit("audit.log", username, "unlock")          # record who opened it
# ... serve/query the decrypted archive read-only, then remove the temp copy ...
```

To encrypt an archive the exporter produced:

```python
master = A.unlock("keystore.json", admin_user, admin_pw)
A.encrypt_db("archive.db", "archive.db.enc", master)     # then delete the plaintext archive.db
```

## Tamper-evident audit log

Each access appends a record to an append-only log; every record includes the SHA-256
hash of the previous record (a hash chain). Any edit, deletion, or reordering breaks the
chain, and `verify_chain()` reports the first broken link. `tip_hash()` returns the
latest chain hash.

This is **tamper-evident**, not tamper-proof: someone who controls the machine and
understands the scheme can recompute the chain forward from a change. To make it
tamper-**proof**, periodically publish just the tip hash (a 64-char string, no data)
somewhere the user cannot rewrite — see below.

## Off-box anchoring (recommended for regulated data)

The database stays local; only tiny auth/audit artifacts go off the machine, so this
adds no storage cost:

- **Authentication** — sign in through your identity provider (e.g. Microsoft Entra ID
  via MSAL). The sign-in itself is then recorded in the IdP's logs automatically, off
  the machine, where the user can't edit it.
- **Audit anchor** — periodically push the current `tip_hash()` to a location the user
  can only append to (a SharePoint list, a Log Analytics custom event, etc.). Once the
  tip is anchored off-box, the local chain can't be silently rewritten behind that point.

This split — data local, auth + audit off-box — gives strong accountability without
hosting the archive anywhere. The IdP/anchor integration is environment-specific and is
left to the deploying organization.

## Honest limitations

- **Local decryption is bypassable by a technical user.** Once a session decrypts the
  archive, the plaintext exists on that machine; a capable user could read it outside
  any app-level logging. This is a deterrent/attribution control against a non-technical
  audience, the same category as a locked filing cabinet with a sign-out sheet — not a
  guarantee against a determined insider. If you need strict, unbypassable access + audit,
  **serve** the archive (never hand out the file) behind an authenticating proxy instead.
- **Don't roll your own crypto.** This module composes vetted primitives (`cryptography`'s
  Scrypt + AES-256-GCM) in the standard envelope pattern; it does not invent crypto. If
  you change it, keep it to standard, reviewed constructions.
- **This is not legal or compliance advice.** It is tooling. Ownership of the compliance
  decision stays with your organization.
