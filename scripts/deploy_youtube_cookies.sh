#!/usr/bin/env bash
# Refresh YouTube cookies on this Mac and install them on the GCE bot host.
#
# Usage (from repo root or anywhere):
#   ./scripts/deploy_youtube_cookies.sh
#   ./scripts/deploy_youtube_cookies.sh chrome
#   ./scripts/deploy_youtube_cookies.sh --skip-refresh   # only upload existing cookies.txt
#
# Cron / launchd (every 12h) example:
#   0 */12 * * * /path/to/spoty-scanner/scripts/deploy_youtube_cookies.sh chrome >>/tmp/sona-cookies.log 2>&1
#
# Env overrides:
#   GCE_INSTANCE   default: instance-sona
#   GCE_ZONE       default: us-east1-d
#   GCE_PROJECT    default: (gcloud config)
#   GCE_DEPLOY_USER default: samuel_carmona_rodrigz
#   GCE_DEPLOY_DIR  default: /home/<user>/sona
#   BROWSER         default: chrome (or first non-flag arg)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BROWSER="${BROWSER:-chrome}"
SKIP_REFRESH=false
RECREATE=true

GCE_INSTANCE="${GCE_INSTANCE:-instance-sona}"
GCE_ZONE="${GCE_ZONE:-us-east1-d}"
GCE_PROJECT="${GCE_PROJECT:-}"
GCE_DEPLOY_USER="${GCE_DEPLOY_USER:-samuel_carmona_rodrigz}"
GCE_DEPLOY_DIR="${GCE_DEPLOY_DIR:-/home/${GCE_DEPLOY_USER}/sona}"

for arg in "$@"; do
  case "$arg" in
    --skip-refresh) SKIP_REFRESH=true ;;
    --no-recreate) RECREATE=false ;;
    chrome|chromium|edge|firefox|opera) BROWSER="$arg" ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
  esac
done

COOKIES_LOCAL="${ROOT_DIR}/cookies.txt"
PYTHON_BIN="${ROOT_DIR}/venv/bin/python"
[[ -x "${PYTHON_BIN}" ]] || PYTHON_BIN="python3"

GCLOUD=(gcloud)
if [[ -n "${GCE_PROJECT}" ]]; then
  GCLOUD+=(--project="${GCE_PROJECT}")
fi

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null || die "gcloud not found in PATH"

# --- 1) Export cookies from local browser ---
if [[ "${SKIP_REFRESH}" != "true" ]]; then
  log "Exporting cookies from ${BROWSER}…"
  mkdir -p "${ROOT_DIR}/.cache"
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/refresh_youtube_cookies.py" \
    --browser "${BROWSER}" \
    --output "${COOKIES_LOCAL}" \
    || die "cookie export failed (is Chrome logged into YouTube?)"
else
  log "Skipping browser export (--skip-refresh)"
fi

[[ -f "${COOKIES_LOCAL}" ]] || die "missing ${COOKIES_LOCAL}"
[[ ! -d "${COOKIES_LOCAL}" ]] || die "${COOKIES_LOCAL} is a directory — remove it and re-export"

# Refuse empty / non-Netscape junk
if ! head -1 "${COOKIES_LOCAL}" | grep -qi 'Netscape\|HTTP Cookie File\|^#'; then
  # still ok if first line is a cookie line (tab-separated)
  if ! head -5 "${COOKIES_LOCAL}" | grep -q $'\t'; then
    die "${COOKIES_LOCAL} does not look like a Netscape cookie file"
  fi
fi

COUNT="$(grep -cve '^\s*$\|^\s*#' "${COOKIES_LOCAL}" || true)"
[[ "${COUNT}" -gt 5 ]] || die "too few cookie lines (${COUNT}); export looks empty"
log "Local cookies ready (${COUNT} entries, $(wc -c <"${COOKIES_LOCAL}" | tr -d ' ') bytes)"

# --- 2) Upload to VM /tmp (OS Login home — always writable) ---
log "Uploading to ${GCE_INSTANCE}:/tmp/sona-cookies.txt …"
"${GCLOUD[@]}" compute scp \
  --zone="${GCE_ZONE}" \
  "${COOKIES_LOCAL}" \
  "${GCE_INSTANCE}:/tmp/sona-cookies.txt"

# --- 3) Install as real FILE under deploy user + recreate bot mount ---
# OS Login user cannot read deploy user's $HOME (mode 700) — always use sudo for checks.
# Flat remote script (no nested heredocs) so gcloud ssh --command stays reliable.
# IMPORTANT: never use shell redirects like `sudo cmd < $DEST` — the redirect is
# opened by the OS Login user *before* sudo, and deploy $HOME is mode 700.
REMOTE_CMD=$(printf '%s\n' \
  'set -euo pipefail' \
  "DEPLOY_USER='${GCE_DEPLOY_USER}'" \
  "DEPLOY_DIR='${GCE_DEPLOY_DIR}'" \
  "RECREATE='${RECREATE}'" \
  "SRC='/tmp/sona-cookies.txt'" \
  'DEST="${DEPLOY_DIR}/cookies.txt"' \
  'sudo test -f "${SRC}" || { echo "missing ${SRC}"; exit 1; }' \
  'sudo mkdir -p "${DEPLOY_DIR}"' \
  'if sudo test -d "${DEST}"; then echo "warning: ${DEST} is a directory — removing"; sudo rm -rf "${DEST}"; fi' \
  'if sudo test -L "${DEST}"; then sudo rm -f "${DEST}"; fi' \
  'sudo cp "${SRC}" "${DEST}"' \
  'sudo chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEST}"' \
  'sudo chmod 644 "${DEST}"' \
  'if ! sudo test -f "${DEST}" || sudo test -d "${DEST}"; then echo "error: ${DEST} not a regular file"; sudo ls -la "${DEPLOY_DIR}" || true; exit 1; fi' \
  'BYTES=$(sudo wc -c "${DEST}" | awk "{print \$1}")' \
  'echo "installed ${BYTES} bytes -> ${DEST}"' \
  'sudo head -2 "${DEST}" || true' \
  'if [ "${RECREATE}" = "true" ]; then' \
  '  sudo -iu "${DEPLOY_USER}" bash -lc "cd \"${DEPLOY_DIR}\" && docker compose up -d --force-recreate bot && sleep 2 && (docker exec sona-bot-1 head -1 /app/cookies.txt 2>/dev/null || docker compose exec -T bot head -1 /app/cookies.txt) && (docker logs --tail 12 sona-bot-1 2>/dev/null || docker compose logs --tail 12 bot)"' \
  'else' \
  '  echo "skip recreate; bot should hot-reload on mtime change"' \
  'fi' \
  'sudo rm -f /tmp/sona-cookies.txt' \
  'echo done' \
)

log "Installing on VM as ${GCE_DEPLOY_USER}:${GCE_DEPLOY_DIR}/cookies.txt …"
"${GCLOUD[@]}" compute ssh \
  --zone="${GCE_ZONE}" \
  "${GCE_INSTANCE}" \
  --command="${REMOTE_CMD}"

log "OK — cookies deployed. Check: docker logs should show 'using cookies file: /app/cookies.txt'"
