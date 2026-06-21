#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BROWSER="chrome"
RESTART_DOCKER=false
PYTHON_BIN="${ROOT_DIR}/venv/bin/python"
LOG_FILE="${ROOT_DIR}/.cache/cookie_refresh.log"

for arg in "$@"; do
  case "$arg" in
    --restart) RESTART_DOCKER=true ;;
    chrome|chromium|edge|firefox|opera) BROWSER="$arg" ;;
  esac
done

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

mkdir -p "${ROOT_DIR}/.cache"
cd "${ROOT_DIR}"

if ! "${PYTHON_BIN}" scripts/refresh_youtube_cookies.py --browser "${BROWSER}" --output "${ROOT_DIR}/cookies.txt"; then
  echo "Cookie refresh failed." >&2
  exit 1
fi

TIMESTAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
COUNT="$("${PYTHON_BIN}" -c "
from pathlib import Path
p = Path('${ROOT_DIR}/cookies.txt')
print(sum(1 for line in p.read_text().splitlines() if line.strip() and not line.startswith('#')))
")"
echo "${TIMESTAMP} refreshed ${COUNT} cookies from ${BROWSER}" >> "${LOG_FILE}"

echo
printf 'Cookies refreshed (%s entries). The bot will detect the change automatically.\n' "${COUNT}"

if [[ "${RESTART_DOCKER}" == "true" ]]; then
  if command -v docker >/dev/null 2>&1; then
    docker compose restart bot
    printf 'Docker bot restarted.\n'
  else
    printf 'docker not found — skip restart.\n'
  fi
fi