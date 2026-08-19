#!/usr/bin/env bash
# Document pull — all 3 doc modules across a year range. Resumes via each module's manifest.
set -euo pipefail
REPO="${MV_REPO:-$HOME/Desktop/mv-exporter}"
cd "$REPO"                                  # so .env loads and mv_exporter finds MV_EXPORT_DIR
: "${MV_EXPORT_DIR:=$(grep '^MV_EXPORT_DIR=' .env | cut -d= -f2-)}"
mkdir -p "$MV_EXPORT_DIR"; chmod 700 "$MV_EXPORT_DIR"

MODULES=(VOUCHER_F1 ENTRY_F1 AR_INQUIRY_F1)
START="${DOCS_FROM_YEAR:-2016}"; END="${DOCS_TO_YEAR:-$(date +%Y)}"
PY() { python3 "$REPO/mv_exporter.py" "$@"; }

case "${1:-}" in
  seat)  PY --form-url VOUCHER_F1 --year "$END" --no-headless --count-only ;;  # interactive: seat MFA + verify enum
  count) for m in "${MODULES[@]}"; do for y in $(seq "$START" "$END"); do PY --form-url "$m" --year "$y" --count-only; done; done ;;
  one)   PY --form-url "${2:?module}" --one "${3:?CID/VID}" ;;                  # single-doc validation
  run)   for m in "${MODULES[@]}"; do for y in $(seq "$START" "$END"); do echo ">>> $m $y"; PY --form-url "$m" --year "$y"; done; done ;;
  *) echo "usage: $0 {seat|count|one <MODULE> <CID/VID>|run}"; exit 2 ;;
esac
