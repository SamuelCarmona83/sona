#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT="${1:-8080}"
echo "Sona — Data explorer"
if [[ -f web/explorer/dist/index.html ]]; then
  echo "  Vue build → http://localhost:${PORT}/"
else
  echo "  Legacy HTML → http://localhost:${PORT}/web/explorer.html"
  echo "  (optional Vue build: cd web/explorer && npm install && npm run build)"
fi
echo ""
echo "Dev (HMR): python3 web/server.py &  cd web/explorer && npm run dev"
echo "Docker:    docker compose up -d explorer"
echo ""
echo "  Ctrl+C para detener"
export EXPLORER_PORT="$PORT"
export EXPLORER_HOST="127.0.0.1"
exec python3 web/server.py