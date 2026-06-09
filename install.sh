#!/bin/sh
# ucode bootstrap installer (macOS / Linux).
#
# Zero-manual install that also repairs dirty machines: it installs or updates
# uv, provisions a modern Python via uv (never relying on system Python),
# installs the latest ucode, then runs `ucode setup` which brings the Databricks
# CLI, Node.js/npm, and the agent CLIs up to date and configures everything.
#
# Usage (note the `sh -c "$(...)"` form so stdin stays attached to your terminal
# and the interactive workspace picker works):
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/althrussell/ucode/main/install.sh)"
#
# Optional environment variables (for non-interactive / workshop rollout):
#   UCODE_AGENTS=claude,codex      Agents to configure non-interactively.
#   UCODE_PROFILE=workshop         Databricks CLI profile to target.
#   UCODE_WORKSPACES=https://...   Comma-separated workspace URLs.
#   UCODE_REF=main                 Git ref/branch/tag of ucode to install.
#   UCODE_PYTHON_VERSION=3.12      Python version uv should provision.
#
# Note: POSIX sh (dash) does not support `pipefail`, so we use `set -eu` only.
set -eu

UCODE_REPO="${UCODE_REPO:-https://github.com/althrussell/ucode}"
UCODE_REF="${UCODE_REF:-main}"
PYTHON_VERSION="${UCODE_PYTHON_VERSION:-3.12}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$1" >&2; }
err()  { printf '\033[1;31mERROR\033[0m %s\n' "$1" >&2; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# 1. Ensure uv (install if missing, else best-effort self-update).
if need_cmd uv; then
  info "uv is present; updating to latest (best-effort)"
  uv self update >/dev/null 2>&1 || warn "uv self update skipped (managed install?)"
else
  info "Installing uv"
  if need_cmd curl; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif need_cmd wget; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    err "Need curl or wget to install uv. Install one and re-run."
    exit 1
  fi
fi

# Make uv visible in THIS shell (its installer wires future shells via env files).
for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
  case ":$PATH:" in
    *":$d:"*) ;;
    *) [ -d "$d" ] && PATH="$d:$PATH" ;;
  esac
done
export PATH

if ! need_cmd uv; then
  err "uv was installed but is not on PATH. Open a new terminal and re-run this installer."
  exit 1
fi

# 2. Provision Python via uv so an old/system Python is never relied upon.
info "Provisioning Python ${PYTHON_VERSION} via uv"
uv python install "${PYTHON_VERSION}" \
  || warn "uv python install ${PYTHON_VERSION} failed; uv will resolve an interpreter"

# 3. Install ucode (force = always latest, replaces a stale copy).
info "Installing ucode from ${UCODE_REPO}@${UCODE_REF}"
uv tool install --force --python "${PYTHON_VERSION}" "git+${UCODE_REPO}@${UCODE_REF}"

# 4. Resolve ucode by absolute path — do not rely on PATH refreshing mid-script.
uv tool update-shell >/dev/null 2>&1 || true
BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
if [ -n "${BIN_DIR}" ] && [ -x "${BIN_DIR}/ucode" ]; then
  UCODE_BIN="${BIN_DIR}/ucode"
elif need_cmd ucode; then
  UCODE_BIN="$(command -v ucode)"
else
  err "ucode installed but could not be located. Open a new terminal and run: ucode setup"
  exit 1
fi

# 5. Provision dependencies + configure + validate via `ucode setup`.
info "Running ucode setup"
set --
[ -n "${UCODE_AGENTS:-}" ]     && set -- "$@" --agents "${UCODE_AGENTS}"
[ -n "${UCODE_PROFILE:-}" ]    && set -- "$@" --profile "${UCODE_PROFILE}"
[ -n "${UCODE_WORKSPACES:-}" ] && set -- "$@" --workspaces "${UCODE_WORKSPACES}"

# Decide whether to configure interactively. Configuration needs to prompt for a
# workspace, which requires a real terminal on stdin. When inputs are supplied
# via env vars (workshop/unattended) we configure non-interactively. When stdin
# is an interactive terminal (the `sh -c "$(curl ...)"` form) we configure with
# prompts. Otherwise (e.g. `curl ... | sh`, CI) we install dependencies only and
# tell the user to finish with `ucode claude` — no fragile TTY juggling.
if [ -n "${UCODE_WORKSPACES:-}" ] || [ -n "${UCODE_PROFILE:-}" ] || [ -n "${UCODE_AGENTS:-}" ]; then
  exec "${UCODE_BIN}" setup "$@"
elif [ -t 0 ]; then
  exec "${UCODE_BIN}" setup
else
  "${UCODE_BIN}" setup --skip-configure || true
  printf '\n\033[1;32m==>\033[0m ucode is installed. Open a NEW terminal and run:  ucode claude\n'
  exit 0
fi
