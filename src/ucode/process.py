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
    # so the spawn does not depend on cmd.exe extension resolution, then run it
    # as a child and mirror its exit code.
    resolved = shutil.which(argv[0]) or argv[0]
    proc = subprocess.Popen([resolved, *argv[1:]], env=env)
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
