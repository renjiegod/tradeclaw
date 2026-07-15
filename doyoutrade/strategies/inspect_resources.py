"""Shared projection for strategy definition inspection (CLI + assistant tool)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def match_query_tokens(haystack: dict[str, str], tokens: list[str]) -> list[str] | None:
    """Return matched field names in declaration order, or None if any token misses."""

    if not tokens:
        return []
    matched: set[str] = set()
    for token in tokens:
        hits = [field for field, text in haystack.items() if token in text]
        if not hits:
            return None
        matched.update(hits)
    return [field for field in haystack.keys() if field in matched]


def _definition_haystack(row: dict[str, Any]) -> dict[str, str]:
    return {
        "definition_id": str(row.get("definition_id") or "").lower(),
        "name": str(row.get("name") or "").lower(),
        "generation_prompt": str(row.get("generation_prompt") or "").lower(),
    }


def _created_at_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    created = row.get("created_at")
    if isinstance(created, datetime):
        created_key = created.isoformat()
    else:
        created_key = str(created or "")
    return (created_key, str(row.get("definition_id") or ""))


def build_strategy_inspect_payload(
    definitions: list[dict[str, Any]],
    *,
    query: str | None = None,
) -> dict[str, Any]:
    """Build inspect output from definition rows (API summaries or tool snapshots)."""

    tokens = [token for token in (query or "").lower().split() if token]

    groups_by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in definitions:
        code_hash = str(row.get("code_hash") or "")
        if not code_hash:
            continue
        groups_by_hash.setdefault(code_hash, []).append(row)

    recommended_by_definition: dict[str, str] = {}
    all_duplicate_groups: list[dict[str, Any]] = []
    for code_hash, group in groups_by_hash.items():
        ranked = sorted(group, key=_created_at_sort_key)
        canonical_id = str(ranked[0].get("definition_id") or "")
        for row in ranked:
            definition_id = str(row.get("definition_id") or "")
            if definition_id:
                recommended_by_definition[definition_id] = canonical_id
        if len(group) > 1:
            all_duplicate_groups.append(
                {
                    "code_hash": code_hash,
                    "recommended_reuse_id": canonical_id,
                    "definition_ids": [str(row.get("definition_id") or "") for row in ranked],
                }
            )

    definitions_payload: list[dict[str, Any]] = []
    matched_definition_ids: set[str] = set()
    for row in definitions:
        if not isinstance(row, dict):
            continue
        definition_id = str(row.get("definition_id") or "")
        if not definition_id:
            continue
        reasons = match_query_tokens(_definition_haystack(row), tokens)
        if reasons is None:
            continue
        matched_definition_ids.add(definition_id)
        entry: dict[str, Any] = {
            "definition_id": definition_id,
            "name": row.get("name"),
            "status": row.get("status"),
            "code_hash": row.get("code_hash"),
        }
        recommended = recommended_by_definition.get(definition_id)
        if recommended is not None:
            entry["recommended_reuse_id"] = recommended
        if tokens:
            entry["match_reasons"] = reasons
        definitions_payload.append(entry)

    payload: dict[str, Any] = {
        "status": "ok",
        "definitions": definitions_payload,
    }
    if tokens:
        payload["query"] = query
        payload["matched_tokens"] = tokens
        payload["total_definitions"] = len(definitions)

    if tokens:
        duplicate_groups = [
            group
            for group in all_duplicate_groups
            if any(did in matched_definition_ids for did in group["definition_ids"])
        ]
    else:
        duplicate_groups = all_duplicate_groups
    if duplicate_groups:
        payload["duplicate_definition_groups"] = duplicate_groups
        payload["reuse_hint"] = (
            "Definitions in duplicate_definition_groups share identical source code. "
            "Prefer the one whose definition_id matches recommended_reuse_id before "
            "creating a new copy."
        )
    return payload
