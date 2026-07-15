"""doyoutrade-cli: agent-facing CLI wrapping assistant tools.

Built so the assistant agent invokes doyoutrade operations via ``execute_bash``
running ``doyoutrade-cli ...`` instead of carrying every tool's schema in
its context window. The envelope contract, environment variables, and
exit-code rules live in the main-agent system prompt
(``doyoutrade/assistant/prompt_templates/main_agent.j2``) under the
"CLI envelope 速读" section — there's no separate skill to load.
"""

from doyoutrade.cli.main import main

__all__ = ["main"]
