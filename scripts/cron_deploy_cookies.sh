#!/usr/bin/env bash
# Wrapper for launchd/cron: sets PATH, logs, deploys YouTube cookies at a quiet hour.
# Prefer 07:00 local so listeners are less likely to hit a bot recreate mid-session.
set -euo pipefail

export HOME="${HOME:-/Users/samuelignaciocarmonarodriguez}"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:${PATH}"

# gcloud from the installer used in .zshrc
if [[ -f "${HOME}/Downloads/google-cloud-sdk/path.zsh.inc" ]]; then
  # shellcheck disable=SC1091
  source "${HOME}/Downloads/google-cloud-sdk/path.bash.inc" 2>/dev/null \
    || export PATH="${HOME}/Downloads/google-cloud-sdk/bin:${PATH}"
elif [[ -d "${HOME}/Downloads/google-cloud-sdk/bin" ]]; then
  export PATH="${HOME}/Downloads/google-cloud-sdk/bin:${PATH}"
fi

REPO="${SONA_REPO:-${HOME}/Documents/Repositories/spoty-scanner}"
LOG_DIR="${REPO}/.cache"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/cookie_deploy_cron.log"

# Non-interactive SSH (key must be unlocked / in agent or passphrase-less)
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-}"
# Prefer keychain-loaded identity if available
if [[ -f "${HOME}/.ssh/google_compute_engine" ]]; then
  # Load key into agent if agent is running and key not already loaded (best-effort)
  if command -v ssh-add >/dev/null 2>&1; then
    ssh-add -l 2>/dev/null | grep -q google_compute_engine \
      || ssh-add --apple-use-keychain "${HOME}/.ssh/google_compute_engine" 2>/dev/null \
      || ssh-add "${HOME}/.ssh/google_compute_engine" 2>/dev/null \
      || true
  fi
fi

{
  echo "======== $(date -u '+%Y-%m-%dT%H:%M:%SZ') start ========"
  echo "host=$(hostname) user=$(whoami)"
  command -v gcloud >/dev/null && gcloud --version 2>&1 | head -1 || echo "gcloud: missing"
  "${REPO}/scripts/deploy_youtube_cookies.sh" chrome
  echo "======== $(date -u '+%Y-%m-%dT%H:%M:%SZ') ok ========"
} >>"${LOG}" 2>&1
