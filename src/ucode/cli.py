#!/usr/bin/env python3
"""CLI entry point for ucode."""

from __future__ import annotations

import sys
from typing import Annotated, cast

import typer
from rich.panel import Panel

from ucode.agents import (
    TOOL_SPECS,
    check_gateway_endpoint,
    configure_selected_tools,
    configure_single_tool,
    configure_tool,
    ensure_bootstrap_dependencies,
    ensure_provider_state,
    install_tool_binary,
    normalize_tool,
    resolve_launch_model,
    validate_all_tools,
    validate_tool,
)
from ucode.agents import (
    launch as launch_agent,
)
from ucode.agents.pi import PI_SETTINGS_BACKUP_PATH, PI_SETTINGS_PATH
from ucode.config_io import restore_file, set_dry_run
from ucode.databricks import (
    build_shared_base_urls,
    discover_claude_models,
    discover_codex_models,
    discover_gemini_models,
    ensure_ai_gateway_v2,
    ensure_databricks_auth,
    ensure_databricks_cli_latest,
    ensure_node_npm,
    find_host_for_profile,
    find_profile_name_for_host,
    get_databricks_token,
    install_databricks_cli,
    list_all_databricks_profiles,
    normalize_workspace_url,
    run_databricks_login,
)
from ucode.mcp import (
    MCP_CLIENTS,
    configure_mcp_command,
    purge_cross_workspace_mcp_residue,
    revert_mcp_configs,
)
from ucode.state import (
    STATE_PATH,
    clear_state,
    load_full_state,
    load_state,
    save_state,
    set_current_workspace,
)
from ucode.tracing import configure_tracing_command
from ucode.ui import (
    console,
    heading,
    print_err,
    print_heading,
    print_kv,
    print_note,
    print_section,
    print_success,
    prompt_for_tools,
    prompt_for_workspace,
    set_verbosity,
    spinner,
    status_badge,
)
from ucode.usage import usage as usage_report

_DISCOVERY_CONSUMERS: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "opencode", "copilot", "pi"),
    "codex": ("codex", "copilot", "pi"),
    "gemini": ("gemini", "opencode", "pi"),
}


def _print_discovery_diagnostics(state: dict) -> None:
    """Surface per-source reasons after a failed discovery so the user knows
    which API call returned what — instead of the generic 'no agents' line."""
    reasons = state.get("_discovery_reasons") or {}
    if not reasons:
        return
    labels = {"claude": "Claude models", "codex": "Codex models", "gemini": "Gemini models"}
    for source, reason in reasons.items():
        consumers = ", ".join(_DISCOVERY_CONSUMERS.get(source, ()))
        label = labels.get(source, source)
        if reason:
            print_note(f"{label} (needed for: {consumers}): {reason}")
        else:
            print_note(f"{label} (needed for: {consumers}): no models returned")
    print_note("Re-run with `UCODE_DEBUG=1` to log raw discovery responses to ~/.ucode/debug.log.")


def _prompt_for_configuration(tool: str | None = None) -> tuple[str, str | None]:
    if not sys.stdin.isatty():
        # Happens with `curl ... | sh` (stdin is the piped script) or any
        # unattended run. Fail with the exact non-interactive incantation
        # instead of the bare "Input is not a terminal / Aborted." from the
        # picker.
        raise RuntimeError(
            "ucode needs a Databricks workspace, but this run is not interactive "
            "(no terminal on stdin).\n"
            "Re-run inside a terminal, or supply the workspace non-interactively:\n"
            "  ucode setup --workspaces https://your-workspace.cloud.databricks.com\n"
            "  ucode setup --profile YOUR_PROFILE\n"
            "For unattended/workshop installs, set UCODE_WORKSPACES (and optionally "
            "UCODE_PROFILE) before running the installer."
        )
    if tool is None:
        desc = "Configure your Databricks workspace"
    else:
        desc = f"Configure {TOOL_SPECS[tool]['display']} to use your Databricks endpoint."
    with spinner("Loading Databricks workspaces and profiles..."):
        # Pass every profile (including duplicate hosts and PAT profiles) so the
        # picker can offer each one and the user can disambiguate a host that has
        # both a DEFAULT and a named profile.
        profiles = [(host, name) for host, name, _auth_type in list_all_databricks_profiles()]
    return prompt_for_workspace(desc, profiles)


def _resolve_workspace_entries(
    workspaces: str | None, profile: str | None
) -> list[tuple[str, str | None]] | None:
    """Resolve `--workspaces`/`--profile` into [(url, profile), ...] or None.

    - ``--workspaces`` present: parse the URLs; when ``--profile`` is also given,
      attach it to every entry.
    - only ``--profile`` present: resolve the host from the named profile (fast
      fail with remediation when the profile is unknown).
    - neither: return None so the caller prompts interactively.
    """
    if workspaces is not None:
        entries = _parse_workspaces_option(workspaces)
        if profile:
            entries = cast(
                "list[tuple[str, str | None]]",
                [(workspace, profile) for workspace, _ in entries],
            )
        return entries
    if profile:
        host = find_host_for_profile(profile)
        if not host:
            raise RuntimeError(_unknown_profile_message(profile))
        return [(host, profile)]
    return None


def _unknown_profile_message(profile: str) -> str:
    return (
        f"Databricks profile '{profile}' was not found in ~/.databrickscfg.\n"
        f"Create it with `databricks auth login --profile {profile}` "
        f"or run `ucode setup --profile {profile} --workspaces <workspace-url>`."
    )


def _parse_agents_option(agents: str) -> list[str]:
    tools: list[str] = []
    for raw_tool in agents.split(","):
        raw_tool = raw_tool.strip()
        if not raw_tool:
            continue
        tool = normalize_tool(raw_tool)
        if tool not in tools:
            tools.append(tool)
    if not tools:
        raise RuntimeError(
            "No agents provided for --agents. Use a comma-separated list like `--agents claude,codex`."
        )
    return tools


def _parse_workspaces_option(workspaces: str) -> list[tuple[str, str | None]]:
    """Parse `--workspaces` into [(url, profile_name | None), ...].

    `--workspaces` supplies bare URLs; the matching profile (if any) is
    resolved later via `find_profile_name_for_host`.
    """
    workspace_entries: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for raw_workspace in workspaces.split(","):
        raw_workspace = raw_workspace.strip()
        if not raw_workspace:
            continue
        try:
            workspace = normalize_workspace_url(raw_workspace)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if workspace not in seen:
            seen.add(workspace)
            workspace_entries.append((workspace, None))
    if not workspace_entries:
        raise RuntimeError(
            "No workspaces provided for --workspaces. Use a comma-separated list like "
            "`--workspaces https://workspace.databricks.com`."
        )
    return workspace_entries


def configure_shared_state(
    workspace: str,
    profile: str | None = None,
    tools: list[str] | None = None,
    force_login: bool = False,
) -> dict:
    """Log into Databricks, enforce AI Gateway v2, fetch model lists, persist state.

    If tools is provided, only fetch models for those tools. Otherwise fetch all.
    If force_login is True, always run databricks auth login (used by explicit configure).
    ``profile`` is the Databricks CLI profile name to address — passed via
    ``--profile`` to every CLI invocation so ambiguous `~/.databrickscfg`
    entries (e.g. DEFAULT and a named profile both pointing at the same host)
    don't error out. If ``None``, we resolve it from the host after login.
    """
    workspace = normalize_workspace_url(workspace)
    previous_workspace = load_state().get("workspace")
    fetch_all = tools is None
    if force_login:
        run_databricks_login(workspace, profile)
    else:
        ensure_databricks_auth(workspace, profile)
    # After login the profile exists in ~/.databrickscfg, so a host->profile
    # lookup is reliable. Persist it so subsequent CLI calls disambiguate.
    if profile is None:
        profile = find_profile_name_for_host(workspace)
    with spinner("Verifying Unity AI Gateway..."):
        token = get_databricks_token(workspace, profile)
        ensure_ai_gateway_v2(workspace, token)
    print_success("Unity AI Gateway detected")

    want_claude = (
        fetch_all or "claude" in tools or "opencode" in tools or "copilot" in tools or "pi" in tools
    )
    want_gemini = fetch_all or "gemini" in tools or "opencode" in tools or "pi" in tools
    want_codex = fetch_all or "codex" in tools or "copilot" in tools or "pi" in tools

    claude_reason: str | None = None
    gemini_reason: str | None = None
    codex_reason: str | None = None
    with spinner("Fetching available models..."):
        if want_claude:
            claude_models, claude_reason = discover_claude_models(workspace, token)
        else:
            claude_models = {}
        if want_gemini:
            gemini_models, gemini_reason = discover_gemini_models(workspace, token)
        else:
            gemini_models = []
        if want_codex:
            codex_models, codex_reason = discover_codex_models(workspace, token)
        else:
            codex_models = []
    opencode_models: dict[str, list[str]] = {}
    if claude_models:
        opencode_models["anthropic"] = list(claude_models.values())
    if gemini_models:
        opencode_models["gemini"] = gemini_models

    # Merge into existing workspace state so prior tool configs are preserved.
    state = load_state()
    state["workspace"] = workspace
    if profile:
        state["profile"] = profile
    else:
        state.pop("profile", None)
    state["base_urls"] = build_shared_base_urls(workspace)
    if want_claude:
        state["claude_models"] = claude_models
    if want_gemini:
        state["gemini_models"] = gemini_models
    if want_codex:
        state["codex_models"] = codex_models
    if fetch_all or "opencode" in tools:
        state["opencode_models"] = opencode_models
    save_state(state)
    # Scrub MCP entries that ucode wrote for the previous workspace so the new
    # workspace's agent configs aren't stale.
    if previous_workspace and previous_workspace != workspace:
        purge_cross_workspace_mcp_residue(state, workspace)
    # Diagnostic reasons are transient — attach after save_state so they don't
    # land on disk but are available to the caller for this run.
    state["_discovery_reasons"] = {
        "claude": claude_reason,
        "gemini": gemini_reason,
        "codex": codex_reason,
    }
    return state


def _configure_shared_workspace_states(
    workspaces: list[tuple[str, str | None]],
    tools: list[str] | None,
    *,
    force_login: bool,
) -> list[dict]:
    if not workspaces:
        raise RuntimeError("At least one workspace must be provided.")
    states: list[dict] = []
    for workspace, profile in workspaces:
        states.append(
            configure_shared_state(workspace, profile=profile, tools=tools, force_login=force_login)
        )
    return states


def configure_workspace_command(
    tool: str | None = None,
    selected_tools: list[str] | None = None,
    workspaces: list[tuple[str, str | None]] | None = None,
    *,
    prompt_optional_updates: bool = True,
) -> int:
    if tool is not None and selected_tools is not None:
        raise RuntimeError("Use either --agent or --agents, not both.")

    workspace_entries = workspaces or [_prompt_for_configuration(tool)]

    if tool is not None:
        states = _configure_shared_workspace_states(workspace_entries, [tool], force_login=True)
        state = states[0]
        state = configure_single_tool(tool, state)
        spec = TOOL_SPECS[tool]
        console.print(
            Panel(
                f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]\n"
                f"[bold]{spec['display']}:[/bold] [green]configured[/green]",
                title="Configuration Complete",
                style="green",
                expand=False,
            )
        )
        with spinner(f"Validating {spec['display']}..."):
            ok, err = validate_tool(tool)
        if ok:
            print_success(f"{spec['display']} is working")
        else:
            print_err(f"{spec['display']}: {err}")
            managed = bool(state.get("managed_configs", {}).get(tool))
            restore_file(spec["config_path"], spec["backup_path"], managed)
            available_tools = [t for t in (state.get("available_tools") or []) if t != tool]
            state["available_tools"] = available_tools
            save_state(state)
            raise RuntimeError(f"{spec['display']} validation failed — config reverted.")
        return 0

    states = _configure_shared_workspace_states(workspace_entries, selected_tools, force_login=True)
    state = states[0]
    save_state(state)

    available_on_workspace: list[str] = []
    tools_to_check = selected_tools or list(TOOL_SPECS)
    for tool_name in tools_to_check:
        with spinner(f"Checking {TOOL_SPECS[tool_name]['display']} availability..."):
            if check_gateway_endpoint(state, tool_name):
                available_on_workspace.append(tool_name)

    if not available_on_workspace:
        print_err("No coding agents are available on this workspace.")
        _print_discovery_diagnostics(state)
        return 1

    if selected_tools is None and not sys.stdin.isatty():
        # Non-interactive (e.g. unattended install with --workspaces but no
        # --agents): configure everything available rather than aborting at the
        # picker. Pass --agents to choose a subset.
        picked = available_on_workspace
        print_note(
            "Non-interactive run — configuring all available agents: "
            + ", ".join(TOOL_SPECS[t]["display"] for t in picked)
        )
    elif selected_tools is None:
        picked = prompt_for_tools([(t, TOOL_SPECS[t]["display"]) for t in available_on_workspace])
    else:
        unavailable_tools = [
            tool_name for tool_name in selected_tools if tool_name not in available_on_workspace
        ]
        if unavailable_tools:
            _print_discovery_diagnostics(state)
            displays = ", ".join(
                TOOL_SPECS[tool_name]["display"] for tool_name in unavailable_tools
            )
            raise RuntimeError(f"Requested agent(s) not available on this workspace: {displays}.")
        picked = selected_tools

    if not picked:
        print_note("No coding agents selected — nothing to configure.")
        return 0

    for tool_name in picked:
        install_tool_binary(
            tool_name,
            strict=False,
            update_existing=True,
            prompt_optional_updates=prompt_optional_updates,
        )

    state = configure_selected_tools(state, picked)

    summary_lines = [f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]"]
    for tool_name in picked:
        spec = TOOL_SPECS[tool_name]
        summary_lines.append(f"[bold]{spec['display']}:[/bold] [green]configured[/green]")
    console.print(
        Panel(
            "\n".join(summary_lines),
            title="Configuration Complete",
            style="green",
            expand=False,
        )
    )

    # Limit validation to just-configured tools so we don't re-validate
    # previously-configured tools the user didn't touch this run.
    validate_state = {**state, "available_tools": picked}
    validate_all_tools(validate_state)
    return 0


def status() -> int:
    state = load_state()
    workspace = state.get("workspace")
    managed_configs = state.get("managed_configs") or {}
    mcp_servers = state.get("mcp_servers") or []
    configured_tools = set(state.get("available_tools") or managed_configs.keys())

    console.print(heading("ucode status"))
    console.print(
        f"  {status_badge('Configured', 'ok') if workspace else status_badge('Not Configured', 'warn')}"
    )

    print_heading("Provider")
    print_kv("Workspace URL", workspace or "not configured")
    profile = state.get("profile")
    if profile:
        print_kv("CLI profile", profile)

    print_heading("Coding Agents")
    for tool, spec in TOOL_SPECS.items():
        configured = tool in configured_tools
        base_url = (
            state.get("base_urls", {}).get(tool, "not configured")
            if configured
            else "not configured"
        )
        config_path = spec["config_path"]
        print_kv("Coding Agent", spec["display"])
        print_kv("Configured", "yes" if configured else "no")
        print_kv("Base URL", base_url)
        if configured and tool in MCP_CLIENTS:
            tool_mcp_servers = [
                str(server.get("name"))
                for server in mcp_servers
                if tool in (server.get("clients") or []) and server.get("name")
            ]
            print_kv("MCP list command", str(MCP_CLIENTS[tool]["list_command"]))
            print_kv(
                "MCP servers",
                ", ".join(tool_mcp_servers) if tool_mcp_servers else "none saved by ucode",
            )
        print_kv("Config file", str(config_path) if config_path.exists() else "missing")
        console.print()

    print_heading("Tracing")
    tracing = state.get("tracing") or {}
    if tracing.get("enabled"):
        print_kv("MLflow tracing", "enabled")
        print_kv("Tracking URI", str(tracing.get("tracking_uri") or "unknown"))
        print_kv(
            "Experiment",
            f"{tracing.get('experiment_name')} (id {tracing.get('experiment_id')})",
        )
        uc_destination = tracing.get("uc_destination")
        if uc_destination:
            print_kv("Unity Catalog", str(uc_destination))
        sql_warehouse_id = tracing.get("sql_warehouse_id")
        if sql_warehouse_id:
            print_kv("SQL warehouse", str(sql_warehouse_id))
    else:
        print_kv("MLflow tracing", "disabled")

    print_heading("State")
    print_kv("State file", str(STATE_PATH) if STATE_PATH.exists() else "missing")
    print_note("Use `ucode configure` to update workspace settings or configure new tools.")
    print_note(
        "Use `ucode configure mcp` to add Databricks MCP servers to configured coding tools."
    )
    print_note("Use `ucode configure tracing` to log coding sessions to an MLflow experiment.")
    print_note("Use `ucode revert` to clear managed configs and restore prior files.")
    return 0


def revert() -> int:
    state = load_state()
    managed_configs = state.get("managed_configs") or {}
    mcp_results = revert_mcp_configs(state)

    results: dict[str, bool] = {
        tool: restore_file(
            spec["config_path"], spec["backup_path"], bool(managed_configs.get(tool))
        )
        for tool, spec in TOOL_SPECS.items()
    }
    pi_settings_restored = restore_file(
        PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH, bool(managed_configs.get("pi"))
    )
    clear_state()

    print_heading("Revert")
    print_kv("Workspace", state.get("workspace") or "none")
    for tool, spec in TOOL_SPECS.items():
        print_kv(f"{spec['display']} config", "restored" if results[tool] else "unchanged")
    print_kv("Pi settings", "restored" if pi_settings_restored else "unchanged")
    for client, spec in MCP_CLIENTS.items():
        print_kv(
            f"{spec['display']} MCP config",
            "restored" if mcp_results.get(client) else "unchanged",
        )
    print_success("ucode state cleared")
    return 0


# ---------------------------------------------------------------------------
# typer app
# ---------------------------------------------------------------------------


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
configure_app = typer.Typer(add_completion=False, no_args_is_help=False)
app.add_typer(configure_app, name="configure", help="Configure workspace and tool settings.")
mcp_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(mcp_app, name="mcp", help="MCP servers exposed by ucode.")


@mcp_app.command("web-search")
def mcp_web_search_cmd() -> None:
    """Run the web_search MCP server over stdio. Invoked as a subprocess by Claude Code."""
    from ucode.mcp_web_search import serve

    serve()


def _auto_configure_tool(
    tool: str, profile: str | None = None, workspace: str | None = None
) -> None:
    """First-time setup for a single tool — mirrors configure_workspace_command.

    ``profile``/``workspace`` override the saved state (used by `ucode <agent>
    --profile`); when neither the override nor saved state has a workspace we
    prompt interactively.
    """
    existing = load_state()
    workspace = workspace or existing.get("workspace")
    profile = profile or existing.get("profile")
    if not workspace:
        workspace, profile = _prompt_for_configuration(tool)
    state = configure_shared_state(workspace, profile=profile, tools=[tool])

    state = configure_single_tool(tool, state)

    spec = TOOL_SPECS[tool]
    console.print(
        Panel(
            f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]\n"
            f"[bold]{spec['display']}:[/bold] [green]configured[/green]",
            title="Configuration Complete",
            style="green",
            expand=False,
        )
    )

    with spinner(f"Validating {spec['display']}..."):
        ok, err = validate_tool(tool)
    if ok:
        print_success(f"{spec['display']} is working")
    else:
        print_err(f"{spec['display']}: {err}")
        managed = bool(state.get("managed_configs", {}).get(tool))
        restore_file(spec["config_path"], spec["backup_path"], managed)
        available_tools = [t for t in (state.get("available_tools") or []) if t != tool]
        state["available_tools"] = available_tools
        save_state(state)
        raise RuntimeError(f"{spec['display']} validation failed — config reverted.")


def _launch_tool(tool_name: str, ctx: typer.Context, profile: str | None = None) -> None:
    try:
        tool = normalize_tool(tool_name)
        override_workspace: str | None = None
        if profile:
            # Resolve the workspace the profile targets BEFORE deciding whether
            # auto-configure is needed. An unknown profile fails fast with
            # remediation rather than silently falling back to the default.
            override_workspace = find_host_for_profile(profile)
            if not override_workspace:
                raise RuntimeError(_unknown_profile_message(profile))
            # Point ucode at this workspace so the launch (and future plain
            # `ucode <agent>` runs) target it.
            set_current_workspace(override_workspace)
        existing = load_state()
        workspace = override_workspace or existing.get("workspace")
        available_tools = existing.get("available_tools") or []
        # A profile switch to a workspace where this tool isn't configured yet
        # must re-run configuration for that workspace.
        needs_auto_configure = (
            not workspace
            or (override_workspace is not None and existing.get("workspace") != override_workspace)
            or tool not in available_tools
        )
        ensure_bootstrap_dependencies(tool, update_existing=needs_auto_configure)
        if needs_auto_configure:
            _auto_configure_tool(tool, profile=profile, workspace=override_workspace)
        state = ensure_provider_state(tool)
        # Re-fetch model lists on every launch so newly-added Databricks
        # endpoints show up without a manual `ucode configure` (and so that
        # tools like pi which read multiple model bundles never run on
        # stale state from before a tool added a new bundle).
        launch_profile = profile or state.get("profile")
        state = configure_shared_state(state["workspace"], profile=launch_profile, tools=[tool])
        state, resolved_model = resolve_launch_model(tool, state, None)
        state = configure_tool(tool, state, resolved_model)
        print_section(f"ucode with {TOOL_SPECS[tool]['display']}")
        if resolved_model:
            print_kv("Model", resolved_model)
        if tool in ("gemini", "opencode", "copilot", "pi"):
            print_note(
                f"{TOOL_SPECS[tool]['display']} token refresh is managed automatically "
                f"every 30 minutes while the session is running."
            )
        print_success(f"Starting {TOOL_SPECS[tool]['display']}")
        launch_agent(tool, state, ctx.args)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


# Long-only `--profile`: never bind `-p`, which Claude/Codex use for prompts.
# With the app-level ignore_unknown_options/allow_extra_args, Click still parses
# this declared option and forwards the rest to the agent.
_ProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        help="Databricks CLI profile to target (resolves its workspace; "
        "disambiguates duplicate hosts).",
    ),
]


@app.command("codex", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def codex_cmd(ctx: typer.Context, profile: _ProfileOption = None) -> None:
    """Launch Codex via Databricks."""
    _launch_tool("codex", ctx, profile=profile)


@app.command("claude", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def claude_cmd(ctx: typer.Context, profile: _ProfileOption = None) -> None:
    """Launch Claude Code via Databricks."""
    _launch_tool("claude", ctx, profile=profile)


@app.command("gemini", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def gemini_cmd(ctx: typer.Context, profile: _ProfileOption = None) -> None:
    """Launch Gemini CLI via Databricks."""
    _launch_tool("gemini", ctx, profile=profile)


@app.command(
    "opencode", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def opencode_cmd(ctx: typer.Context, profile: _ProfileOption = None) -> None:
    """Launch OpenCode via Databricks."""
    _launch_tool("opencode", ctx, profile=profile)


@app.command("copilot", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def copilot_cmd(ctx: typer.Context, profile: _ProfileOption = None) -> None:
    """Launch GitHub Copilot CLI via Databricks."""
    _launch_tool("copilot", ctx, profile=profile)


@app.command("pi", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def pi_cmd(ctx: typer.Context, profile: _ProfileOption = None) -> None:
    """Launch Pi coding agent via Databricks."""
    _launch_tool("pi", ctx, profile=profile)


@app.command("auth-token")
def auth_token_cmd(
    host: Annotated[
        str | None,
        typer.Option("--host", "--workspace", help="Workspace URL (defaults to saved state)."),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Databricks CLI profile (defaults to saved state)."),
    ] = None,
) -> None:
    """Print a Databricks access token to stdout — the cross-platform credential
    helper invoked by Claude's apiKeyHelper and Codex's auth.command.

    stdout receives ONLY the bare token so a credential helper never feeds
    decoration to the agent; every diagnostic goes to stderr and any failure
    exits non-zero with empty stdout. Honors DATABRICKS_BEARER first (the CI
    short-circuit), otherwise force-refreshes via the Databricks CLI without
    ever opening a browser.
    """
    import os

    bearer = os.environ.get("DATABRICKS_BEARER", "").strip()
    if bearer:
        sys.stdout.write(bearer)
        sys.stdout.flush()
        return

    workspace = host
    effective_profile = profile
    if workspace is None or effective_profile is None:
        state = load_state()
        workspace = workspace or state.get("workspace")
        if effective_profile is None:
            effective_profile = state.get("profile")
    if not workspace:
        print(
            "auth-token: no --host provided and no workspace is configured. "
            "Pass --host or run `ucode configure`.",
            file=sys.stderr,
        )
        raise typer.Exit(1)
    try:
        token = get_databricks_token(workspace, effective_profile, force_refresh=True)
    except RuntimeError as exc:
        print(f"auth-token: {exc}", file=sys.stderr)
        raise typer.Exit(1) from None
    if not token:
        print("auth-token: Databricks returned an empty token.", file=sys.stderr)
        raise typer.Exit(1)
    sys.stdout.write(token)
    sys.stdout.flush()


@app.command("setup")
def setup_cmd(
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Databricks CLI profile to target/create."),
    ] = None,
    workspaces: Annotated[
        str | None,
        typer.Option("--workspaces", help="Comma-separated workspace URLs (no prompt when given)."),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option("--agents", help="Comma-separated agents to configure (e.g. claude,codex)."),
    ] = None,
    tracing: Annotated[
        bool, typer.Option("--tracing", help="Also enable MLflow tracing for the workspace(s).")
    ] = False,
    skip_upgrade: Annotated[
        bool,
        typer.Option(
            "--skip-upgrade",
            help="Skip dependency upgrades (still installs anything missing).",
        ),
    ] = False,
    skip_configure: Annotated[
        bool,
        typer.Option(
            "--skip-configure",
            help="Only install/upgrade dependencies; skip the workspace/agent "
            "configuration prompt. Used by the installer when run non-interactively.",
        ),
    ] = False,
    verbose: Annotated[
        str, typer.Option("--verbose", help="Output verbosity: 'normal' or 'low'.")
    ] = "normal",
) -> None:
    """Zero-manual, idempotent provisioning: bring every dependency up to date,
    then configure and validate the requested agents.

    Used by the bootstrap one-liner and safe to re-run any time. Fully
    non-interactive when --agents/--profile/--workspaces are supplied (the
    workshop one-command path); otherwise prompts only for what's missing.
    Pass --skip-configure to provision dependencies without touching the
    workspace configuration (the user finishes later with `ucode claude`).
    """
    if verbose not in ("normal", "low"):
        print_err("--verbose must be one of: normal, low.")
        raise typer.Exit(2)
    set_verbosity(verbose)
    try:
        rc = _run_setup(
            profile=profile,
            workspaces=workspaces,
            agents=agents,
            tracing=tracing,
            skip_upgrade=skip_upgrade,
            skip_configure=skip_configure,
        )
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None
    raise typer.Exit(rc)


def _run_setup(
    *,
    profile: str | None,
    workspaces: str | None,
    agents: str | None,
    tracing: bool,
    skip_upgrade: bool,
    skip_configure: bool = False,
) -> int:
    """Orchestrate dependency provisioning + configuration, aggregating failures.

    Continues past non-fatal step failures and returns non-zero only when a
    critical step (dependency or configuration) fails, so a partially-broken
    machine surfaces every problem in one pass instead of aborting on the first.
    """
    print_section("ucode setup")
    results: list[tuple[str, bool, str]] = []
    prompt_optional_updates = not skip_upgrade

    # 1. Databricks CLI -> latest (falls back to minimum check when offline).
    try:
        install_databricks_cli()
        if not skip_upgrade:
            ensure_databricks_cli_latest()
        results.append(("Databricks CLI", True, ""))
    except RuntimeError as exc:
        results.append(("Databricks CLI", False, str(exc)))

    # 2. Node.js / npm (required for the npm-distributed agent CLIs).
    try:
        ensure_node_npm()
        results.append(("Node.js / npm", True, ""))
    except RuntimeError as exc:
        results.append(("Node.js / npm", False, str(exc)))

    # 3. Configure + validate the requested agents.
    selected_tools: list[str] | None = None
    configured_ok = True
    # Only skip when nothing to configure non-interactively was supplied; an
    # explicit --workspaces/--profile/--agents still configures even with
    # --skip-configure (so workshop one-liners keep working).
    configure_skipped = skip_configure and not (workspaces or profile or agents)
    if configure_skipped:
        print_note(
            "Dependencies are ready. Skipping interactive configuration — "
            "finish by running `ucode claude` (or `ucode setup`) in your terminal."
        )
    else:
        try:
            selected_tools = _parse_agents_option(agents) if agents else None
            workspace_entries = _resolve_workspace_entries(workspaces, profile)
            if selected_tools is not None:
                configure_workspace_command(
                    selected_tools=selected_tools,
                    workspaces=workspace_entries,
                    prompt_optional_updates=prompt_optional_updates,
                )
            else:
                configure_workspace_command(
                    workspaces=workspace_entries,
                    prompt_optional_updates=prompt_optional_updates,
                )
            if tracing:
                tracing_workspaces = workspace_entries
                if tracing_workspaces is None:
                    current = load_full_state().get("current_workspace")
                    tracing_workspaces = [(current, profile)] if current else None
                if tracing_workspaces:
                    configure_tracing_command(workspaces=tracing_workspaces)
            results.append(("Configure agents", True, ""))
        except RuntimeError as exc:
            configured_ok = False
            results.append(("Configure agents", False, str(exc)))

    # 4. Validation pass via doctor (read-only).
    from ucode.doctor import run_doctor

    doctor_workspace = None
    if not workspaces and not profile:
        doctor_workspace = load_full_state().get("current_workspace")
    doctor_rc = run_doctor(
        profile=profile,
        workspace=doctor_workspace,
        agents=selected_tools,
        fix=False,
    )

    # 5. Summary.
    print_section("Setup summary")
    for name, ok, detail in results:
        if ok:
            print_success(name)
        else:
            print_err(f"{name}: {detail}")
    deps_ok = all(ok for _name, ok, _detail in results)
    if configure_skipped:
        # Dependency-only run (non-interactive installer): success means the
        # deps installed. The user configures their workspace next, in a
        # terminal, with `ucode claude`.
        if deps_ok:
            print_success(
                "Dependencies installed. Open a new terminal and run `ucode claude` "
                "to connect your Databricks workspace."
            )
            return 0
        print_note("Re-run the installer after addressing the issues above; it is safe to repeat.")
        return 1
    critical_ok = deps_ok and configured_ok
    if critical_ok and doctor_rc == 0:
        print_success("ucode is ready. Launch an agent with `ucode claude` (or codex/gemini/...).")
        return 0
    print_note("Re-run `ucode setup` after addressing the issues above; it is safe to repeat.")
    return 1


@configure_app.callback(invoke_without_command=True)
def configure(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print config files without writing them.")
    ] = False,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="Configure only the named agent (e.g. claude, codex, gemini, opencode, copilot, pi).",
        ),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option(
            "--agents",
            help="Configure a comma-separated list of agents without prompting (e.g. claude,codex).",
        ),
    ] = None,
    workspaces: Annotated[
        str | None,
        typer.Option(
            "--workspaces",
            help="Configure a comma-separated list of workspaces without prompting.",
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Databricks CLI profile to target/create. When --workspaces is omitted, "
            "the workspace is resolved from this profile.",
        ),
    ] = None,
    tracing: Annotated[
        bool,
        typer.Option(
            "--tracing",
            help="Also enable MLflow tracing for the configured workspace(s).",
        ),
    ] = False,
    skip_upgrade: Annotated[
        bool,
        typer.Option(
            "--skip-upgrade",
            help="Don't prompt to upgrade already-installed agent CLIs to a newer version. "
            "Required updates (when an agent is below its minimum supported version) are "
            "still applied.",
        ),
    ] = False,
    verbose: Annotated[
        str,
        typer.Option(
            "--verbose",
            help="Output verbosity: 'normal' (default) renders decorative panels; "
            "'low' prints terse single-line status instead.",
        ),
    ] = "normal",
) -> None:
    """Configure workspace URL and AI Gateway."""
    if ctx.invoked_subcommand is not None:
        return
    if verbose not in ("normal", "low"):
        print_err("--verbose must be one of: normal, low.")
        raise typer.Exit(2)
    set_dry_run(dry_run)
    set_verbosity(verbose)
    prompt_optional_updates = not skip_upgrade
    try:
        install_databricks_cli()
        if agent is not None and agents is not None:
            raise RuntimeError("Use either --agent or --agents, not both.")
        workspace_entries = _resolve_workspace_entries(workspaces, profile)
        if agent is not None:
            tool = normalize_tool(agent)
            install_tool_binary(
                tool,
                strict=True,
                update_existing=True,
                prompt_optional_updates=prompt_optional_updates,
            )
            if workspace_entries is None:
                configure_workspace_command(tool)
            else:
                configure_workspace_command(tool, workspaces=workspace_entries)
        elif agents is not None:
            selected_tools = _parse_agents_option(agents)
            if workspace_entries is None:
                configure_workspace_command(
                    selected_tools=selected_tools,
                    prompt_optional_updates=prompt_optional_updates,
                )
            else:
                configure_workspace_command(
                    selected_tools=selected_tools,
                    workspaces=workspace_entries,
                    prompt_optional_updates=prompt_optional_updates,
                )
        else:
            # Tool binaries are installed after the user picks which agents
            # they want, in configure_workspace_command.
            if workspace_entries is None:
                configure_workspace_command(prompt_optional_updates=prompt_optional_updates)
            else:
                configure_workspace_command(
                    workspaces=workspace_entries,
                    prompt_optional_updates=prompt_optional_updates,
                )
        if tracing:
            # The workspaces were just configured, so enable tracing for them
            # directly instead of re-prompting. Fall back to the workspace that
            # `configure_workspace_command` made current (the interactive pick).
            tracing_workspaces = workspace_entries
            if tracing_workspaces is None:
                current = load_full_state().get("current_workspace")
                tracing_workspaces = [(current, profile)] if current else None
            if tracing_workspaces:
                configure_tracing_command(workspaces=tracing_workspaces)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@configure_app.command("mcp")
def configure_mcp() -> None:
    """Add Databricks MCP servers to installed coding tools."""
    try:
        configure_mcp_command()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@configure_app.command("tracing")
def configure_tracing(
    disable: Annotated[
        bool, typer.Option("--disable", help="Turn off MLflow tracing for configured agents.")
    ] = False,
) -> None:
    """Send coding-session traces to an MLflow experiment in your workspace."""
    try:
        install_databricks_cli()
        configure_tracing_command(disable=disable)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@app.command("status")
def status_cmd() -> None:
    """Show current workspace, tool configs, and saved model selections."""
    try:
        status()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("revert")
def revert_cmd() -> None:
    """Clear ucode state and restore backed-up agent config files."""
    try:
        revert()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("doctor")
def doctor_cmd(
    profile: Annotated[
        str | None, typer.Option("--profile", help="Databricks CLI profile to validate.")
    ] = None,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", "--host", help="Workspace URL (defaults to saved state)."),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option("--agents", help="Comma-separated agents to validate (e.g. claude,codex)."),
    ] = None,
    fix: Annotated[
        bool, typer.Option("--fix", help="Auto-install/upgrade fixable problems.")
    ] = False,
) -> None:
    """Validate dependencies, Databricks auth, the AI Gateway, agents, and the
    cross-platform credential helper. Exits non-zero if any critical check fails."""
    from ucode.doctor import run_doctor

    try:
        agent_list = _parse_agents_option(agents) if agents else None
        rc = run_doctor(profile=profile, workspace=workspace, agents=agent_list, fix=fix)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None
    raise typer.Exit(rc)


@app.command("usage")
def usage_cmd() -> None:
    """Show Databricks AI Gateway usage summary (last 7 days)."""
    try:
        install_databricks_cli()
        usage_report()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("upgrade")
def upgrade_cmd() -> None:
    """Upgrade ucode to the latest version from GitHub."""
    import subprocess

    # Source tarball (not git+...) so upgrade works without a Git executable.
    source = "ucode @ https://github.com/althrussell/ucode/archive/main.tar.gz"
    print_section("Upgrade")
    print_kv("Source", source)
    try:
        subprocess.run(
            ["uv", "tool", "install", "--reinstall", source],
            check=True,
        )
    except FileNotFoundError:
        print_err("`uv` was not found on PATH. Install uv to upgrade ucode.")
        raise typer.Exit(1) from None
    except subprocess.CalledProcessError as exc:
        print_err(f"Upgrade failed (exit code {exc.returncode}).")
        raise typer.Exit(1) from None
    print_success("ucode upgraded")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
