"""Swarm 多智能体系统 —— 数据模型（运行期 DTO）。

全部用 Pydantic 定义，由 dag / presets / store / orchestrator 共享。
枚举用 str+Enum 以保证 JSON 序列化兼容。

移植自 Vibe-Trading ``agent/src/swarm/models.py``，去掉了文件存储 / grounding
相关字段，适配 doyoutrade 的 SQLAlchemy + AssistantService 执行模型。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """SwarmTask 生命周期状态。

    迁移路径：pending -> blocked -> in_progress -> completed | failed | cancelled
    """

    pending = "pending"
    blocked = "blocked"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class RunStatus(str, Enum):
    """SwarmRun 生命周期状态。

    迁移路径：pending -> running -> completed | failed | cancelled
    """

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class WorkerStatus(str, Enum):
    """worker 返回的终态。

    ``incomplete`` 与 ``failed`` 不同：worker 正常跑完但没产出实质交付物，
    绝不能并入 ``completed``。
    """

    completed = "completed"
    failed = "failed"
    timeout = "timeout"
    incomplete = "incomplete"


class SwarmAgentSpec(BaseModel):
    """Swarm 中单个 agent 的角色定义。

    从 YAML preset 解析，描述 agent 身份、可用工具与约束。字段与 doyoutrade 的
    ``AgentRecord`` 几乎一一对应：tools -> tool_names、skills -> skill_names、
    max_iterations -> max_turns。

    Attributes:
        id: 唯一标识，如 "bull_advocate"。
        role: 角色描述。
        system_prompt: 注入 LLM 的系统提示词（支持 {var} 占位）。
        tools: 允许的工具名白名单（须为 doyoutrade 注册的真实工具）。
        skills: 允许的 skill 名列表（须为 doyoutrade/skills 下真实存在的）。
        max_iterations: ReAct 循环最大迭代数（映射到 AgentRecord.max_turns）。
        timeout_seconds: worker 超时秒数。
        model_route_name: 覆盖默认 model route；空串用全局默认。
        max_retries: 失败重试次数。
    """

    id: str
    role: str
    system_prompt: str
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    max_iterations: int = 25
    timeout_seconds: int = 600
    model_route_name: str = ""
    max_retries: int = 1


class SwarmTask(BaseModel):
    """Swarm DAG 中的一个任务节点，绑定到某个 agent。

    Attributes:
        id: 唯一标识，如 "task-bull"。
        agent_id: 执行此任务的 agent id。
        prompt_template: 用户提示词模板，支持 {var} 占位。
        depends_on: DAG 声明的上游任务 id（不可变）。
        blocked_by: 运行期剩余未完成上游 id（随运行收缩）。
        input_from: 从上游任务拉取摘要的映射，如 {"bull_report": "task-bull"}。
        status: 当前状态。
        summary: 完成后的摘要文本。
        session_id: 执行此 worker 的临时 assistant session（供前端 drill-down）。
        error: 失败时的错误信息。
        started_at / completed_at: ISO 时间戳。
        worker_iterations: worker 实际执行的迭代数。
    """

    id: str
    agent_id: str
    prompt_template: str
    depends_on: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    input_from: dict[str, str] = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.pending
    summary: str | None = None
    session_id: str | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    worker_iterations: int = 0


class SwarmEvent(BaseModel):
    """Swarm 事件日志条目，持久化后供 SSE 流式推送与运行后审计。

    Attributes:
        type: 事件类型，如 "run_started" / "task_started" / "task_completed" /
            "task_failed" / "task_blocked" / "task_retry" / "run_completed"。
        agent_id: 关联 agent id（可选）。
        task_id: 关联 task id（可选）。
        data: 附加数据。
        timestamp: ISO 时间戳。
    """

    type: str
    agent_id: str | None = None
    task_id: str | None = None
    data: dict = Field(default_factory=dict)
    timestamp: str


class SwarmRun(BaseModel):
    """单次 Swarm preset 执行的完整状态（聚合根）。

    Attributes:
        id: 运行 id，如 "swarm-20260620-ab12cd34"。
        preset_name: 使用的 preset 名。
        status: 运行状态。
        user_vars: 用户提供的模板变量。
        agents: 参与的 agent 定义列表。
        tasks: 所有任务条目。
        created_at / completed_at: ISO 时间戳。
        final_report: 最终汇总报告文本。
        total_input_tokens / total_output_tokens: 跨所有 worker 的累计 token。
        provider / model: 运行启动时生效的 provider / model（审计用）。
        error: 运行级错误信息。
    """

    id: str
    preset_name: str
    status: RunStatus = RunStatus.pending
    user_vars: dict[str, str] = Field(default_factory=dict)
    agents: list[SwarmAgentSpec] = Field(default_factory=list)
    tasks: list[SwarmTask] = Field(default_factory=list)
    created_at: str
    completed_at: str | None = None
    final_report: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    provider: str | None = None
    model: str | None = None
    error: str | None = None


class WorkerResult(BaseModel):
    """worker 执行完成后的返回值。

    Attributes:
        status: WorkerStatus —— completed|failed|timeout|incomplete。
        summary: 执行摘要（worker 最终回复文本）。
        session_id: 承载此 worker 的临时 assistant session id。
        iterations: 实际迭代数。
        error: 失败时的错误信息。
        input_tokens / output_tokens: 累计 token（精确或估算）。
    """

    status: WorkerStatus
    summary: str
    session_id: str | None = None
    iterations: int = 0
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
