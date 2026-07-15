from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from doyoutrade.capabilities.manifest import CapabilityManifest


class CapabilityRegistry:
    def __init__(self, manifests: Iterable[CapabilityManifest]):
        items = list(manifests)
        by_id: dict[str, CapabilityManifest] = {}
        for manifest in items:
            if manifest.id in by_id:
                raise ValueError(f"duplicate capability id: {manifest.id}")
            by_id[manifest.id] = manifest
        self._items = sorted(items, key=lambda item: (item.kind, item.id))
        self._by_id = by_id

    @classmethod
    def from_dicts(cls, raw_items: Iterable[dict]) -> "CapabilityRegistry":
        return cls(CapabilityManifest.from_dict(raw) for raw in raw_items)

    @classmethod
    def from_paths(cls, paths: Iterable[Path]) -> "CapabilityRegistry":
        return cls.from_dicts(_load_manifest_dicts(paths))

    def get(self, capability_id: str) -> CapabilityManifest | None:
        return self._by_id.get(capability_id)

    def ids(self, *, kind: str | None = None) -> list[str]:
        return [item.id for item in self._filter(kind)]

    def kinds(self) -> list[str]:
        return sorted({item.kind for item in self._items})

    def summary(self, *, kind: str | None = None) -> list[dict]:
        return [item.public_summary() for item in self._filter(kind)]

    def data_provider_ids(self) -> list[str]:
        return [
            str(item.metadata["provider_id"])
            for item in self._filter("data_provider")
            if isinstance(item.metadata.get("provider_id"), str)
        ]

    def model_provider_kinds(self) -> list[str]:
        return [
            str(item.metadata["provider_kind"])
            for item in self._filter("model_provider")
            if isinstance(item.metadata.get("provider_kind"), str)
        ]

    def channel_types(self) -> list[str]:
        return [
            str(item.metadata["channel_type"])
            for item in self._filter("channel")
            if isinstance(item.metadata.get("channel_type"), str)
        ]

    def _filter(self, kind: str | None) -> list[CapabilityManifest]:
        if kind is None:
            return list(self._items)
        return [item for item in self._items if item.kind == kind]


def load_builtin_capabilities(*, extra_dirs: Iterable[Path] | None = None) -> CapabilityRegistry:
    roots = [Path(__file__).resolve().parent / "builtins"]
    if extra_dirs is not None:
        roots.extend(Path(path) for path in extra_dirs)
    return CapabilityRegistry.from_dicts(_load_manifest_dicts(roots))


def _load_manifest_dicts(roots: Iterable[Path]) -> list[dict]:
    raw_items: list[dict] = []
    for root in roots:
        if root.is_file():
            paths = [root]
        else:
            paths = sorted(root.glob("*.json"))
        for path in paths:
            with path.open("r", encoding="utf-8") as fh:
                raw_items.append(json.load(fh))
    return raw_items
