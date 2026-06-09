"""Databricks workspace integration: CLI auth, token retrieval, model
discovery, AI Gateway v2 enforcement, SQL warehouse discovery, URL builders."""

from __future__ import annotations

import functools
import json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal, cast, overload
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

from databricks.sql.exc import ServerOperationError

from ucode.config_io import APP_DIR
from ucode.process import refresh_windows_path, windows_safe_args
from ucode.ui import (
    err_console,
    normalize_workspace_url,
    print_kv,
    print_note,
    print_section,
    print_success,
    print_warning,
    spinner,
)

UNIX_DATABRICKS_INSTALL_URL = (
    "https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh"
)
AI_GATEWAY_V2_DOCS_URL = "https://docs.databricks.com/aws/en/ai-gateway/overview-beta"
MIN_DATABRICKS_CLI_VERSION = (0, 298, 0)
TOKEN_REFRESH_INTERVAL_SECONDS = 1800
# GitHub releases API for the Databricks setup-cli, used to detect whether the
# installed CLI is behind the newest published release.
DATABRICKS_CLI_RELEASES_API = "https://api.github.com/repos/databricks/setup-cli/releases/latest"
# Node.js is required to install the npm-distributed agent CLIs. Agents target
# modern Node; 18 is the floor that keeps every supported agent CLI working.
MIN_NODE_MAJOR = 18
NODE_DOWNLOAD_URL = "https://nodejs.org/en/download"


def _debug_enabled() -> bool:
    return os.environ.get("UCODE_DEBUG") == "1"


_DEBUG_LOGGER: logging.Logger | None = None


def _get_debug_logger() -> logging.Logger | None:
    """Lazily configure a rotating file logger when UCODE_DEBUG=1.

    Returns the logger on first call (and caches it), or None if debug is
    disabled or the log file could not be opened. A one-time breadcrumb is
    printed to stderr so the user knows where to tail."""
    global _DEBUG_LOGGER
    if _DEBUG_LOGGER is not None or not _debug_enabled():
        return _DEBUG_LOGGER

    log_path = APP_DIR / "debug.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
        )
    except OSError:
        return None

    logger = logging.getLogger("ucode.debug")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    _DEBUG_LOGGER = logger
    err_console.print(f"[dim]\\[ucode debug] logging to {log_path}[/dim]")
    return _DEBUG_LOGGER


def _debug(label: str, detail: str) -> None:
    """When UCODE_DEBUG=1, append a timestamped entry to ~/.ucode/debug.log."""
    logger = _get_debug_logger()
    if logger is not None:
        logger.debug("%s: %s", label, detail)


_SECRET_KEY_PATTERN = re.compile(r"(token|secret|password|bearer|api_key|apikey)", re.IGNORECASE)


def _format_subprocess_result(
    result: subprocess.CompletedProcess[str],
) -> str:
    """Format a CompletedProcess for the debug log without leaking tokens.

    On success, stdout is suppressed (it often contains the access token).
    On failure, stdout/stderr are included truncated."""
    stderr = (result.stderr or "").strip()[:500]
    if result.returncode == 0:
        return f"rc=0 stderr={stderr!r}"
    stdout = (result.stdout or "").strip()[:500]
    return f"rc={result.returncode} stdout={stdout!r} stderr={stderr!r}"


def _scrub_databrickscfg(text: str) -> str:
    """Redact value of any INI key that looks secret-bearing."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if "=" in stripped and not stripped.startswith(("#", ";")):
            key = stripped.split("=", 1)[0].strip()
            if _SECRET_KEY_PATTERN.search(key):
                indent = line[: len(line) - len(stripped)]
                out.append(f"{indent}{key} = <redacted>")
                continue
        out.append(line)
    return "\n".join(out)


def _scrub_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: (
                "<redacted>"
                if isinstance(k, str) and _SECRET_KEY_PATTERN.search(k)
                else _scrub_json(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    return value


@functools.cache
def _log_auth_diagnostics() -> None:
    """Dump CLI version, profiles, and ~/.databrickscfg (scrubbed) to the debug log.

    No-op unless UCODE_DEBUG=1; cached so it runs at most once per process."""
    if not _debug_enabled():
        return

    try:
        version_result = subprocess.run(
            windows_safe_args(["databricks", "--version"]),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = (version_result.stdout or version_result.stderr or "").strip()
        _debug("databricks --version", version[:200])
    except (OSError, subprocess.TimeoutExpired) as exc:
        _debug("databricks --version", f"exception: {type(exc).__name__}: {exc}")

    try:
        profiles_result = subprocess.run(
            windows_safe_args(["databricks", "auth", "profiles", "--output", "json"]),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        _debug(
            "databricks auth profiles",
            f"rc={profiles_result.returncode} "
            f"stderr={(profiles_result.stderr or '').strip()[:300]!r}",
        )
        if profiles_result.returncode == 0 and profiles_result.stdout:
            try:
                payload = json.loads(profiles_result.stdout)
                _debug("profiles json", json.dumps(_scrub_json(payload))[:2000])
            except json.JSONDecodeError as exc:
                _debug("profiles json", f"decode error: {exc}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _debug("databricks auth profiles", f"exception: {type(exc).__name__}: {exc}")

    cfg_path = Path(os.environ.get("DATABRICKS_CONFIG_FILE") or "~/.databrickscfg").expanduser()
    try:
        if cfg_path.is_file():
            raw = cfg_path.read_text(encoding="utf-8", errors="replace")
            _debug(f"databrickscfg ({cfg_path})", _scrub_databrickscfg(raw)[:4000])
        else:
            _debug(f"databrickscfg ({cfg_path})", "not present")
    except OSError as exc:
        _debug(f"databrickscfg ({cfg_path})", f"read error: {exc}")


def _http_get_json(
    url: str, token: str, *, timeout: int = 10
) -> tuple[dict | list | None, str | None]:
    """GET a JSON endpoint. Returns (payload, None) on success, (None, reason) on failure.

    Honors UCODE_DEBUG=1 to append status + truncated body to ~/.ucode/debug.log.
    """
    request = urllib_request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        _debug(f"GET {url}", f"HTTP 200, {len(body)} bytes")
        if _debug_enabled():
            _debug("body", body[:4000])
        try:
            return json.loads(body), None
        except json.JSONDecodeError as exc:
            return None, f"response was not valid JSON ({exc.msg})"
    except urllib_error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        except Exception:
            body = ""
        _debug(f"GET {url}", f"HTTP {exc.code} {exc.reason}")
        if _debug_enabled() and body:
            _debug("body", body[:4000])
        reason = f"HTTP {exc.code} {exc.reason}"
        # Surface the response body too — gateway auth failures return 400
        # with body `Invalid Token`, which is invisible without this.
        body_excerpt = body.strip()[:200]
        if body_excerpt:
            reason = f"{reason}: {body_excerpt}"
        return None, reason
    except urllib_error.URLError as exc:
        _debug(f"GET {url}", f"URLError: {exc.reason}")
        return None, f"network error: {exc.reason}"


def _github_get_json(url: str, *, timeout: int = 5) -> tuple[dict | list | None, str | None]:
    """GET a public GitHub JSON endpoint without authentication.

    GitHub rejects requests lacking a ``User-Agent`` header, and its
    unauthenticated rate limit (60/hr) returns 403; both are treated as a
    soft failure (``(None, reason)``) so callers can fall back. Never raises.
    """
    request = urllib_request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ucode"},
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        try:
            return json.loads(body), None
        except json.JSONDecodeError as exc:
            return None, f"response was not valid JSON ({exc.msg})"
    except urllib_error.HTTPError as exc:
        _debug(f"GET {url}", f"HTTP {exc.code} {exc.reason}")
        return None, f"HTTP {exc.code} {exc.reason}"
    except urllib_error.URLError as exc:
        _debug(f"GET {url}", f"URLError: {exc.reason}")
        return None, f"network error: {exc.reason}"


def _http_post_json(
    url: str, token: str, payload: dict, *, timeout: int = 10
) -> tuple[dict | list | None, str | None]:
    """POST a JSON body to an endpoint. Returns (payload, None) on success,
    (None, reason) on failure. Mirrors `_http_get_json`."""
    body_bytes = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=body_bytes,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        _debug(f"POST {url}", f"HTTP {response.status}, {len(body)} bytes")
        if _debug_enabled():
            _debug("body", body[:4000])
        try:
            return json.loads(body), None
        except json.JSONDecodeError as exc:
            return None, f"response was not valid JSON ({exc.msg})"
    except urllib_error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        except Exception:
            body = ""
        _debug(f"POST {url}", f"HTTP {exc.code} {exc.reason}")
        if _debug_enabled() and body:
            _debug("body", body[:4000])
        reason = f"HTTP {exc.code} {exc.reason}"
        body_excerpt = body.strip()[:200]
        if body_excerpt:
            reason = f"{reason}: {body_excerpt}"
        return None, reason
    except urllib_error.URLError as exc:
        _debug(f"POST {url}", f"URLError: {exc.reason}")
        return None, f"network error: {exc.reason}"


def get_current_user_name(workspace: str, token: str) -> str | None:
    """Return the current user's login (email) via SCIM `Me`, or None on failure.

    Databricks puts the workspace login in `userName`; fall back to the first
    `emails` entry for workspaces that diverge."""
    hostname = workspace_hostname(workspace)
    payload, _ = _http_get_json(f"https://{hostname}/api/2.0/preview/scim/v2/Me", token)
    if not isinstance(payload, dict):
        return None
    user_name = payload.get("userName")
    if isinstance(user_name, str) and user_name.strip():
        return user_name.strip()
    emails = payload.get("emails")
    if isinstance(emails, list):
        for entry in emails:
            if isinstance(entry, dict) and isinstance(entry.get("value"), str):
                return entry["value"].strip()
    return None


# Experiment tag Databricks sets when an experiment's traces are written to a
# Unity Catalog table. Its value is the UC destination, e.g.
# "my_catalog.my_schema.my_table". A plain (file/DBFS-backed) experiment does
# not carry this tag, so its presence is our signal that traces land in UC.
UC_TRACE_DESTINATION_TAG = "mlflow.experiment.databricksTraceDestinationPath"


def _experiment_tags(experiment: dict) -> dict[str, str | None]:
    """Flatten an experiment's ``tags`` list ([{key, value}, ...]) into a dict."""
    out: dict[str, str | None] = {}
    tags = experiment.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and isinstance(tag.get("key"), str):
                out[tag["key"]] = tag.get("value")
    return out


def _uc_trace_destination(experiment: dict) -> str | None:
    """The Unity Catalog destination (``catalog.schema.table``) an experiment
    logs traces to, or None when it isn't UC-backed. Any three-part UC name
    qualifies — the specific catalog/schema/table is not constrained."""
    value = _experiment_tags(experiment).get(UC_TRACE_DESTINATION_TAG)
    if isinstance(value, str):
        parts = value.split(".")
        if len(parts) == 3 and all(parts):
            return value
    return None


def find_uc_backed_experiment(
    workspace: str, token: str, leaf_name: str
) -> tuple[dict | None, str | None]:
    """Find an existing experiment whose final path segment is ``leaf_name`` and
    whose traces are backed by Unity Catalog.

    Returns (experiment, reason). On success ``experiment`` is
    ``{"experiment_id", "experiment_name", "uc_destination"}`` and reason is
    None. On failure ``experiment`` is None and reason explains why (no such
    experiment, or it exists but isn't UC-backed) so the caller can tell the
    user to create one."""
    hostname = workspace_hostname(workspace)
    # Leaf-match in the filter (anything ending in the name), then confirm the
    # exact leaf segment in Python so "/Users/<me>/ucode-traces" matches but
    # "team-ucode-traces" does not.
    safe_leaf = leaf_name.replace("'", "")
    payload, reason = _http_post_json(
        f"https://{hostname}/api/2.0/mlflow/experiments/search",
        token,
        {"filter": f"name LIKE '%{safe_leaf}'", "max_results": 1000},
    )
    if not isinstance(payload, dict):
        return None, reason or "could not search MLflow experiments"

    experiments = payload.get("experiments")
    named = [
        exp
        for exp in (experiments if isinstance(experiments, list) else [])
        if isinstance(exp, dict)
        and str(exp.get("name") or "").rsplit("/", 1)[-1] == leaf_name
        and exp.get("experiment_id")
    ]
    if not named:
        return None, f"no experiment named '{leaf_name}' exists on this workspace"

    for exp in named:
        dest = _uc_trace_destination(exp)
        if dest:
            return {
                "experiment_id": str(exp["experiment_id"]),
                "experiment_name": str(exp.get("name") or leaf_name),
                "uc_destination": dest,
            }, None

    return (
        None,
        f"experiment '{leaf_name}' exists but its traces are not backed by Unity Catalog",
    )


def resolve_sql_warehouse_id(workspace: str, token: str) -> tuple[str | None, str | None]:
    """Pick a SQL warehouse for writing traces to a UC-backed experiment.

    Writing traces to a Unity Catalog table requires a SQL warehouse
    (``MLFLOW_TRACING_SQL_WAREHOUSE_ID``); without one the MLflow exporter
    silently drops them. We prefer a RUNNING warehouse so the first trace isn't
    blocked on a cold start, falling back to any existing warehouse (a stopped
    one auto-starts on first query). Returns (warehouse_id, reason); reason is
    None on success, else explains why none could be resolved."""
    hostname = workspace_hostname(workspace)
    payload, reason = _http_get_json(f"https://{hostname}/api/2.0/sql/warehouses", token)
    if not isinstance(payload, dict):
        return None, reason or "could not list SQL warehouses"

    warehouses = payload.get("warehouses")
    warehouses = (
        [w for w in warehouses if isinstance(w, dict) and w.get("id")]
        if isinstance(warehouses, list)
        else []
    )
    if not warehouses:
        return None, "no SQL warehouse exists on this workspace"

    running = next((w for w in warehouses if str(w.get("state")).upper() == "RUNNING"), None)
    chosen = running or warehouses[0]
    return str(chosen["id"]), None


@overload
def run(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: Literal[True],
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: Literal[False] = False,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[bytes]: ...


def run(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        windows_safe_args(args),
        check=check,
        capture_output=capture_output,
        text=text,
        env=env,
        timeout=timeout,
    )


def build_databricks_cli_env(workspace: str, profile: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["DATABRICKS_HOST"] = workspace
    if profile is None:
        env.pop("DATABRICKS_CONFIG_PROFILE", None)
    return env


def workspace_hostname(workspace: str) -> str:
    parsed = urlparse(normalize_workspace_url(workspace))
    if not parsed.hostname:
        raise RuntimeError(f"Unable to derive hostname from workspace URL: {workspace}")
    return parsed.hostname


def _parse_databricks_cli_version(output: str) -> tuple[int, int, int] | None:
    # Example output: "Databricks CLI v0.299.2"
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", output)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _run_databricks_cli_installer(brew_subcommand: str = "install") -> None:
    system = platform.system()
    try:
        if system == "Windows":
            _winget_install_databricks_cli(upgrade=brew_subcommand == "upgrade")
        elif system == "Darwin" and shutil.which("brew"):
            run(["brew", brew_subcommand, "databricks"], timeout=240)
        elif shutil.which("curl"):
            run(["sh", "-c", f"curl -fsSL {UNIX_DATABRICKS_INSTALL_URL} | sudo sh"], timeout=240)
        elif shutil.which("wget"):
            run(["sh", "-c", f"wget -qO- {UNIX_DATABRICKS_INSTALL_URL} | sudo sh"], timeout=240)
        else:
            raise RuntimeError("Neither curl nor wget is available.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
        raise RuntimeError("Failed to install/upgrade Databricks CLI automatically.") from exc


def _winget_install_databricks_cli(*, upgrade: bool) -> None:
    """Install/upgrade the Databricks CLI on Windows via winget.

    The Databricks setup-cli repo has no PowerShell install script (the old
    ``install.ps1`` URL 404s); winget is the official Windows path. We pin
    ``--source winget`` because the ``msstore`` source can fail certificate
    validation on locked-down/server images and otherwise makes winget prompt
    to disambiguate the source.
    """
    if not shutil.which("winget"):
        raise RuntimeError(
            "winget is required to install the Databricks CLI on Windows but was not found.\n"
            "Install 'App Installer' from the Microsoft Store (provides winget), then re-run, "
            "or install the CLI manually: https://docs.databricks.com/dev-tools/cli/install.html"
        )
    run(
        [
            "winget",
            "upgrade" if upgrade else "install",
            "-e",
            "--id",
            "Databricks.DatabricksCLI",
            "--source",
            "winget",
            "--silent",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ],
        timeout=600,
    )


def ensure_databricks_cli_version() -> None:
    try:
        result = run(
            ["databricks", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Failed to read Databricks CLI version.") from exc

    raw = result.stdout or result.stderr or ""
    output = (raw if isinstance(raw, str) else raw.decode(errors="replace")).strip()
    version = _parse_databricks_cli_version(output)
    if version is None:
        raise RuntimeError(
            f"Could not parse Databricks CLI version from `databricks --version` output: {output!r}"
        )
    if version < MIN_DATABRICKS_CLI_VERSION:
        current = ".".join(str(n) for n in version)
        required = ".".join(str(n) for n in MIN_DATABRICKS_CLI_VERSION)
        print_warning(
            f"Databricks CLI v{current} is too old (need v{required} or newer). Upgrading..."
        )
        _run_databricks_cli_installer(brew_subcommand="upgrade")
        ensure_databricks_cli_version()


def install_databricks_cli() -> None:
    # A prior winget/MSI install may have updated the persisted PATH without this
    # process seeing it; reload before deciding whether to (re)install.
    refresh_windows_path()
    if shutil.which("databricks"):
        ensure_databricks_cli_version()
        return

    print_section("Bootstrap")
    print_warning("`databricks` was not found. Installing Databricks CLI...")
    _run_databricks_cli_installer(brew_subcommand="install")
    # winget appends its Links dir to the persisted PATH; pull it in so the
    # just-installed `databricks` is resolvable without a terminal restart.
    refresh_windows_path()

    if not shutil.which("databricks"):
        raise RuntimeError(
            "Databricks CLI install completed, but `databricks` is still not on PATH.\n"
            "Open a NEW terminal and re-run `ucode setup` (Windows applies PATH "
            "changes to new shells only)."
        )
    # A fresh install should land on the newest release, not merely the minimum.
    ensure_databricks_cli_latest()


def _installed_databricks_cli_version() -> tuple[int, int, int] | None:
    """Return the installed Databricks CLI version tuple, or None if unreadable."""
    try:
        result = run(
            ["databricks", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = result.stdout or result.stderr or ""
    output = (raw if isinstance(raw, str) else raw.decode(errors="replace")).strip()
    return _parse_databricks_cli_version(output)


def latest_databricks_cli_version() -> tuple[int, int, int] | None:
    """Return the newest published Databricks CLI version, or None if unknown.

    Queries the GitHub releases API with a short timeout. Any failure — network
    error, GitHub's unauthenticated 60/hr rate limit (403/429), malformed JSON —
    returns None so callers can degrade gracefully to the minimum-version check.
    Never raises.
    """
    payload, reason = _github_get_json(DATABRICKS_CLI_RELEASES_API, timeout=5)
    if not isinstance(payload, dict):
        _debug("latest_databricks_cli_version", reason or "no payload")
        return None
    tag = payload.get("tag_name") or payload.get("name") or ""
    version = _parse_databricks_cli_version(str(tag))
    if version is None:
        _debug("latest_databricks_cli_version", f"unparseable tag: {tag!r}")
    return version


def ensure_databricks_cli_latest() -> None:
    """Upgrade the Databricks CLI to the newest release when behind.

    Falls back to :func:`ensure_databricks_cli_version` (minimum-version check)
    whenever the latest release cannot be determined — offline machines, GitHub
    rate limits, etc. — so provisioning is never blocked by a network hiccup.
    """
    latest = latest_databricks_cli_version()
    if latest is None:
        # Offline / rate-limited: enforce at least the minimum and move on.
        ensure_databricks_cli_version()
        return
    installed = _installed_databricks_cli_version()
    if installed is not None and installed >= latest:
        return
    current = ".".join(str(n) for n in installed) if installed else "unknown"
    target = ".".join(str(n) for n in latest)
    print_warning(f"Databricks CLI v{current} is behind latest v{target}. Upgrading...")
    try:
        _run_databricks_cli_installer(brew_subcommand="upgrade")
    except RuntimeError as exc:
        # A failed *upgrade* on a machine that already has a working CLI must not
        # abort provisioning (e.g. winget reporting no applicable upgrade). Only
        # the minimum-version guarantee is mandatory.
        if installed is not None:
            print_warning(f"Could not upgrade the Databricks CLI ({exc}); keeping v{current}.")
        else:
            raise
    # Guarantee the result still satisfies the minimum even if the upgrade was a
    # no-op for some reason.
    ensure_databricks_cli_version()


def _node_major_version() -> int | None:
    """Return the installed Node.js major version, or None when node is absent."""
    if not shutil.which("node"):
        return None
    try:
        result = run(
            ["node", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"v?(\d+)\.", result.stdout or result.stderr or "")
    return int(match.group(1)) if match else None


def _run_with_privilege(cmd: list[str], *, timeout: int) -> None:
    """Run ``cmd`` as root, escalating with sudo only when necessary.

    Used for apt/dnf on Linux. brew (no root) and winget (per-user) never reach
    here. Raises a RuntimeError with a copy-pasteable command when neither root
    nor sudo is available.
    """
    is_root = getattr(os, "geteuid", lambda: 1)() == 0
    if is_root:
        run(cmd, timeout=timeout)
        return
    if shutil.which("sudo"):
        run(["sudo", *cmd], timeout=timeout)
        return
    raise RuntimeError(
        "Root privileges are required to install Node.js. Re-run as root or run:\n"
        f"    sudo {' '.join(cmd)}"
    )


def _raise_node_remediation(system: str, cause: Exception | None = None) -> None:
    if system == "Windows":
        hint = f"Install Node.js from {NODE_DOWNLOAD_URL} or run `winget install OpenJS.NodeJS`."
    elif system == "Darwin":
        hint = f"Install Node.js with `brew install node` or from {NODE_DOWNLOAD_URL}."
    else:
        hint = (
            "Install Node.js + npm via your package manager "
            f"(e.g. `sudo apt-get install -y nodejs npm`) or from {NODE_DOWNLOAD_URL}."
        )
    message = f"Node.js/npm is required to install the agent CLIs. {hint}"
    if cause is not None:
        raise RuntimeError(message) from cause
    raise RuntimeError(message)


def ensure_node_npm() -> None:
    """Ensure ``node`` + ``npm`` are present and Node is recent enough.

    Installs or upgrades with no prompts using whatever manager is available
    (brew on macOS, winget on Windows, apt/dnf on Linux). Best-effort: if an
    upgrade fails but a working ``npm`` already exists, it warns and continues;
    it only raises (with platform-specific remediation) when no usable npm can
    be produced.
    """
    # Pick up any PATH changes a prior install persisted before probing.
    refresh_windows_path()
    have_npm = bool(shutil.which("npm"))
    node_major = _node_major_version()
    if have_npm and node_major is not None and node_major >= MIN_NODE_MAJOR:
        return

    system = platform.system()
    already_present = have_npm or node_major is not None
    try:
        if system == "Darwin" and shutil.which("brew"):
            run(["brew", "upgrade" if already_present else "install", "node"], timeout=900)
        elif system == "Windows" and shutil.which("winget"):
            # Pin --source winget: the msstore source can fail certificate
            # validation on locked-down images and otherwise makes winget prompt
            # to disambiguate the source (which aborts a non-interactive run).
            run(
                [
                    "winget",
                    "install",
                    "--silent",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                    "-e",
                    "--id",
                    "OpenJS.NodeJS",
                    "--source",
                    "winget",
                ],
                timeout=900,
            )
        elif shutil.which("apt-get"):
            _run_with_privilege(["apt-get", "install", "-y", "nodejs", "npm"], timeout=900)
        elif shutil.which("dnf"):
            _run_with_privilege(["dnf", "install", "-y", "nodejs"], timeout=900)
        else:
            _raise_node_remediation(system)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # An upgrade attempt that failed on a machine that already has a usable
        # npm should not abort provisioning.
        if shutil.which("npm"):
            print_warning(
                f"Could not upgrade Node.js ({type(exc).__name__}); "
                "continuing with the installed version."
            )
            return
        _raise_node_remediation(system, exc)

    # winget/MSI Node installs land on a PATH entry the running process can't see
    # yet; reload it so the npm-based agent installs that follow can find npm.
    refresh_windows_path()
    if not shutil.which("npm"):
        _raise_node_remediation(system)


def _profile_args(profile: str | None) -> list[str]:
    """Return ``["--profile", profile]`` when set, otherwise an empty list.

    Centralizing this keeps every `databricks` CLI invocation in this module
    consistent when a workspace's `~/.databrickscfg` has more than one profile
    pointing at the same host."""
    return ["--profile", profile] if profile else []


def has_valid_databricks_auth(workspace: str, profile: str | None = None) -> bool:
    # Honor the CI short-circuit (see ``get_databricks_token``): if a
    # pre-fetched bearer is available, treat auth as valid and skip the
    # `databricks auth token` shell-out (which only knows user-OAuth).
    if os.environ.get("DATABRICKS_BEARER", "").strip():
        return True
    _log_auth_diagnostics()
    # Mirror run_databricks_login: when ~/.databrickscfg has multiple
    # profiles for the same host, `databricks auth token --host …` refuses
    # to disambiguate without --profile, so resolve it from the host here.
    profile = profile or find_profile_name_for_host(workspace)
    try:
        env = build_databricks_cli_env(workspace, profile)
        result = run(
            [
                "databricks",
                "auth",
                "token",
                "--host",
                workspace,
                *_profile_args(profile),
                "--output",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        _debug(
            "has_valid_databricks_auth",
            _format_subprocess_result(result),
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout or "{}")
        return bool(data.get("access_token"))
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired) as exc:
        _debug("has_valid_databricks_auth", f"exception: {type(exc).__name__}: {exc}")
        return False


def list_all_databricks_profiles() -> list[tuple[str, str, str]]:
    """Return [(host_url, profile_name, auth_type), ...] for *every* profile.

    No host dedup and no PAT filtering — callers decide what to drop. This is
    the source of truth for duplicate-host handling: a workspace that has both a
    ``DEFAULT`` and a named profile pointing at the same host yields both
    entries here, so the picker can list each one and host->profile resolution
    can disambiguate ``databricks auth token --host``.

    Returns ``[]`` on any failure (CLI missing, timeout, non-zero exit, JSON
    decode error). When ``UCODE_DEBUG=1`` each dropout path logs *why* the
    result was empty so a silently-disappearing workspace picker is
    diagnosable from ``~/.ucode/debug.log``.
    """
    try:
        result = run(
            ["databricks", "auth", "profiles", "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _debug("list_all_databricks_profiles", f"subprocess error: {type(exc).__name__}: {exc}")
        return []
    if result.returncode != 0:
        _debug("list_all_databricks_profiles", _format_subprocess_result(result))
        return []
    try:
        profiles = json.loads(result.stdout or "{}").get("profiles") or []
    except json.JSONDecodeError as exc:
        _debug("list_all_databricks_profiles", f"json decode error: {exc.msg}")
        return []

    out: list[tuple[str, str, str]] = []
    for p in profiles:
        host = (p.get("host") or "").rstrip("/")
        name = p.get("name")
        if not host or not name:
            continue
        out.append((host, name, p.get("auth_type") or ""))

    _debug("list_all_databricks_profiles", f"returned={len(out)} total={len(profiles)}")
    return out


def get_databricks_profiles() -> list[tuple[str, str]]:
    """Return a deduped [(host_url, profile_name), ...] view of CLI profiles.

    Dedupes by host (first non-PAT profile wins) for callers that just want one
    entry per workspace. Use :func:`list_all_databricks_profiles` when duplicate
    hosts must be preserved (the workspace picker does).
    """
    out: dict[str, str] = {}
    for host, name, auth_type in list_all_databricks_profiles():
        if auth_type == "pat":
            continue
        out.setdefault(host, name)
    return list(out.items())


def find_profile_name_for_host(workspace: str) -> str | None:
    """Return a Databricks CLI profile name for a workspace host, deterministically.

    When a host has multiple profiles (e.g. ``DEFAULT`` plus a named profile),
    prefer an exact ``DEFAULT`` match, then the first non-PAT profile, then the
    first PAT profile. Only returns ``None`` when the host has no profile at all.
    Guaranteeing a name whenever any profile matches is what lets us always pass
    ``--profile`` and avoid the ``databricks auth token --host`` ambiguity error
    that previously crashed duplicate-host setups.
    """
    normalized = workspace.rstrip("/")
    matches = [
        (name, auth_type)
        for host, name, auth_type in list_all_databricks_profiles()
        if host == normalized
    ]
    if not matches:
        return None
    for name, _auth_type in matches:
        if name == "DEFAULT":
            return name
    for name, auth_type in matches:
        if auth_type != "pat":
            return name
    return matches[0][0]


def find_host_for_profile(profile: str) -> str | None:
    """Return the workspace host URL for a named Databricks CLI profile, or None.

    Inverse of :func:`find_profile_name_for_host`; lets ``--profile`` resolve the
    workspace it targets without the user re-typing the URL.
    """
    for host, name, _auth_type in list_all_databricks_profiles():
        if name == profile:
            return host
    return None


def run_databricks_login(workspace: str, profile: str | None = None) -> None:
    """Run databricks auth login unconditionally.

    When ``profile`` is provided, it is passed via ``--profile``. Otherwise we
    fall back to looking up an existing profile by host so a stored session is
    refreshed in place rather than overwriting another profile's tokens."""
    print_section("Databricks Login")
    print_kv("Workspace", workspace)
    print_note("A browser may open for `databricks auth login`.")
    try:
        profile_name = profile or find_profile_name_for_host(workspace)
        cmd = [
            "databricks",
            "auth",
            "login",
            "--host",
            workspace,
            *_profile_args(profile_name),
        ]
        run(cmd, env=build_databricks_cli_env(workspace, profile_name), timeout=300)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("`databricks auth login` failed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("`databricks auth login` timed out.") from exc
    print_success("Databricks authentication complete")


def ensure_databricks_auth(workspace: str, profile: str | None = None) -> None:
    """Check auth and login only if needed (used by launch path)."""
    with spinner("Checking Databricks auth..."):
        auth_is_valid = has_valid_databricks_auth(workspace, profile)
    if auth_is_valid:
        print_success(f"Databricks auth already available for {workspace}")
        return
    run_databricks_login(workspace, profile)


def get_databricks_token(
    workspace: str,
    profile: str | None = None,
    *,
    force_refresh: bool = False,
) -> str:
    # ``DATABRICKS_BEARER`` is the CI escape hatch: when set, skip the
    # `databricks auth token` subprocess entirely and return the pre-fetched
    # bearer directly. Used by the e2e job, where the protected runner has
    # no `databricks auth login` cache and `databricks auth token` only knows
    # how to read user-OAuth caches (not M2M client_credentials). Mirrors the
    # same short-circuit honored by the ``ucode auth-token`` credential helper.
    bearer = os.environ.get("DATABRICKS_BEARER", "").strip()
    if bearer:
        _debug("get_databricks_token", "using DATABRICKS_BEARER env var")
        return bearer

    _log_auth_diagnostics()
    # See has_valid_databricks_auth: resolve the profile from the host when
    # the caller didn't supply one, so duplicate-host cfgs don't break us.
    profile = profile or find_profile_name_for_host(workspace)
    env = build_databricks_cli_env(workspace, profile)
    cmd = [
        "databricks",
        "auth",
        "token",
        "--host",
        workspace,
        *_profile_args(profile),
        "--output",
        "json",
    ]
    if force_refresh:
        cmd.append("--force-refresh")

    _debug(
        "get_databricks_token.env",
        "set="
        + ",".join(sorted(k for k in env if k.startswith("DATABRICKS_") or k in {"BUNDLE_PROFILE"}))
        + f" profile={profile or '<none>'}",
    )

    def _fetch() -> str:
        try:
            result = run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            _debug("auth token", _format_subprocess_result(result))
            if result.returncode == 0:
                return json.loads(result.stdout or "{}").get("access_token", "")
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            _debug("auth token", f"exception: {type(exc).__name__}: {exc}")
        return ""

    token = _fetch()
    if not token:
        # Session may have expired — attempt non-interactive re-auth and retry once.
        _debug("auth token", "empty on first fetch; attempting auth login --no-browser")
        try:
            reauth = run(
                [
                    "databricks",
                    "auth",
                    "login",
                    "--host",
                    workspace,
                    *_profile_args(profile),
                    "--no-browser",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            _debug("auth login", _format_subprocess_result(reauth))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            _debug("auth login", f"exception: {type(exc).__name__}: {exc}")
        token = _fetch()

    if not token:
        profile_name = profile or find_profile_name_for_host(workspace)
        stale_profile_hint = ""
        if profile_name:
            stale_profile_hint = (
                " The saved Databricks CLI profile may be stale or invalid. Try:\n"
                f"  databricks auth logout --profile {profile_name}\n"
                f"  databricks auth login --host {workspace} --profile {profile_name}"
            )
        raise RuntimeError(
            f"Databricks CLI returned no access token for {workspace}. "
            "Run `databricks auth login` to re-authenticate."
            f"{stale_profile_hint}"
        )
    return token


def _extract_connection_page(payload: object) -> tuple[list[dict], str | None]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], None
    if not isinstance(payload, dict):
        raise RuntimeError("Databricks connections listing returned invalid JSON.")

    payload_dict = cast(dict[str, object], payload)
    raw_connections = payload_dict.get("connections") or []
    if not isinstance(raw_connections, list):
        raise RuntimeError("Databricks connections listing returned invalid JSON.")

    next_page_token = payload_dict.get("next_page_token")
    if next_page_token is not None and not isinstance(next_page_token, str):
        raise RuntimeError("Databricks connections listing returned invalid JSON.")

    return [item for item in raw_connections if isinstance(item, dict)], next_page_token


def list_databricks_connections(workspace: str, profile: str | None = None) -> list[dict]:
    env = build_databricks_cli_env(workspace)
    connections: list[dict] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()

    try:
        while True:
            cmd = [
                "databricks",
                "connections",
                "list",
                *_profile_args(profile),
                "--max-results",
                "0",
                "--output",
                "json",
            ]
            if page_token:
                cmd.extend(["--page-token", page_token])

            result = run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            payload = json.loads(result.stdout or "{}")
            page_connections, page_token = _extract_connection_page(payload)
            connections.extend(page_connections)

            if not page_token:
                return connections
            if page_token in seen_page_tokens:
                raise RuntimeError("Databricks connections listing returned a repeated page token.")
            seen_page_tokens.add(page_token)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Failed to list Databricks connections via `databricks connections list`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while listing Databricks connections.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Databricks connections listing returned invalid JSON.") from exc


def _extract_genie_spaces_page(payload: object) -> tuple[list[dict], str | None]:
    if not isinstance(payload, dict):
        raise RuntimeError("Databricks Genie spaces listing returned invalid JSON.")

    payload_dict = cast(dict[str, object], payload)
    raw_spaces = payload_dict.get("spaces") or []
    if not isinstance(raw_spaces, list):
        raise RuntimeError("Databricks Genie spaces listing returned invalid JSON.")

    next_page_token = payload_dict.get("next_page_token")
    if next_page_token is not None and not isinstance(next_page_token, str):
        raise RuntimeError("Databricks Genie spaces listing returned invalid JSON.")

    return [item for item in raw_spaces if isinstance(item, dict)], next_page_token


def list_genie_spaces(workspace: str, profile: str | None = None) -> list[dict]:
    env = build_databricks_cli_env(workspace)
    spaces: list[dict] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()

    try:
        while True:
            cmd = [
                "databricks",
                "genie",
                "list-spaces",
                *_profile_args(profile),
                "--page-size",
                "100",
                "--output",
                "json",
            ]
            if page_token:
                cmd.extend(["--page-token", page_token])

            result = run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            payload = json.loads(result.stdout or "{}")
            page_spaces, page_token = _extract_genie_spaces_page(payload)
            spaces.extend(page_spaces)

            if not page_token:
                return spaces
            if page_token in seen_page_tokens:
                raise RuntimeError(
                    "Databricks Genie spaces listing returned a repeated page token."
                )
            seen_page_tokens.add(page_token)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Failed to list Databricks Genie spaces via `databricks genie list-spaces`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while listing Databricks Genie spaces.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Databricks Genie spaces listing returned invalid JSON.") from exc


def _extract_apps_payload(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        payload_dict = cast(dict[str, object], payload)
        raw_apps = payload_dict.get("apps") or []
        if isinstance(raw_apps, list):
            return [item for item in raw_apps if isinstance(item, dict)]
    raise RuntimeError("Databricks apps listing returned invalid JSON.")


def list_databricks_apps(workspace: str, profile: str | None = None) -> list[dict]:
    env = build_databricks_cli_env(workspace)
    try:
        result = run(
            [
                "databricks",
                "apps",
                "list",
                *_profile_args(profile),
                "--limit",
                "1000",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return _extract_apps_payload(json.loads(result.stdout or "[]"))
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Failed to list Databricks apps via `databricks apps list`.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while listing Databricks apps.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Databricks apps listing returned invalid JSON.") from exc


@functools.cache
def resolve_ucode_invocation() -> list[str]:
    """Return an absolute argv prefix that invokes ``ucode``.

    Agent launchers (a Claude/Codex credential helper, a GUI-spawned shell) can
    run with a thinned PATH that lacks the uv tool bin dir, so the credential
    helper command we bake into agent configs must not depend on ``ucode`` being
    on PATH. Resolution order:

    1. ``ucode`` already on PATH (``shutil.which``);
    2. ``<uv tool dir --bin>/ucode[.exe]`` (mirrors ``_uv_tool_mlflow_path``);
    3. ``[sys.executable, "-m", "ucode"]`` as a last resort.

    Cached because the absolute path is stable for the process lifetime and this
    is called on every state hydration.
    """
    found = shutil.which("ucode")
    if found:
        return [found]
    uv_bin = _uv_tool_bin_path("ucode")
    if uv_bin:
        return [uv_bin]
    return [sys.executable, "-m", "ucode"]


def _uv_tool_bin_path(name: str) -> str | None:
    """Absolute path to a binary inside uv's tool bin dir, or None."""
    if not shutil.which("uv"):
        return None
    try:
        result = run(
            ["uv", "tool", "dir", "--bin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    bin_dir = (result.stdout or "").strip()
    if result.returncode != 0 or not bin_dir:
        return None
    exe = f"{name}.exe" if platform.system() == "Windows" else name
    candidate = Path(bin_dir) / exe
    return str(candidate) if candidate.exists() else None


def build_auth_helper_argv(workspace: str, profile: str | None = None) -> list[str]:
    """argv for the cross-platform credential helper (no shell required).

    Codex runs ``auth.command``/``auth.args`` directly (no shell), so spaces in
    paths are handled natively. This is the canonical form; the string variant
    is derived from it.
    """
    argv = [*resolve_ucode_invocation(), "auth-token", "--host", workspace.rstrip("/")]
    if profile:
        argv += ["--profile", profile]
    return argv


def _dquote(token: str) -> str:
    """Wrap a token in double quotes for both cmd.exe and POSIX shells.

    Claude's ``apiKeyHelper`` is a single command string run through a shell, so
    paths/profiles containing spaces (e.g. ``C:\\Users\\First Last\\...``) must be
    quoted. Double quotes are honored by both cmd.exe and POSIX shells; embedded
    double quotes (vanishingly rare in exe paths/profile names) are backslash
    escaped.
    """
    return '"' + token.replace('"', '\\"') + '"'


def build_auth_helper_string(workspace: str, profile: str | None = None) -> str:
    """Plain command string for Claude's ``apiKeyHelper`` (shell-invoked).

    Every token is double-quoted so the command parses identically under
    cmd.exe and POSIX shells regardless of spaces in the resolved ``ucode``
    path or the profile name.
    """
    return " ".join(_dquote(part) for part in build_auth_helper_argv(workspace, profile))


def discover_claude_models(workspace: str, token: str) -> tuple[dict[str, str], str | None]:
    """Discover Claude families on this workspace's AI Gateway.

    Returns (models_by_family, reason). reason is None on success; otherwise it
    describes why the dict is empty (HTTP error, network error, or no models
    matching the expected naming convention).
    """
    hostname = workspace_hostname(workspace)
    payload, reason = _http_get_json(f"https://{hostname}/ai-gateway/anthropic/v1/models", token)
    if payload is None:
        return {}, reason

    data = cast(dict, payload) if isinstance(payload, dict) else {}
    raw_ids = [
        m["id"]
        for m in data.get("data", [])
        if isinstance(m.get("id"), str) and not m["id"].endswith("-anthropic")
    ]

    result: dict[str, str] = {}
    for family, key in [("opus", "opus"), ("sonnet", "sonnet"), ("haiku", "haiku")]:
        candidates = sorted(
            [m for m in raw_ids if f"databricks-claude-{family}-" in m],
            reverse=True,
        )
        if candidates:
            result[key] = candidates[0]
    if result:
        return result, None
    if not raw_ids:
        return {}, "AI Gateway returned no Claude model ids"
    sample = ", ".join(raw_ids[:5])
    return {}, (
        "AI Gateway returned model ids but none matched "
        f"`databricks-claude-{{opus,sonnet,haiku}}-*` (got: {sample})"
    )


def fetch_ai_gateway_claude_models(workspace: str, token: str) -> dict[str, str]:
    """Backwards-compatible wrapper that discards the diagnostic reason."""
    models, _ = discover_claude_models(workspace, token)
    return models


def discover_endpoints_with_api_type(
    workspace: str, token: str, api_type: str
) -> tuple[list[str], str | None]:
    """List endpoint names whose served_entities expose api_type with v2 support.

    Returns (endpoints, reason). reason is None on success; otherwise it
    describes why the list is empty.
    """
    hostname = workspace_hostname(workspace)
    payload, reason = _http_get_json(
        f"https://{hostname}/api/2.0/serving-endpoints:foundation-models", token
    )
    if payload is None:
        return [], reason

    data = cast(dict, payload) if isinstance(payload, dict) else {}
    endpoints = data.get("endpoints", [])
    out: list[str] = []
    saw_endpoint_without_v2 = False
    for ep in endpoints:
        name = ep.get("name", "")
        entities = ep.get("config", {}).get("served_entities", [])
        api_types: set[str] = set()
        any_v2 = False
        for se in entities:
            fm = se.get("foundation_model", {})
            if fm.get("ai_gateway_v2_supported") is True:
                any_v2 = True
                api_types.update(fm.get("api_types", []))
        if not any_v2 and entities:
            saw_endpoint_without_v2 = True
        if api_type in api_types:
            out.append(name)
    if out:
        return sorted(out), None
    if not endpoints:
        return [], "foundation-models listing returned no endpoints"
    if saw_endpoint_without_v2:
        return [], (
            f"no endpoint exposes api_type `{api_type}` with "
            "`ai_gateway_v2_supported=true` (workspace has v1-only endpoints)"
        )
    return [], f"no endpoint exposes api_type `{api_type}`"


def _fetch_endpoints_with_api_type(workspace: str, token: str, api_type: str) -> list[str]:
    """Backwards-compatible wrapper that discards the diagnostic reason."""
    endpoints, _ = discover_endpoints_with_api_type(workspace, token, api_type)
    return endpoints


def discover_gemini_models(workspace: str, token: str) -> tuple[list[str], str | None]:
    return discover_endpoints_with_api_type(workspace, token, "gemini/v1/generateContent")


def discover_codex_models(workspace: str, token: str) -> tuple[list[str], str | None]:
    return discover_endpoints_with_api_type(workspace, token, "openai/v1/responses")


def fetch_gemini_models(workspace: str, token: str) -> list[str]:
    models, _ = discover_gemini_models(workspace, token)
    return models


def fetch_codex_models(workspace: str, token: str) -> list[str]:
    models, _ = discover_codex_models(workspace, token)
    return models


def ensure_ai_gateway_v2(workspace: str, token: str) -> None:
    """Probe AI Gateway v2 and raise if unavailable.

    Uses the dedicated v2 listing endpoint `GET /api/ai-gateway/v2/endpoints`:
    a 200 response (even with an empty list) means v2 is wired up on this
    workspace — a "no endpoints provisioned" case will surface naturally in
    downstream discovery. Failure branches:

    - 401 / 403 / 400 with `Invalid Token`: the token is bad for *this*
      workspace.
    - 404: AI Gateway V2 is not enabled on this workspace — point at the docs.
    - other (5xx, network errors): surface the reason verbatim.
    """
    hostname = workspace_hostname(workspace)
    url = f"https://{hostname}/api/ai-gateway/v2/endpoints?page_size=1"
    payload, reason = _http_get_json(url, token)
    if payload is not None:
        return
    reason_str = reason or "unknown error"
    if _looks_like_auth_failure(reason_str):
        raise RuntimeError(
            f"Databricks rejected the access token for {workspace} ({reason_str}). "
            f"Try:\n"
            f"  databricks auth logout --host {workspace}\n"
            f"  databricks auth login --host {workspace}"
        )
    if "HTTP 404" in reason_str:
        raise RuntimeError(
            "Databricks Unity AI Gateway is not enabled on this workspace "
            f"({reason_str}). See {AI_GATEWAY_V2_DOCS_URL}"
        )
    raise RuntimeError(
        "Databricks Unity AI Gateway probe failed on this workspace "
        f"({reason_str}). See {AI_GATEWAY_V2_DOCS_URL}"
    )


def _looks_like_auth_failure(reason: str) -> bool:
    """True when the gateway response signals the token is not accepted.

    Covers 401/403 directly and the gateway's 400 + `Invalid Token` body
    (which happens when the bearer is valid but issued for a different
    workspace)."""
    if "HTTP 401" in reason or "HTTP 403" in reason:
        return True
    if "HTTP 400" in reason and "invalid token" in reason.lower():
        return True
    return False


def discover_sql_warehouse_http_path(
    workspace: str,
    token: str,
    *,
    quiet: bool = False,
) -> str:
    hostname = workspace_hostname(workspace)
    request = urllib_request.Request(
        f"https://{hostname}/api/2.0/sql/warehouses",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib_request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        detail = body.strip() or f"HTTP {exc.code}"
        raise RuntimeError(f"Failed to list SQL warehouses: {detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Could not reach workspace hostname {hostname}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Databricks warehouse discovery returned invalid JSON.") from exc

    warehouses = payload.get("warehouses")
    if not isinstance(warehouses, list) or not warehouses:
        raise RuntimeError(
            "No SQL warehouses found in this workspace. Create one or pass `--http-path`."
        )

    running = [w for w in warehouses if isinstance(w, dict) and w.get("state") == "RUNNING"]
    chosen = (
        running[0]
        if running
        else next(
            (w for w in warehouses if isinstance(w, dict) and w.get("id")),
            None,
        )
    )
    if not chosen:
        raise RuntimeError("No usable SQL warehouse was returned by Databricks.")

    warehouse_id = chosen.get("id")
    if not isinstance(warehouse_id, str) or not warehouse_id.strip():
        raise RuntimeError("Databricks returned a warehouse without an ID.")

    warehouse_name = chosen.get("name")
    warehouse_state = chosen.get("state", "UNKNOWN")
    label_value = (
        warehouse_name if isinstance(warehouse_name, str) and warehouse_name else warehouse_id
    )
    if not quiet:
        print_note(f"Using SQL warehouse `{label_value}` ({warehouse_state}).")
    return f"/sql/1.0/warehouses/{warehouse_id}"


def run_usage_query(
    workspace: str,
    http_path: str,
    token: str,
    query: str,
) -> tuple[list[str], list[tuple]]:
    try:
        logging.getLogger("databricks.sql").setLevel(logging.ERROR)
        from databricks import sql
    except ImportError as exc:
        raise RuntimeError(
            "`databricks-sql-connector` is not installed. "
            "Install it with `pip install databricks-sql-connector`."
        ) from exc

    try:
        with sql.connect(
            server_hostname=workspace_hostname(workspace),
            http_path=http_path,
            access_token=token,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                columns = [desc[0] for desc in (cursor.description or [])]
                rows = cast(list[tuple], cursor.fetchall())
    except ServerOperationError as exc:
        if _is_usage_table_access_error(exc):
            raise RuntimeError(
                "Unable to read `system.ai_gateway.usage`. Ask your workspace admin "
                "to enable READ access to `system.ai_gateway.usage` for your account."
            ) from exc
        raise RuntimeError(f"Usage query failed: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Usage query failed: {exc}") from exc

    return columns, rows


def _is_usage_table_access_error(exc: BaseException) -> bool:
    """Return True when a `ServerOperationError` blocks reads of
    `system.ai_gateway.usage` — gated on one of the bracketed error codes
    `INSUFFICIENT_PERMISSIONS` plus a `system.ai_gateway` substring (identifier quoting
    stripped first)."""
    normalized = str(exc).lower().translate(str.maketrans("", "", """`[]"'"""))
    if "system.ai_gateway" not in normalized:
        return False
    return "insufficient_permissions" in normalized


# ---------------------------------------------------------------------------
# URL builders (AI Gateway v2 only — no fallback to /serving-endpoints)
# ---------------------------------------------------------------------------


def build_tool_base_url(tool: str, workspace: str) -> str:
    if tool == "codex":
        return f"{workspace}/ai-gateway/codex/v1"
    if tool == "claude":
        return f"{workspace}/ai-gateway/anthropic"
    if tool == "gemini":
        return f"{workspace}/ai-gateway/gemini"
    if tool == "opencode":
        raise RuntimeError(
            "OpenCode has multiple base URLs — use build_opencode_base_urls() instead."
        )
    if tool == "copilot":
        raise RuntimeError(
            "Copilot has multiple base URLs — use build_copilot_base_urls() instead."
        )
    if tool == "pi":
        raise RuntimeError("Pi has multiple base URLs — use build_pi_base_urls() instead.")
    raise RuntimeError(f"Unsupported tool '{tool}'.")


def build_opencode_base_urls(workspace: str) -> dict[str, str]:
    return {
        "anthropic": build_tool_base_url("claude", workspace) + "/v1",
        "gemini": build_tool_base_url("gemini", workspace) + "/v1beta",
    }


def build_pi_base_urls(workspace: str) -> dict[str, str]:
    # Pi speaks each model family's native API dialect to its dedicated gateway
    # path (verified end-to-end). Each `api` type appends its own path suffix:
    #
    # - anthropic-messages       appends `/v1/messages`
    # - openai-responses         appends `/responses`
    # - google-generative-ai     appends `/v1beta/models/{id}:streamGenerateContent`
    # - openai-completions       appends `/chat/completions`
    #
    # So the baseUrls below stop just before the suffix Pi will tack on.
    # Compat flags applied per-provider in agents/pi.py; required for `oss`
    # only (MLflow rejects `store` and `tools[].function.strict`).
    return {
        "claude": build_tool_base_url("claude", workspace),
        "openai": build_tool_base_url("codex", workspace),
        "gemini": build_tool_base_url("gemini", workspace) + "/v1beta",
    }


def build_copilot_base_url(workspace: str) -> str:
    # Copilot CLI's `openai` provider appends `/chat/completions` to the
    # configured base URL. The Databricks MLflow chat-completions gateway is
    # OpenAI-compatible and serves Claude, codex (gpt-5), and gemini models
    # behind one URL.
    return f"{workspace}/ai-gateway/mlflow/v1"


def build_shared_base_urls(workspace: str) -> dict[str, str | dict[str, str]]:
    urls: dict[str, str | dict[str, str]] = {
        "codex": build_tool_base_url("codex", workspace),
        "claude": build_tool_base_url("claude", workspace),
        "gemini": build_tool_base_url("gemini", workspace),
        "opencode": build_opencode_base_urls(workspace),
        "copilot": build_copilot_base_url(workspace),
        "pi": build_pi_base_urls(workspace),
    }
    return urls
