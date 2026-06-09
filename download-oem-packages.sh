#!/usr/bin/env bash
# download-oem-packages.sh — Wrapper for patch_resolver.py CLI
#
# Downloads OEM patch packages from remote APT repositories.
# No local apt-get required — all resolution via HTTP + dpkg-deb.
#
# Usage:
#   ./download-oem-packages.sh --patch-repo "URL DIST COMP PRIORITY" [options]
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PWD}/.venv"
RESOLVER="patch_resolver.py"

# ── Ensure Python and deps ─────────────────────────────────────────────────────

function ensure_env() {
  local py="python3"
  if ! ${py} -c 'import packaging' >/dev/null 2>&1; then
    echo "[setup] Installing Python dependencies..."
    if ! ${py} -m venv "$VENV_DIR" >/dev/null 2>&1; then
      echo "ERROR: Cannot create virtualenv. Install python3-venv:"
      echo "    sudo apt install python3-venv python3-full"
      exit 1
    fi
    source "${VENV_DIR}/bin/activate"
    python -m pip install --upgrade pip -q
    python -m pip install -r requirements.txt -q
  fi
  if [[ -d "$VENV_DIR" ]]; then
    source "${VENV_DIR}/bin/activate"
  fi
}

ensure_env

# ── Forward to Python CLI ──────────────────────────────────────────────────────

PY_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --patch-repo)
      PY_ARGS+=("--patch-repo" "$2"); shift 2 ;;
    --base-list|--patch-list|--output-dir|--architecture)
      PY_ARGS+=("$1" "$2"); shift 2 ;;
    --include-recommends|--dry-run)
      PY_ARGS+=("$1"); shift ;;
    --max-workers|--retry|--max-depth)
      PY_ARGS+=("$1" "$2"); shift 2 ;;
    --patch-repo-url)
      # Legacy: translate to --patch-repo format
      local url="$2" dist="${3:-unstable}" comp="${4:-commercial}"
      PY_ARGS+=("--patch-repo" "${url} ${dist} ${comp} 0")
      shift 4 ;;
    --patch-distribution|--patch-components)
      # Consumed by --patch-repo-url above (legacy compat)
      shift 2 ;;
    -h|--help)
      echo "Usage: $0 [options]"
      echo ""
      echo "Options:"
      echo "  --patch-repo 'URL DIST COMP PRIORITY'  Repository (repeatable)"
      echo "  --base-list FILE                       Base package list (default: packages-full-x86_64.txt)"
      echo "  --patch-list FILE                      Patch package list (default: patch-packages.txt)"
      echo "  --output-dir DIR                       Download dir (default: download)"
      echo "  --architecture ARCH                    Target: amd64|arm64|all (auto-detected from base list)"
      echo "  --dry-run                              Preview without downloading"
      echo "  --include-recommends                   Resolve Recommends"
      echo "  --max-workers N                        Concurrent downloads (default: 8)"
      echo "  --retry N                              Retry count (default: 1)"
      echo "  --max-depth N                          Max dependency recursion depth (default: 10)"
      exit 0 ;;
    *)
      echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ ${#PY_ARGS[@]} -eq 0 ]] && [[ -f oem-patch-sources.list ]]; then
  # Convenience: read first source line from oem-patch-sources.list
  local line
  line=$(grep -v '^#' oem-patch-sources.list | grep -v '^$' | head -1 || true)
  if [[ -n "$line" ]]; then
    PY_ARGS+=("--patch-repo" "${line} 0")
  fi
fi

exec "${VENV_DIR}/bin/python" "${RESOLVER}" "${PY_ARGS[@]}"
