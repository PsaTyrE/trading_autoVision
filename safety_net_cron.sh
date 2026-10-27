#!/bin/bash
# Pi-local safety net runner: checks naked positions on BOTH live+demo accounts
# (from the Pi itself, so the live API key IP whitelist is satisfied) and writes
# a status file. Hermes-side monitor only READS this file (never calls Binance
# from the Hermes container, which is IP-blocked on the live key).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
PYTHON_BIN="${VIRTUAL_ENV:-$DIR/venv}/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then PYTHON_BIN="python3"; fi
OUT=$($PYTHON_BIN safety_net.py 2>>safety_net.err.log)
RC=$?
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
  echo "{"
  echo "  \"ts\": \"$TS\","
  echo "  \"rc\": $RC,"
  echo "  \"data\": $OUT"
  echo "}"
} > safety_net_status.json.tmp && mv safety_net_status.json.tmp safety_net_status.json
exit 0
