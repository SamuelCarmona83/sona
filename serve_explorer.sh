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
exec python3 -m http.server "$PORT"