"""API-layer operation handlers consumed by ``doyoutrade-cli``.

These are the domain-side ``OperationHandler`` subclasses (task / cron /
cycle / model route / market data / factor / pattern / stock lookup /
strategy discovery) wired into :func:`doyoutrade.api.cli_tools.build_cli_tool_registry`.
They are intentionally separate from the agent chat surface in
:mod:`doyoutrade.tools` — the chat registry only exposes framework
primitives (``load_skill`` / ``compact`` / file tools / bash), and every
other operation reaches the agent via ``execute_bash`` → ``doyoutrade-cli``.
"""
