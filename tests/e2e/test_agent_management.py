"""
E2E: Agent management — create → clone → session → message
"""
import asyncio
from unittest.mock import MagicMock

from doyoutrade.agent_runtime import AgentTurnResponse
from doyoutrade.assistant.repository import InMemoryAgentRepository, InMemoryAssistantRepository
from doyoutrade.assistant.service import AssistantService


class _EchoAdapter:
    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        if on_thinking_delta:
            await on_thinking_delta("thinking...")
        if on_text_delta:
            last = messages[-1].content if messages else ""
            await on_text_delta(f"Echo: {last}")
        return AgentTurnResponse(
            content=f"Echo: {messages[-1].content if messages else ''}",
            tool_calls=[],
            raw=MagicMock(tool_calls=None, content=f"Echo: {messages[-1].content if messages else ''}"),
        )


async def test_agent_management_flow():
    agent_repo = InMemoryAgentRepository()
    session_repo = InMemoryAssistantRepository()

    # 1. Create an agent
    agent = await agent_repo.create_agent({
        "name": "E2E Test Agent",
        "system_prompt": "You echo back user input.",
        "max_turns": 4,
        "tool_names": [],
        "skill_names": [],
    })
    assert agent["name"] == "E2E Test Agent"
    assert agent["is_default"] is False
    print("PASS: create agent")

    # 2. Clone the agent
    cloned = await agent_repo.clone_agent(agent["id"], "Cloned Agent")
    assert cloned["name"] == "Cloned Agent"
    assert cloned["system_prompt"] == agent["system_prompt"]
    assert cloned["id"] != agent["id"]
    print("PASS: clone agent")

    # 3. Create a session bound to the agent
    async def _factory(route):
        return _EchoAdapter()

    service = AssistantService(
        session_repo,
        agent_repository=agent_repo,
        model_adapter_factory=_factory,
    )
    session = await service.create_session(agent_id=cloned["id"], title="E2E Session")
    assert session["agent_id"] == cloned["id"]
    print("PASS: create session with agent_id")

    # 4. Send a message and get a response
    result = await service.send_message(session_id=session["session_id"], content="Hello")
    assert len(result["messages"]) == 2
    assert "Echo:" in result["messages"][1]["content"]
    print("PASS: send message")

    # 5. Verify session has agent_id persisted
    retrieved = await session_repo.get_session(session["session_id"])
    assert retrieved["agent_id"] == cloned["id"]
    print("PASS: verify agent_id persisted in session")

    # 6. Verify inactive agent cannot create session
    inactive_agent = await agent_repo.create_agent({
        "name": "Inactive Agent",
        "system_prompt": "I am inactive.",
        "status": "inactive",
    })
    try:
        await service.create_session(agent_id=inactive_agent["id"], title="Should Fail")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "inactive" in str(e)
    print("PASS: inactive agent blocks session creation")

    # 7. Verify missing agent
    try:
        await service.create_session(agent_id="nonexistent", title="Should Fail")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Agent not found" in str(e)
    print("PASS: missing agent blocks session creation")

    print("\nAll E2E tests passed!")


async def test_builtin_main_agent_flow():
    """E2E: the code-fixed main agent — pinned identity, locked edits, clone, chat."""
    from doyoutrade.assistant.main_agent import MAIN_AGENT_ID, MAIN_AGENT_NAME, builtin_tool_names
    from doyoutrade.persistence.errors import BuiltinAgentImmutableError

    agent_repo = InMemoryAgentRepository()
    session_repo = InMemoryAssistantRepository()

    # 1. ensure_main_agent pins the code-fixed identity.
    main = await agent_repo.ensure_main_agent()
    assert main["id"] == MAIN_AGENT_ID
    assert main["name"] == MAIN_AGENT_NAME
    assert main["is_builtin"] is True
    assert main["is_default"] is True
    assert main["editable_fields"] == ["model_route_name", "context_compaction", "max_turns"]
    print("PASS: ensure_main_agent pins fixed identity")

    # 2. Editable knobs change; locked fields are refused (visible error).
    await agent_repo.update_agent(MAIN_AGENT_ID, {"model_route_name": "fast", "max_turns": 10})
    for locked in ({"name": "X"}, {"skill_names": ["a"]}, {"system_prompt": "z"}):
        try:
            await agent_repo.update_agent(MAIN_AGENT_ID, dict(locked))
            assert False, f"locked update {locked} should have raised"
        except BuiltinAgentImmutableError:
            pass
    print("PASS: editable knobs allowed, locked fields rejected")

    # 3. Deleting the builtin is refused.
    try:
        await agent_repo.delete_agent(MAIN_AGENT_ID)
        assert False, "deleting builtin should have raised"
    except BuiltinAgentImmutableError:
        pass
    print("PASS: builtin delete rejected")

    # 4. Cloning yields an ordinary editable agent seeded with code defaults.
    clone = await agent_repo.clone_agent(MAIN_AGENT_ID, "Main Clone")
    assert clone["is_builtin"] is False
    assert clone["tool_names"] == list(builtin_tool_names())
    print("PASS: builtin clone inherits code defaults, is editable")

    # 5. The builtin can still run a chat session (loads full tools + template prompt).
    async def _factory(route):
        return _EchoAdapter()

    service = AssistantService(
        session_repo,
        agent_repository=agent_repo,
        model_adapter_factory=_factory,
    )
    session = await service.create_session(agent_id=MAIN_AGENT_ID, title="Main Session")
    result = await service.send_message(session_id=session["session_id"], content="Hi")
    assert "Echo:" in result["messages"][1]["content"]
    print("PASS: builtin main agent runs a chat session")

    print("\nBuiltin main-agent E2E tests passed!")


if __name__ == "__main__":
    asyncio.run(test_agent_management_flow())
    asyncio.run(test_builtin_main_agent_flow())