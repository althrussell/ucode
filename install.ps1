<#
.SYNOPSIS
  ucode bootstrap installer (Windows).

.DESCRIPTION
  Zero-manual install that also repairs dirty machines: installs or updates uv,
  provisions a modern Python via uv (never relying on system Python), installs
  the latest ucode, then runs `ucode setup` which brings the Databricks CLI,
  Node.js/npm, and the agent CLIs up to date and configures everything.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/althrussell/ucode/main/install.ps1 | iex"

.NOTES
  Optional environment variables (for non-interactive / workshop rollout):
    UCODE_AGENTS=claude,codex      Agents to configure non-interactively.
    UCODE_PROFILE=workshop         Databricks CLI profile to target.
    UCODE_WORKSPACES=https://...   Comma-separated workspace URLs.
    UCODE_REF=main                 Git ref/branch/tag of ucode to install.
    UCODE_PYTHON_VERSION=3.12      Python version uv should provision.
#>

$ErrorActionPreference = 'Stop'
# Old PowerShell / locked-down hosts may default to TLS 1.0; force TLS 1.2 so
# the uv and GitHub downloads succeed.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$repo  = if ($env:UCODE_REPO) { $env:UCODE_REPO } else { 'https://github.com/althrussell/ucode' }
$ref   = if ($env:UCODE_REF) { $env:UCODE_REF } else { 'main' }
$pyver = if ($env:UCODE_PYTHON_VERSION) { $env:UCODE_PYTHON_VERSION } else { '3.12' }

function Write-Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Warn($m) { Write-Host "!   $m" -ForegroundColor Yellow }
function Test-HasCommand($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }

# 1. Ensure uv (install if missing, else best-effort self-update).
if (Test-HasCommand 'uv') {
  Write-Info 'uv is present; updating to latest (best-effort)'
  try { uv self update | Out-Null } catch { Write-Warn 'uv self update skipped (managed install?)' }
} else {
  Write-Info 'Installing uv'
  irm https://astral.sh/uv/install.ps1 | iex
}

# Make uv visible in THIS session (its installer wires future sessions).
$uvBin = Join-Path $env:USERPROFILE '.local\bin'
if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }

if (-not (Test-HasCommand 'uv')) {
  throw 'uv was installed but is not on PATH. Open a new terminal and re-run this installer.'
}

# 2. Provision Python via uv so an old/system Python is never relied upon.
Write-Info "Provisioning Python $pyver via uv"
try { uv python install $pyver } catch { Write-Warn "uv python install $pyver failed; uv will resolve an interpreter" }

# 3. Install ucode (force = always latest, replaces a stale copy).
#
# Install from the GitHub source tarball rather than `git+...` so a machine
# without Git still works (uv's git source requires a Git executable). The
# `<ref>.tar.gz` path accepts a branch, tag, or commit SHA.
Write-Info "Installing ucode from $repo (ref $ref)"
uv tool install --force --python $pyver "ucode @ $repo/archive/$ref.tar.gz"

# 4. Resolve ucode by absolute path — do not rely on PATH refreshing mid-script.
try { uv tool update-shell | Out-Null } catch {}
$binDir = $null
try { $binDir = (uv tool dir --bin).Trim() } catch {}
if ($binDir -and (Test-Path (Join-Path $binDir 'ucode.exe'))) {
  $ucode = Join-Path $binDir 'ucode.exe'
} elseif (Test-HasCommand 'ucode') {
  $ucode = (Get-Command ucode).Source
} else {
  throw 'ucode installed but could not be located. Open a new terminal and run: ucode setup'
}

# 5. Provision dependencies + configure + validate via `ucode setup`.
#
# Configuration prompts for a workspace, which needs a real console on stdin.
# With env inputs (workshop/unattended) we configure non-interactively; with an
# interactive console we configure with prompts; otherwise we install
# dependencies only and tell the user to finish with `ucode claude`.
Write-Info 'Running ucode setup'
$hasInputs = $env:UCODE_AGENTS -or $env:UCODE_PROFILE -or $env:UCODE_WORKSPACES
if ($hasInputs) {
  $setupArgs = @('setup')
  if ($env:UCODE_AGENTS)     { $setupArgs += @('--agents', $env:UCODE_AGENTS) }
  if ($env:UCODE_PROFILE)    { $setupArgs += @('--profile', $env:UCODE_PROFILE) }
  if ($env:UCODE_WORKSPACES) { $setupArgs += @('--workspaces', $env:UCODE_WORKSPACES) }
  & $ucode @setupArgs
  exit $LASTEXITCODE
} elseif (-not [Console]::IsInputRedirected) {
  & $ucode setup
  exit $LASTEXITCODE
} else {
  & $ucode setup --skip-configure
  Write-Host "`n==> ucode is installed. Open a NEW terminal and run:  ucode claude" -ForegroundColor Green
  exit 0
}
