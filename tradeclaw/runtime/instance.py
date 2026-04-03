from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class AgentInstanceConfig:
    name: str
    mode: str
    orchestrator_mode: str = "single-agent"
    template_id: str = ""
    description: str = ""
    # Overrides config.data.default_provider when set (auto | mock | qmt or custom registered id).
    data_provider: str | None = None


@dataclass
class AgentInstance:
    config: AgentInstanceConfig
    worker: object
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "configured"
    last_error: str = ""
