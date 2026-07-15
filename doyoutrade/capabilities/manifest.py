from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_PUBLIC_METADATA_KEYS = (
    "provider_id",
    "provider_kind",
    "channel_type",
    "ui_hints",
)


@dataclass(frozen=True)
class CapabilityManifest:
    id: str
    kind: str
    label: str
    description: str
    config_schema: dict[str, Any]
    runtime: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CapabilityManifest":
        if not isinstance(raw, dict):
            raise ValueError("capability manifest must be an object")
        capability_id = _required_string(raw, "id")
        kind = _required_string(raw, "kind")
        label = _required_string(raw, "label")
        description = _required_string(raw, "description")
        config_schema = raw.get("config_schema")
        if not isinstance(config_schema, dict):
            raise ValueError(f"capability {capability_id}: config_schema must be an object")
        runtime = raw.get("runtime") or {}
        if not isinstance(runtime, dict):
            raise ValueError(f"capability {capability_id}: runtime must be an object")
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError(f"capability {capability_id}: metadata must be an object")
        return cls(
            id=capability_id,
            kind=kind,
            label=label,
            description=description,
            config_schema=dict(config_schema),
            runtime=dict(runtime),
            metadata=dict(metadata),
        )

    def public_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "id": self.id,
            "capability_id": self.id,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "config_schema": self.config_schema,
        }
        for key in _PUBLIC_METADATA_KEYS:
            if key in self.metadata:
                summary[key] = self.metadata[key]
        return summary


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"capability manifest field {key} is required")
    return value.strip()
