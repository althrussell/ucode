"""`ucode doctor` — validate every dependency, auth, and the credential helper.

Read-only by default; ``--fix`` auto-installs/upgrades what it can. Checks run
in dependency order and a failed prerequisite *skips* (never crashes) its
dependents — e.g. if Databricks auth fails, the gateway/model/smoke checks
report "skipped" rather than throwing. ``run_doctor`` never raises; every check
is wrapped. Exit code is non-zero when any critical check fails.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from ucode.agents import TOOL_SPECS, install_tool_binary, normalize_tool
from ucode.databricks import (
    MIN_DATABRICKS_CLI_VERSION,
    MIN_NODE_MAJOR,
    _installed_databricks_cli_version,
    _node_major_version,
    ensure_ai_gateway_v2,
    ensure_databricks_cli_latest,
    ensure_node_npm,
    find_host_for_profile,
    find_profile_name_for_host,
    get_databricks_token,
    has_valid_databricks_auth,
    latest_databricks_cli_version,
    normalize_workspace_url,
    resolve_ucode_invocation,
)
from ucode.state import load_state
from ucode.telemetry import agent_version
from ucode.ui import console, print_section, status_badge

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_BADGE = {
    PASS: ("PASS", "ok"),
    WARN: ("WARN", "warn"),
    FAIL: ("FAIL", "error"),
    SKIP: ("SKIP", "info"),
}


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    critical: bool = False


class _Doctor:
    def __init__(self, *, fix: bool) -> None:
        self.fix = fix
        self.results: list[CheckResult] = []

    def record(
        self, name: str, status: str, detail: str = "", *, critical: bool = False
    ) -> CheckResult:
        result = CheckResult(name=name, status=status, detail=detail, critical=critical)
        self.results.append(result)
        return result

    def render(self) -> None:
        print_section("ucode doctor")
        width = max((len(r.name) for r in self.results), default=0)
        for r in self.results:
            text, kind = _BADGE[r.status]
            line = f"  {status_badge(text, kind)}  {r.name.ljust(width)}"
            if r.detail:
                line += f"  [dim]{r.detail}[/dim]"
            console.print(line)

    def exit_code(self) -> int:
        return 1 if any(r.status == FAIL and r.critical for r in self.results) else 0


def _check_python(doc: _Doctor) -> None:
    version = sys.version_info
    label = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= (3, 12):
        doc.record("Python >= 3.12", PASS, label)
    else:
        doc.record(
            "Python >= 3.12",
            FAIL,
            f"found {label}; reinstall ucode with `uv tool install --python 3.12 ...`",
            critical=True,
        )


def _check_uv(doc: _Doctor) -> None:
    if shutil.which("uv"):
        doc.record("uv present", PASS)
    else:
        doc.record("uv present", WARN, "install from https://astral.sh/uv (used to manage ucode)")


def _check_ucode_resolvable(doc: _Doctor) -> None:
    invocation = resolve_ucode_invocation()
    if shutil.which("ucode"):
        doc.record("ucode on PATH", PASS, invocation[0])
    elif len(invocation) == 1:
        # Resolved via uv's tool bin dir but not on PATH — the helper works
        # (it uses the absolute path) but interactive use needs a PATH fix.
        doc.record(
            "ucode on PATH",
            WARN,
            f"not on PATH; resolved to {invocation[0]}. Run `uv tool update-shell` and restart.",
        )
    else:
        doc.record(
            "ucode on PATH",
            WARN,
            "falling back to `python -m ucode`; run `uv tool update-shell` and restart.",
        )


def _check_node(doc: _Doctor, *, required: bool) -> None:
    have_npm = bool(shutil.which("npm"))
    node_major = _node_major_version()
    if have_npm and node_major is not None and node_major >= MIN_NODE_MAJOR:
        doc.record("Node.js / npm", PASS, f"node v{node_major}")
        return
    detail = (
        f"node too old (v{node_major})"
        if node_major is not None and node_major < MIN_NODE_MAJOR
        else "node/npm not found"
    )
    if doc.fix:
        try:
            ensure_node_npm()
            new_major = _node_major_version()
            if shutil.which("npm") and new_major is not None and new_major >= MIN_NODE_MAJOR:
                doc.record("Node.js / npm", PASS, f"installed node v{new_major}")
                return
            detail = "fix attempted but node/npm still unavailable"
        except RuntimeError as exc:
            detail = str(exc)
    doc.record("Node.js / npm", FAIL if required else WARN, detail, critical=required)


def _check_databricks_cli(doc: _Doctor) -> None:
    if not shutil.which("databricks"):
        detail = "not found"
        if doc.fix:
            try:
                from ucode.databricks import install_databricks_cli

                install_databricks_cli()
            except RuntimeError as exc:
                detail = str(exc)
        if not shutil.which("databricks"):
            doc.record("Databricks CLI", FAIL, detail, critical=True)
            return
    installed = _installed_databricks_cli_version()
    min_text = ".".join(str(n) for n in MIN_DATABRICKS_CLI_VERSION)
    if installed is None:
        doc.record("Databricks CLI", WARN, "version unreadable")
        return
    current = ".".join(str(n) for n in installed)
    if installed < MIN_DATABRICKS_CLI_VERSION:
        if doc.fix:
            try:
                ensure_databricks_cli_latest()
                installed = _installed_databricks_cli_version()
                current = ".".join(str(n) for n in installed) if installed else current
            except RuntimeError as exc:
                doc.record("Databricks CLI", FAIL, str(exc), critical=True)
                return
        if installed is None or installed < MIN_DATABRICKS_CLI_VERSION:
            doc.record(
                "Databricks CLI",
                FAIL,
                f"v{current} < required v{min_text}; run `ucode setup`",
                critical=True,
            )
            return
    latest = latest_databricks_cli_version()
    if latest is not None and installed < latest:
        target = ".".join(str(n) for n in latest)
        doc.record("Databricks CLI", WARN, f"v{current} (update available: v{target})")
    else:
        doc.record("Databricks CLI", PASS, f"v{current}")


def _check_agents(doc: _Doctor, agents: list[str]) -> None:
    state = load_state()
    managed_configs = state.get("managed_configs") or {}
    for tool in agents:
        spec = TOOL_SPECS[tool]
        name = f"Agent: {spec['display']}"
        if not shutil.which(spec["binary"]):
            detail = f"`{spec['binary']}` not installed"
            if doc.fix:
                try:
                    install_tool_binary(tool, strict=False, update_existing=True)
                except RuntimeError as exc:
                    detail = str(exc)
            if not shutil.which(spec["binary"]):
                doc.record(name, FAIL, detail, critical=True)
                continue
        ver = agent_version(spec["binary"])
        config_ok = tool in managed_configs and spec["config_path"].exists()
        if config_ok:
            doc.record(name, PASS, f"v{ver}")
        else:
            doc.record(
                name,
                WARN,
                f"v{ver}; managed config missing — run `ucode configure --agents {tool}`",
            )


def _check_auth_and_gateway(doc: _Doctor, workspace: str | None, profile: str | None) -> None:
    if not workspace:
        doc.record("Databricks auth", SKIP, "no workspace configured (pass --workspace/--profile)")
        doc.record("AI Gateway v2", SKIP, "auth not checked")
        doc.record("Models discovered", SKIP, "auth not checked")
        doc.record("Credential helper smoke test", SKIP, "auth not checked")
        return

    effective_profile = profile or find_profile_name_for_host(workspace)
    try:
        auth_ok = has_valid_databricks_auth(workspace, effective_profile)
    except Exception as exc:  # never let a check crash doctor
        auth_ok = False
        doc.record("Databricks auth", FAIL, f"{type(exc).__name__}: {exc}", critical=True)
    else:
        if auth_ok:
            doc.record("Databricks auth", PASS, workspace)
        else:
            doc.record(
                "Databricks auth",
                FAIL,
                f"run `databricks auth login --host {workspace}"
                + (f" --profile {effective_profile}`" if effective_profile else "`"),
                critical=True,
            )

    if not auth_ok:
        doc.record("AI Gateway v2", SKIP, "auth failed")
        doc.record("Models discovered", SKIP, "auth failed")
        doc.record("Credential helper smoke test", SKIP, "auth failed")
        return

    token = ""
    try:
        token = get_databricks_token(workspace, effective_profile)
    except RuntimeError as exc:
        doc.record("AI Gateway v2", FAIL, str(exc), critical=True)
    if not token:
        doc.record("AI Gateway v2", SKIP, "no token")
        doc.record("Models discovered", SKIP, "no token")
        _smoke_test_helper(doc, workspace, effective_profile)
        return

    gateway_ok = False
    try:
        ensure_ai_gateway_v2(workspace, token)
        gateway_ok = True
        doc.record("AI Gateway v2", PASS)
    except RuntimeError as exc:
        doc.record("AI Gateway v2", FAIL, str(exc), critical=True)

    if gateway_ok:
        _check_models(doc, workspace, token)
    else:
        doc.record("Models discovered", SKIP, "gateway unavailable")

    _smoke_test_helper(doc, workspace, effective_profile)


def _check_models(doc: _Doctor, workspace: str, token: str) -> None:
    from ucode.databricks import (
        discover_claude_models,
        discover_codex_models,
        discover_gemini_models,
    )

    found: list[str] = []
    try:
        claude, _ = discover_claude_models(workspace, token)
        if claude:
            found.append("claude")
        codex, _ = discover_codex_models(workspace, token)
        if codex:
            found.append("codex")
        gemini, _ = discover_gemini_models(workspace, token)
        if gemini:
            found.append("gemini")
    except Exception as exc:  # discovery should never crash doctor
        doc.record("Models discovered", FAIL, f"{type(exc).__name__}: {exc}", critical=True)
        return
    if found:
        doc.record("Models discovered", PASS, ", ".join(found))
    else:
        doc.record(
            "Models discovered",
            FAIL,
            "no Claude/Codex/Gemini endpoints on this workspace",
            critical=True,
        )


def _smoke_test_helper(doc: _Doctor, workspace: str, profile: str | None) -> None:
    """Run `ucode auth-token` end-to-end and assert a non-empty token.

    This is the check that catches a broken Windows credential-helper path: it
    exercises the exact invocation Claude/Codex bake into their configs.
    """
    argv = [*resolve_ucode_invocation(), "auth-token", "--host", workspace]
    if profile:
        argv += ["--profile", profile]
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        doc.record(
            "Credential helper smoke test",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            critical=True,
        )
        return
    if result.returncode == 0 and (result.stdout or "").strip():
        doc.record("Credential helper smoke test", PASS)
    else:
        detail = (result.stderr or "").strip().splitlines()
        doc.record(
            "Credential helper smoke test",
            FAIL,
            detail[-1] if detail else f"exit {result.returncode}, empty token",
            critical=True,
        )


def _resolve_workspace(workspace: str | None, profile: str | None) -> tuple[str | None, str | None]:
    if workspace:
        return normalize_workspace_url(workspace), profile
    if profile:
        return find_host_for_profile(profile), profile
    state = load_state()
    return state.get("workspace"), state.get("profile")


def run_doctor(
    *,
    profile: str | None = None,
    workspace: str | None = None,
    agents: list[str] | None = None,
    fix: bool = False,
) -> int:
    """Run all checks and return an exit code (0 = healthy, 1 = critical failure)."""
    doc = _Doctor(fix=fix)

    resolved_workspace, resolved_profile = _resolve_workspace(workspace, profile)

    if agents is None:
        state = load_state()
        agent_list = [t for t in (state.get("available_tools") or []) if t in TOOL_SPECS]
    else:
        agent_list = [normalize_tool(a) for a in agents]

    _check_python(doc)
    _check_uv(doc)
    _check_ucode_resolvable(doc)
    _check_databricks_cli(doc)
    _check_node(doc, required=bool(agent_list))
    if agent_list:
        _check_agents(doc, agent_list)
    _check_auth_and_gateway(doc, resolved_workspace, resolved_profile)

    doc.render()
    return doc.exit_code()
