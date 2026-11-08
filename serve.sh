#!/bin/bash
# Listen only on the address assigned to tailscale0.
set -euo pipefail

cd "$(dirname "$0")"
set -a
# shellcheck disable=SC1091
source ./pc-power.env
set +a

ip=""
for _ in $(seq 1 60); do
  ip="$(ip -4 -o addr show dev tailscale0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
  case "$ip" in
    100.*) break ;;
  esac
  sleep 1
  ip=""
done

case "$ip" in
  100.*) ;;
  *)
    echo "refusing to listen: tailscale0 has no 100.x address" >&2
    exit 1
    ;;
esac

port="${PORT:-8741}"
echo "pc-power listening on ${ip}:${port}"
exec ./.venv/bin/uvicorn server:app --host "$ip" --port "$port" --app-dir "$(pwd)"
