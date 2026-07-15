"""Allow ``python -m doyoutrade.cli`` as a fallback when the
``doyoutrade-cli`` console script isn't on PATH.

This is the last-resort discovery path documented in the main-agent
system prompt under "CLI envelope 速读". The console script in
``.venv/bin/doyoutrade-cli`` is the preferred entry point; ``execute_bash``
prepends the interpreter's bin dir to PATH so the console script
resolves in practice. ``python -m doyoutrade.cli`` works wherever the
doyoutrade package is importable, regardless of venv activation.
"""

from doyoutrade.cli.main import main


if __name__ == "__main__":  # pragma: no cover
    main()
