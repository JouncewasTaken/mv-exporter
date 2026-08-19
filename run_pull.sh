#!/usr/bin/env bash
# Multiview pull — runs on the Linux tower. Loads .env from the repo dir.
set -euo pipefail

# --- paths (override via env if your layout differs) ---
REPO="${MV_REPO:-$HOME/Desktop/mv-exporter}"                       # where mv_tables.py + .env live
export MV_LANDING_DB="${MV_LANDING_DB:-$HOME/Desktop/MV_EXPORT/MV_LANDING.db}"
DIR="$(cd "$(dirname "$0")" && pwd)"                                # where the forms_*.txt lists live

mkdir -p "$(dirname "$MV_LANDING_DB")"
cd "$REPO"                       # so mv_exporter._load_dotenv() finds .env in CWD
PY() { python3 "$REPO/mv_tables.py" "$@"; }

band() {  # $1=list  $2=startYYYY-MM  $3=endYYYY-MM  rest=extra args
  local list="$1" start="$2" end="$3"; shift 3
  local cur="$start-01" stop; stop="$(date -d "$end-01 +1 month" +%Y-%m)"
  while [ "$(date -d "$cur" +%Y-%m)" != "$stop" ]; do
    local lo hi; lo="$(date -d "$cur" +%Y-%m-01)"; hi="$(date -d "$cur +1 month -1 day" +%Y-%m-%d)"
    echo ">>> $(basename "$list") $lo..$hi"
    PY --db "$MV_LANDING_DB" --forms "$list" --from "$lo" --to "$hi" "$@"
    cur="$(date -d "$cur +1 month" +%Y-%m-01)"
  done
}

WIDE_FROM="${WIDE_FROM:-2016-01-01}"; WIDE_TO="${WIDE_TO:-2026-12-31}"

case "${1:-}" in
  seat)        PY --db "$MV_LANDING_DB" --forms <(echo VOUCHER_F1) --from 2025-06-01 --to 2025-06-07 --no-headless ;;  # one-time MFA seat
  static)
    PY --db "$MV_LANDING_DB" --forms "$DIR/forms_static.txt"    --from "$WIDE_FROM" --to "$WIDE_TO"
    PY --db "$MV_LANDING_DB" --forms "$DIR/forms_ownership.txt" --from "$WIDE_FROM" --to "$WIDE_TO" --ownership-versions ;;
  closed)      band "$DIR/forms_transactional.txt" 2016-01 2025-12 ;;
  current)     band "$DIR/forms_transactional.txt" 2026-01 "$(date +%Y-%m)" ;;
  wholetable)  PY --db "$MV_LANDING_DB" --forms "$DIR/forms_wholetable.txt" --from "$WIDE_FROM" --to "$WIDE_TO" ;;
  seal)        sqlite3 "$MV_LANDING_DB" "PRAGMA wal_checkpoint(TRUNCATE);" ;;
  *) echo "usage: $0 {seat|static|closed|current|wholetable|seal}"; exit 2 ;;
esac

