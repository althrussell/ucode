# ucode deployment guide — Windows

A copy-paste-runnable guide for installing and rolling out `ucode` on Windows.
You do **not** need to install anything by hand first — the one-liner provisions
the entire toolchain and repairs dirty machines.

---

## 1. Overview & what gets installed

`ucode` launches Codex, Claude Code, Gemini CLI, OpenCode, GitHub Copilot CLI,
and Pi against a Databricks AI Gateway. The installer brings up the full
dependency chain, none of which you install manually:

| Dependency | Why | How it's provisioned |
|------------|-----|----------------------|
| `uv` | Installs/runs `ucode` and manages Python | Astral installer, or `uv self update` if present |
| Python 3.12 | `ucode` runtime | `uv python install 3.12` (never relies on system Python) |
| Databricks CLI (latest) | Auth + token refresh | `winget install Databricks.DatabricksCLI --source winget`, upgraded if behind a GitHub release compare |
| Node.js / npm | Installs the npm-distributed agent CLIs | `winget install OpenJS.NodeJS` (or nodejs.org MSI) |
| Agent CLIs | The agents you launch | `npm install -g` per agent |
| MLflow CLI (optional) | Only for Claude Code tracing | `uv tool install "ucode[tracing]"` path |

`jq`, `sh`, and a bash shell are **not** required — the credential helper is now
the native `ucode auth-token` command, which fixed the previous Windows token
refresh failures.

`winget` (the "App Installer", bundled with Windows 10/11) is used to provision
the Databricks CLI and Node.js. If it is missing — common on Windows Server
images — install **App Installer** from the Microsoft Store first, or install
those two dependencies manually; `ucode setup` will tell you exactly what to do.

---

## 2. One-command install

In **PowerShell**:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/althrussell/ucode/main/install.ps1 | iex"
```

`-ExecutionPolicy Bypass` lets the script run on locked-down hosts without
changing the machine policy, and the script forces TLS 1.2 for older Windows
images. It installs/updates `uv`, provisions Python 3.12, installs the latest
`ucode`, then runs `ucode setup`.

---

## 3. Dirty-machine notes

The installer is idempotent and self-healing:

- **Old/system Python:** ignored — `uv` provisions its own Python 3.12.
- **Outdated Databricks CLI:** `ucode setup` compares against the latest GitHub
  release and upgrades in place via `winget` (`Databricks.DatabricksCLI`, source
  pinned to `winget` so the Microsoft Store source can't trigger a certificate
  failure). Offline, or winget unavailable? It falls back to enforcing the
  minimum supported version and points you at the manual installer.
- **Missing or old Node.js:** installed/upgraded via `winget`
  (`OpenJS.NodeJS`), or from the nodejs.org MSI if winget is unavailable. A
  working-but-older Node is upgraded best-effort and never blocks the run.
- **Locked-down PowerShell execution policy:** handled by `-ExecutionPolicy
  Bypass` on the one-liner; you do not need admin rights to change machine
  policy.
- **TLS:** the script sets TLS 1.2 explicitly so downloads succeed on older
  Windows builds.

Re-run the one-liner or `ucode setup` any time to repair.

---

## 4. PATH & shells

- `uv` installs tool binaries to `%USERPROFILE%\.local\bin`.
- The installer runs `uv tool update-shell`, which adds that directory to your
  user PATH. **Open a new PowerShell window** to pick it up.
- **PowerShell vs cmd.exe:** both work for launching `ucode`. The credential
  helper command baked into agent configs is double-quoted so it parses under
  both shells even when the path contains spaces (e.g.
  `C:\Users\First Last\.local\bin\ucode.exe`).
- Verify:

  ```powershell
  where.exe ucode databricks node
  ```

If `ucode` isn't found mid-session, the credential helper still works because
agent configs bake the absolute path — but run `uv tool update-shell` and open a
new window for interactive use.

---

## 5. Authentication & profiles

Log in to a workspace (creates/refreshes a named profile in `%USERPROFILE%\.databrickscfg`):

```powershell
databricks auth login --host https://your-workspace.databricks.com --profile workshop
```

Launch an agent against that profile:

```powershell
ucode claude --profile workshop
```

`--profile` resolves the workspace automatically. An unknown profile fails fast
with the exact `databricks auth login` command to create it.

**Duplicate-host example.** Suppose `.databrickscfg` has both:

```ini
[DEFAULT]
host = https://your-workspace.databricks.com

[workshop]
host = https://your-workspace.databricks.com
```

`ucode` no longer fails to disambiguate. The workspace picker lists both:

```
https://your-workspace.databricks.com  (profile: DEFAULT)
https://your-workspace.databricks.com  (profile: workshop)
```

and `ucode claude --profile workshop` selects the named one unambiguously.

---

## 6. Per-agent launch

```powershell
ucode codex      # OpenAI Codex
ucode claude     # Claude Code
ucode gemini     # Gemini CLI
ucode opencode   # OpenCode
ucode copilot    # GitHub Copilot CLI
ucode pi         # Pi
```

Flags after the agent name pass straight through:

```powershell
ucode claude -r                 # resume last session
ucode codex --full-auto
ucode claude --profile workshop -r
```

On Windows, `ucode` spawns the agent's `.cmd` shim correctly and forwards
Ctrl-C, so sessions start and stop cleanly. First launch of an agent
auto-configures it; later launches go straight to the agent.

---

## 7. Workshop at-scale rollout

The single non-interactive command to script across many machines:

```powershell
ucode setup --agents claude,codex --profile workshop --workspaces https://your-workspace.databricks.com
```

Or via the bootstrap one-liner with environment variables (handy for Intune or
an MDM script):

```powershell
$env:UCODE_AGENTS     = "claude,codex"
$env:UCODE_PROFILE    = "workshop"
$env:UCODE_WORKSPACES = "https://your-workspace.databricks.com"
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/althrussell/ucode/main/install.ps1 | iex"
```

For **Intune / MDM**: deploy the PowerShell one-liner as a platform script (run
in the user context so `uv`'s per-user install lands correctly). Pre-seed
profiles by distributing `.databrickscfg` or running `databricks auth login
--profile <name>`. `ucode setup` is idempotent, so a recurring policy can run it
safely on every check-in.

---

## 8. Verification

```powershell
ucode doctor
```

Reads each check as **PASS / WARN / SKIP / FAIL**:

- **PASS** — healthy.
- **WARN** — works but suboptimal (e.g. an update is available, or `ucode` isn't
  on PATH yet).
- **SKIP** — a prerequisite wasn't met, so the dependent check didn't run (e.g.
  gateway checks skip when auth fails).
- **FAIL** — a problem; critical fails make `ucode doctor` exit non-zero.

The end-to-end **credential helper smoke test** runs `ucode auth-token` exactly
as the agents invoke it — the surest confirmation that token refresh works on
this machine.

Auto-repair what's fixable:

```powershell
ucode doctor --fix
```

---

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Script "cannot be loaded because running scripts is disabled" | Use the `-ExecutionPolicy Bypass` one-liner exactly as shown. |
| TLS / download errors on older Windows | The script forces TLS 1.2; if a proxy strips it, set the proxy to allow TLS 1.2. |
| Corporate proxy / SSL inspection blocks downloads | Set `$env:HTTPS_PROXY`/`$env:HTTP_PROXY`; import the proxy CA into the Windows cert store. |
| `ucode` not recognized in a **new** terminal (worked during install) | The bin dir wasn't persisted to your user PATH. Run once: `[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';' + (Join-Path $env:USERPROFILE '.local\bin'), 'User')`, then open a new terminal. (The current installer does this automatically.) |
| `ucode` / `databricks` not found | `uv tool update-shell`, open a new window; or run `ucode setup`. |
| Node too old | `ucode doctor --fix` or `winget install OpenJS.NodeJS`. |
| `npm` permission errors | Reinstall Node via `winget` (per-user) so global installs don't need admin. |
| GitHub rate-limit message during CLI update | Harmless — `ucode` falls back to the minimum-version check and continues. |
| Token / login failures | `databricks auth login --host <url> --profile <name>`, then `ucode doctor`. |
| Want to start clean | `ucode revert` (restores backed-up agent configs and clears state). |

`ucode setup` and `ucode doctor --fix` are safe to re-run — they're idempotent
and won't create duplicate config entries.

---

## 10. Uninstall

```powershell
ucode revert                 # restore original agent config files, clear state
uv tool uninstall ucode      # remove the ucode binary
```
