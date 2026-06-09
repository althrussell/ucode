"""Cross-platform child-process launch for the agent binaries.

POSIX uses ``os.execvp`` (process replacement) for the lowest overhead and
exact signal/exit-code semantics. Windows cannot ``execvp`` an npm ``.cmd``
shim (which is how ``claude``/``codex`` install) and mishandles Ctrl-C and exit
codes, so there we resolve the real shim and spawn it as a child, forwarding
Ctrl-C and re-raising the child's exit code as ``SystemExit``.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess

_BATCH_SUFFIXES = (".cmd", ".bat")


def refresh_windows_path() -> None:
    """Reload the persisted PATH from the registry into this process (Windows).

    winget/MSI installers (Databricks CLI, Node.js) append their install
    directories to the *persisted* user/machine PATH, but a process that is
    already running keeps the PATH it inherited at launch — so a tool installed
    mid-run is invisible until the terminal is restarted. Re-reading both
    registry PATH values and merging them into ``os.environ`` makes freshly
    installed executables resolvable immediately. No-op off Windows; never
    raises.
    """
    if platform.system() != "Windows":
        return
    try:
        import winreg
    except ImportError:  # pragma: no cover - winreg is always present on Windows
        return

    # Windows always separates PATH entries with ';' regardless of the host this
    # code is exercised on, so use it explicitly rather than os.pathsep.
    sep = ";"
    # winreg exists only on Windows; static checkers resolving on POSIX can't see
    # its members, so silence those attribute lookups here.
    hklm = winreg.HKEY_LOCAL_MACHINE  # ty: ignore[unresolved-attribute]
    hkcu = winreg.HKEY_CURRENT_USER  # ty: ignore[unresolved-attribute]
    parts: list[str] = []
    for root, subkey in (
        (hklm, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (hkcu, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:  # ty: ignore[unresolved-attribute]
                value, _ = winreg.QueryValueEx(key, "Path")  # ty: ignore[unresolved-attribute]
        except OSError:
            continue
        if value:
            parts.extend(p for p in str(value).split(sep) if p)
    # Keep whatever the process already had (e.g. the uv tool dir) as a fallback.
    parts.extend(p for p in os.environ.get("PATH", "").split(sep) if p)

    seen: set[str] = set()
    merged: list[str] = []
    for raw in parts:
        # Registry PATHs are REG_EXPAND_SZ — expand %SystemRoot% etc.
        expanded = os.path.expandvars(raw)
        key = expanded.lower()
        if key not in seen:
            seen.add(key)
            merged.append(expanded)
    os.environ["PATH"] = sep.join(merged)


def windows_safe_args(args: list[str]) -> list[str]:
    """Make ``args`` robust for ``subprocess`` on Windows. No-op elsewhere.

    ``subprocess`` with ``shell=False`` (which is what we always use) hands the
    command straight to ``CreateProcess``. That only auto-appends ``.exe`` and
    **cannot execute ``.cmd``/``.bat`` shims directly** — which is exactly how
    npm installs ``claude``/``codex`` and how some CLIs ship. We therefore:

    * resolve a bare program name to its real path via PATHEXT-aware
      ``shutil.which`` (so ``databricks`` finds ``databricks.exe`` and ``npm``
      finds ``npm.cmd``), and
    * route batch shims through ``%COMSPEC% /c`` so cmd.exe interprets them.

    If the program can't be resolved we leave ``args`` untouched so the caller
    still gets subprocess's normal ``FileNotFoundError``.
    """
    if platform.system() != "Windows" or not args:
        return args
    program = args[0]
    # Detect Windows path separators explicitly (\ or /) rather than os.sep so
    # the check is correct regardless of the host the code is exercised on.
    has_separator = "\\" in program or "/" in program
    resolved = program if has_separator else shutil.which(program)
    if not resolved:
        return args
    if resolved.lower().endswith(_BATCH_SUFFIXES):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        # NOTE: no ``/s``. With ``/s`` cmd.exe always strips the first and last
        # quote on the line, which mangles a quoted interpreter path that
        # contains spaces (e.g. ``C:\Program Files\nodejs\npm.cmd`` → cmd tries
        # to run ``C:\Program``). Without ``/s``, cmd.exe preserves the quotes
        # around a single quoted executable path, which is exactly what we need.
        return [comspec, "/d", "/c", resolved, *args[1:]]
    return [resolved, *args[1:]]


def exec_or_spawn(argv: list[str], env: dict | None = None) -> None:
    """Replace the current process (POSIX) or spawn-and-wait (Windows).

    :param argv: Full argument vector; ``argv[0]`` is the program.
    :param env: Optional environment mapping. When provided it is used verbatim
        (it should already contain any mutations such as ``OAUTH_TOKEN``);
        otherwise the child inherits the current environment.
    :raises SystemExit: On Windows, with the child's return code. On POSIX this
        function does not return (the process image is replaced).
    """
    if not argv:
        raise ValueError("argv must not be empty")

    if platform.system() != "Windows":
        if env is None:
            os.execvp(argv[0], argv)
        else:
            os.execvpe(argv[0], argv, env)
        return  # pragma: no cover - process image replaced above

    # Windows: resolve the real executable/shim (`claude.cmd`, `codex.cmd`, ...)
    # and route batch shims through cmd.exe so the spawn doesn't depend on
    # CreateProcess extension resolution (which can't run .cmd/.bat directly),
    # then run it as a child and mirror its exit code.
    proc = subprocess.Popen(windows_safe_args(argv), env=env)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        # Forward Ctrl-C so the agent can shut down cleanly, then reap it.
        try:
            proc.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            pass
        returncode = proc.wait()
    raise SystemExit(returncode)
