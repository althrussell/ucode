# ucode deployment guide — macOS

A copy-paste-runnable guide for installing and rolling out `ucode` on macOS. You
do **not** need to install anything by hand first — the one-liner provisions the
entire toolchain and repairs dirty machines.

---

## 1. Overview & what gets installed

`ucode` launches Codex, Claude Code, Gemini CLI, OpenCode, GitHub Copilot CLI,
and Pi against a Databricks AI Gateway. The installer brings up the full
dependency chain, none of which you install manually:

| Dependency | Why | How it's provisioned |
|------------|-----|----------------------|
| `uv` | Installs/runs `ucode` and manages Python | Astral installer, or `uv self update` if present |
| Python 3.12 | `ucode` runtime | `uv python install 3.12` (never relies on system Python) |
| Databricks CLI (latest) | Auth + token refresh | GitHub release compare, upgraded if behind |
| Node.js / npm | Installs the npm-distributed agent CLIs | `brew install node` (or nodejs.org) |
| Agent CLIs | The agents you launch | `npm install -g` per agent |
| MLflow CLI (optional) | Only for Claude Code tracing | `uv tool install "ucode[tracing]"` path |

`jq` is **not** required (the credential helper is now `ucode auth-token`).

---

## 2. One-command install

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/althrussell/ucode/main/install.sh)"
```

This installs/updates `uv`, provisions Python 3.12, installs the latest `ucode`,
then runs `ucode setup` to update remaining dependencies and configure your
agents.

> Use the `sh -c "$(curl …)"` form shown above, **not** `curl … | sh`. The
> `sh -c` form keeps your terminal attached to stdin so the interactive
> workspace picker works. With `curl … | sh`, stdin is the piped script, so
> `ucode setup` runs non-interactively and will ask you to pass `--workspaces`
> (or set `UCODE_WORKSPACES`).

Notes:

- **Apple Silicon vs Intel:** identical command; `uv` and Homebrew pick the
  right architecture automatically.
- **Terminal / iTerm:** either works. After install, open a new tab/window so
  the updated PATH is picked up (see §4).
- **Gatekeeper / quarantine:** tools installed via `uv`, Homebrew, and `npm` are
  not quarantined, so no "unidentified developer" prompts.

---

## 3. Dirty-machine notes

The installer is idempotent and self-healing:

- **Old/system Python:** ignored — `uv` provisions its own Python 3.12.
- **Outdated Databricks CLI:** `ucode setup` compares against the latest GitHub
  release and upgrades in place (`brew upgrade databricks` or the official
  installer). Offline? It falls back to enforcing the minimum supported version.
- **Missing or old Node.js:** installed/upgraded via Homebrew (or nodejs.org if
  Homebrew is absent). A working-but-older Node is upgraded best-effort and never
  blocks the run.
- **Homebrew present/absent:** if present, it's used for `node`/`databricks`. If
  absent, the official installers are used; `ucode doctor` tells you if a manual
  step is needed.

Re-run `curl ... | sh` or `ucode setup` any time to repair.

---

## 4. PATH & shells

- `uv` installs tool binaries to `~/.local/bin`.
- The installer runs `uv tool update-shell`, which appends `~/.local/bin` to
  your shell profile (`~/.zshrc` for zsh, `~/.bash_profile` for bash). **Open a
  new terminal** (or `source ~/.zshrc`) to pick it up.
- The default macOS shell is **zsh**; bash works identically.
- Verify:

  ```bash
  which ucode databricks node
  ```

If `ucode` isn't found mid-session, the credential helper still works because
agent configs bake the absolute path — but run `uv tool update-shell` and
restart your terminal for interactive use.

---

## 5. Authentication & profiles

Log in to a workspace (creates/refreshes a named profile in `~/.databrickscfg`):

```bash
databricks auth login --host https://your-workspace.databricks.com --profile workshop
```

Launch an agent against that profile:

```bash
ucode claude --profile workshop
```

`--profile` resolves the workspace automatically. An unknown profile fails fast
with the exact `databricks auth login` command to create it.

**Duplicate-host example.** Suppose `~/.databrickscfg` has both:

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

```bash
ucode codex      # OpenAI Codex
ucode claude     # Claude Code
ucode gemini     # Gemini CLI
ucode opencode   # OpenCode
ucode copilot    # GitHub Copilot CLI
ucode pi         # Pi
```

Flags after the agent name pass straight through:

```bash
ucode claude -r                 # resume last session
ucode codex --full-auto
ucode claude --profile workshop -r
```

First launch of an agent auto-configures it (prompts for the workspace if none
is saved); later launches go straight to the agent.

---

## 7. Workshop at-scale rollout

The single non-interactive command to script across many machines:

```bash
ucode setup --agents claude,codex --profile workshop \
  --workspaces https://your-workspace.databricks.com
```

Or via the bootstrap one-liner with environment variables (handy for Jamf or a
deployment script):

```bash
UCODE_AGENTS="claude,codex" \
UCODE_PROFILE="workshop" \
UCODE_WORKSPACES="https://your-workspace.databricks.com" \
  sh -c "$(curl -fsSL https://raw.githubusercontent.com/althrussell/ucode/main/install.sh)"
```

Pre-seed profiles ahead of time by distributing a `~/.databrickscfg` or running
`databricks auth login --profile <name>` per machine. `ucode setup` is
idempotent, so a Jamf policy can run it on every check-in safely.

---

## 8. Verification

```bash
ucode doctor
```

Reads each check as **PASS / WARN / SKIP / FAIL**:

- **PASS** — healthy.
- **WARN** — works but suboptimal (e.g. an update is available, or `ucode` isn't
  on PATH yet).
- **SKIP** — a prerequisite wasn't met, so the dependent check didn't run (e.g.
  gateway checks skip when auth fails).
- **FAIL** — a problem; critical fails make `ucode doctor` exit non-zero.

Auto-repair what's fixable:

```bash
ucode doctor --fix
```

---

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ucode: command not found` after install | `uv tool update-shell`, then open a new terminal. |
| `databricks: command not found` | `ucode setup` (installs the latest CLI), or `brew install databricks`. |
| Corporate proxy / SSL inspection blocks downloads | Set `HTTPS_PROXY`/`HTTP_PROXY`; ensure the proxy CA is in the system keychain. |
| Node too old | `ucode doctor --fix` or `brew upgrade node`. |
| `npm` EACCES / permission errors | Ensure Homebrew owns `/opt/homebrew` (`sudo chown -R $(whoami) ...`) or reinstall Node via Homebrew. |
| GitHub rate-limit message during CLI update | Harmless — `ucode` falls back to the minimum-version check and continues. |
| Token / login failures | `databricks auth login --host <url> --profile <name>`, then `ucode doctor`. |
| Want to start clean | `ucode revert` (restores backed-up agent configs and clears state). |

`ucode setup` and `ucode doctor --fix` are safe to re-run — they're idempotent
and won't create duplicate config entries.

---

## 10. Uninstall

```bash
ucode revert                 # restore original agent config files, clear state
uv tool uninstall ucode      # remove the ucode binary
```
