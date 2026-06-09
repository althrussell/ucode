"""Enable ``python -m ucode`` as a fallback invocation path.

The credential helper resolver (:func:`ucode.databricks.resolve_ucode_invocation`)
falls back to ``[sys.executable, "-m", "ucode"]`` when the ``ucode`` console
script cannot be found on PATH or in uv's tool bin dir, so this module must
dispatch to the same entry point as the installed script.
"""

from ucode.cli import main

if __name__ == "__main__":
    main()
