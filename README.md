# Unity AI Gateway Coding CLI (ucode)

`ucode` is a lightweight launcher for running Codex, Claude Code, Gemini CLI, OpenCode, GitHub Copilot CLI, and Pi through Databricks.

## Requirements

Nothing to install by hand. The one-line installer below provisions everything
(`uv`, Python 3.12, the latest Databricks CLI, Node.js/npm, and the agent CLIs)
and repairs dirty machines (old Python, outdated Databricks CLI, missing/old
Node). `jq` is **not** required.

You only need to know one thing about yourself: **your Databricks workspace
URL**. It is the address in your browser's bar when you are logged into
Databricks, for example `https://my-company.cloud.databricks.com` (AWS),
`https://adb-1234567890.12.azuredatabricks.net` (Azure), or
`https://1234567890.gcp.databricks.com` (GCP). Copy everything up to and
including `databricks.com`/`databricks.net` — no trailing path.

## Quick start (no experience needed)

Follow these three steps exactly. Copy each block as-is.

### Step 1 — Install

Open your terminal and paste the one line for your operating system.

**macOS / Linux** (paste into Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/althrussell/ucode/main/install.sh | sh
```

**Windows** (paste into PowerShell — works whether you are in PowerShell or the
classic Command Prompt):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/althrussell/ucode/main/install.ps1 | iex"
```

The installer takes a few minutes the first time. It installs every dependency,
then configures and validates your agents. It is safe to re-run any time.

### Step 2 — Open a NEW terminal window

The installer adds `ucode` to your PATH, but your **current** terminal won't see
it yet. **Close this terminal and open a brand-new one** before continuing.
(On macOS: quit and reopen Terminal. On Windows: open a new PowerShell window.)

To confirm `ucode` is ready, paste:

```bash
ucode --help
```

If you see a list of commands, you're set. If you instead see
`command not found` (macOS/Linux) or
`'ucode' is not recognized` (Windows), see
[Troubleshooting](#troubleshooting-ucode-not-found) below.

### Step 3 — Launch an agent

Pick any one of these and paste it. The first time, `ucode` will ask for your
workspace URL (from the Requirements section above), open your browser to log
in, and configure everything for you.

```bash
ucode claude
```

```bash
ucode codex
```

That's it. After the first run, the same command goes straight to the agent.

For step-by-step, platform-specific instructions (dirty-machine repair, PATH,
profiles, at-scale workshop rollout, troubleshooting), see the deployment guides:

- [Windows deployment guide](docs/deployment-windows.md)
- [macOS deployment guide](docs/deployment-macos.md)

### Troubleshooting: `ucode` not found

If a new terminal still can't find `ucode`, paste this (macOS / Linux) and then
open another new terminal:

```bash
uv tool update-shell
```

On Windows (PowerShell), paste:

```powershell
uv tool update-shell
```

Already have `uv` and prefer to do it yourself? Install just the package and run
setup:

```bash
uv tool install git+https://github.com/althrussell/ucode
ucode setup
```

---

## Usage

Run the tool you want. Each of these is ready to paste:

```bash
ucode codex
```

```bash
ucode claude
```

```bash
ucode gemini
```

```bash
ucode opencode
```

```bash
ucode copilot
```

```bash
ucode pi
```

(`codex` = OpenAI Codex, `claude` = Claude Code, `gemini` = Gemini CLI,
`opencode` = OpenCode, `copilot` = GitHub Copilot CLI, `pi` = Pi.)

On first launch, `ucode` prompts for your Databricks workspace URL,
authenticates, and configures that tool automatically. Later launches go
straight to the agent.

Pass flags directly to the underlying tool — for example, resume Claude's last
session:

```bash
ucode claude -r
```

Or run Codex fully automated:

```bash
ucode codex --full-auto
```

All agents route through Databricks AI Gateway using your workspace credentials — no API keys required.

To configure all tools at once:

```bash
ucode configure
```

To configure specific tools without the picker, pass a comma-separated list
(use any of `codex`, `claude`, `gemini`, `opencode`, `copilot`, `pi`):

```bash
ucode configure --agents claude,codex
```

To configure without the workspace picker, paste the command below **after
replacing the example URL with your own workspace URL** (comma-separate multiple
workspaces, no spaces):

```bash
ucode configure --workspaces https://my-company.cloud.databricks.com
```

When multiple workspaces are provided, `ucode` logs into and saves state for each workspace. Launch commands such as `ucode codex` use the first workspace in the list.

### Selecting a Databricks profile

A "profile" is a named login saved in your `~/.databrickscfg` file (created by
`databricks auth login`). If you have one and want to use it, pass `--profile`
followed by its name. The examples below use a profile literally named
`workshop` — replace `workshop` with your own profile name:

```bash
ucode claude --profile workshop
```

```bash
ucode configure --agents claude --profile workshop
```

`--profile` resolves the workspace the profile points at, so you don't re-type
the URL. An unknown profile fails fast with the exact `databricks auth login`
command to create it.

**Duplicate hosts are supported.** If the same workspace host has more than one
profile (for example a `DEFAULT` profile and a named one), `ucode` no longer
fails to disambiguate — the workspace picker lists each profile separately, and
`--profile` always wins from the command line.

### Workshop-scale rollout (one command)

`ucode setup` is fully non-interactive when you supply everything, which makes it
the single command to script across many machines. Replace the example profile
name (`workshop`) and the example workspace URL with your own:

```bash
ucode setup --agents claude,codex --profile workshop --workspaces https://my-company.cloud.databricks.com
```

It installs/upgrades all dependencies, configures the requested agents, and runs
a `ucode doctor` validation pass. The bootstrap one-liner also reads
`UCODE_AGENTS`, `UCODE_PROFILE`, and `UCODE_WORKSPACES` from the environment for
MDM/Intune (Windows) and Jamf/script (macOS) deployment.

### Validating an install

```bash
ucode doctor            # read-only health check
ucode doctor --fix      # auto-install/upgrade anything fixable
```

`ucode doctor` checks Python, `uv`, Node.js/npm, the Databricks CLI, that
`ucode` is resolvable for the credential helper, each agent binary, Databricks
auth, AI Gateway v2 reachability, and runs an end-to-end credential-helper smoke
test. Each check reports pass / warn / skip / fail with remediation.

### MCP servers (optional)

```bash
ucode configure mcp
```

Add Databricks MCP servers to installed MCP-capable tools: Codex, Claude Code, Gemini CLI, OpenCode, and GitHub Copilot CLI.
Options are shown in this order:

- Discovered external MCP connections
- Databricks SQL
- Managed Databricks MCPs (Vector Search, UC Functions, etc.)
- Custom MCP server URL

Discovered external MCP connections are listed directly. MCP auth uses a Databricks token that
`ucode` sets when launching each tool.

---

## Other Commands

| Command | Description |
|---------|-------------|
| `ucode setup` | Zero-manual provisioning: update all dependencies, then configure + validate (idempotent) |
| `ucode doctor` | Validate dependencies, auth, gateway, agents, and the credential helper (`--fix` to repair) |
| `ucode status` | Show current workspace, base URLs, managed config files, and selected models |
| `ucode usage` | Show AI Gateway usage summary |
| `ucode auth-token` | Print a Databricks access token to stdout (the cross-platform credential helper agents call) |
| `ucode revert` | Clear saved state and restore backed-up config files |
| `ucode configure --dry-run` | Preview config files without writing them |
| `ucode configure --agents claude,codex` | Configure specific agents without the interactive picker |
| `ucode claude --profile NAME` | Launch an agent against a specific Databricks CLI profile (replace `NAME`) |
| `ucode configure --workspaces https://my-company.cloud.databricks.com` | Configure workspaces without the interactive picker (replace with your URL) |

## Managed Local Files

`ucode` manages these files:

| File | Tool |
|------|------|
| `~/.codex/config.toml` | Codex |
| `~/.claude/settings.json` | Claude Code |
| `~/.gemini/.env` | Gemini CLI |
| `~/.config/opencode/opencode.json` | OpenCode |
| `~/.copilot/.env` | GitHub Copilot CLI |
| `~/.pi/agent/models.json` | Pi |

Existing files are backed up before being overwritten. `ucode revert` restores backups.


## Documentation

- [Databricks AI Gateway overview](https://docs.databricks.com/aws/en/ai-gateway/overview-beta)
- [Databricks AI Gateway coding agent integration](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-integration-beta)
- [Databricks CLI authentication](https://docs.databricks.com/aws/en/dev-tools/cli/authentication)
- [Monitor AI Gateway usage](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints#track-usage-of-an-endpoint)

## Contributing

Contributions are welcome.

### Getting started

```bash
git clone https://github.com/althrussell/ucode
cd ucode
uv sync
```

### Development workflow

1. Create a feature branch off `main`.
2. Make your changes — keep them scoped to the requested behavior.
3. Run the test suite before pushing:

   ```bash
   uv run pytest          # unit tests
   uv run ruff check .    # lint
   ```

4. For end-to-end testing against a real workspace:

   ```bash
   UCODE_TEST_WORKSPACE=<db_workspace_url> uv run pytest tests/test_e2e.py -v
   ```

5. Open a pull request against `main`.

### Adding a new agent

- Add `src/ucode/agents/<name>.py` with at least `write_tool_config`, `launch`, `default_model`, and `validate_cmd`.
- Register it in `src/ucode/agents/__init__.py`.
- Add focused tests under `tests/`.

## Security

Please report security vulnerabilities to security@databricks.com rather than opening a public issue.

## License

See [LICENSE.md](./LICENSE.md) and [NOTICE.md](./NOTICE.md).
