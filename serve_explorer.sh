#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT="${1:-8080}"
echo "Spoty Scanner — Explorador de datos"
echo "  http://localhost:${PORT}/web/explorer.html"
echo ""
echo "Con Docker Compose ya está integrado:"
echo "  docker compose up -d explorer"
echo "  http://localhost:8080/web/explorer.html"
echo ""
echo "  Ctrl+C para detener"
export EXPLORER_PORT="$PORT"
export EXPLORER_HOST="127.0.0.1"
exec python3 web/server.py