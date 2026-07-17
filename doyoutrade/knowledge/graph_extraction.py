"""知识图谱 LLM 抽取 — 复盘自由文本 → ``provenance='llm'`` 候选边。

与 :mod:`doyoutrade.knowledge.graph` 的确定性投影互补：确定性层覆盖硬数据
（角色卡 / 交割单 / 信号），本模块用 LLM 从复盘日记 markdown 中抽取
**观点性事实**（题材归属、龙头判断、战法使用、个股联动），落进同一套
``kg_nodes`` / ``kg_edges``，以 ``provenance='llm'`` + ``confidence`` 与硬
事实区分。抽取 prompt 的规则借鉴 Graphiti（``prompts/extract_edges``）：
受控实体/关系词表、事实必须涉及两个不同实体、专有名词不泛化、
REFERENCE_DATE 解析相对时间、说不清的时间留 null。

管线（全程软失败——模型/解析/落库错误返回结构化 error dict，从不 raise
到 cron 链路；仿 :mod:`doyoutrade.portfolio_import.image_extractor`）：

1. :func:`extract_graph_candidates` — 一次模型调用 + 严格 JSON 解析
   （strict ``json.loads`` → ``{...}`` 子串重试，不做 json-repair 式修补）
   + 逐条词表/类型校验，坏行进 ``warnings``（带 reason 与原始值）。
2. :func:`resolve_and_apply_candidates` — symbol 实体解析（代码直通 →
   图内已有节点精确匹配 → instrument_catalog 检索），未解析实体的边
   **跳过并计入 warnings**（不猜代码）；映射成
   ``KnowledgeGraphEdgeSpec(provenance='llm')`` 幂等落库。
3. :func:`extract_and_apply` — 编排 1+2 并写 ``kg_source_state`` 水位
   （同一 source_ref + 相同文本 hash 时直接跳过，日重跑零模型成本）。

dedupe 语义：``dedupe_key = llm|<relation>|<src>|<dst>``——同一事实跨日
重复抽取只保留一条 active 边；fact 措辞 / confidence / 时间窗变化时旧边
置 ``expired_at``、插新边（bi-temporal 历史保留），由
``apply_projection`` 统一实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any

from doyoutrade.knowledge.graph import NODE_CYCLE, NODE_SYMBOL
from doyoutrade.models.base import ModelRequest
from doyoutrade.persistence.repositories import (
    KnowledgeGraphEdgeSpec,
    KnowledgeGraphNodeSpec,
)

logger = logging.getLogger(__name__)

NODE_THEME = "theme"
NODE_PLAYBOOK = "playbook"

#: 受控关系词表：relation → (允许的 src 节点类型, 允许的 dst 节点类型)。
#: LLM 输出超出词表一律进 warnings —— 词表扩展在这里集中登记。
EXTRACTION_RELATIONS: dict[str, tuple[str, str]] = {
    "belongs_to_theme": (NODE_SYMBOL, NODE_THEME),
    "leads_theme": (NODE_SYMBOL, NODE_THEME),
    "uses_playbook": (NODE_SYMBOL, NODE_PLAYBOOK),
    "linked_with": (NODE_SYMBOL, NODE_SYMBOL),
    "observed_in": (NODE_THEME, NODE_CYCLE),
}

_EXTRACTION_NODE_TYPES = frozenset(
    {NODE_SYMBOL, NODE_THEME, NODE_PLAYBOOK, NODE_CYCLE}
)

#: 6 位代码（可带交易所后缀）——直接视为 canonical symbol，不再查目录。
_SYMBOL_CODE_RE = re.compile(r"^\d{6}(\.[A-Za-z]+)?$")
_CYCLE_RE = re.compile(r"^\d{4}-\d{2}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SYSTEM_PROMPT_TEMPLATE = """你是交易知识图谱的事实抽取器。从用户提供的复盘日记 markdown 中抽取实体之间的关系事实。

实体类型（只允许这四种）：
- symbol：具体股票。值优先用 6 位代码（如 300059 或 300059.SZ）；文中只有名称时用准确的股票名称原文，不得截断或改写。
- theme：题材 / 板块（如 券商、AI算力、低空经济）。
- playbook：战法 / 打法名（如 首板低吸、龙头首阴）。
- cycle：情绪周期月，格式 YYYY-MM。

关系（只允许这五种，src 类型 → dst 类型固定）：
- belongs_to_theme：symbol → theme，该股属于某题材。
- leads_theme：symbol → theme，该股是该题材的龙头 / 核心。
- uses_playbook：symbol → playbook，对该股使用了某战法。
- linked_with：symbol → symbol，两股联动 / 映射关系。
- observed_in：theme → cycle，题材活跃于某个周期月。

规则：
1. 只抽取文中明确表述的事实，不推断、不虚构；一条事实必须涉及两个不同实体。
2. 专有名词保持原样，不泛化（"东方财富"不得写成"券商股"）。
3. REFERENCE_DATE = {reference_date}。相对时间（"今天"、"上周"）据此换算成 ISO 日期填入 valid_from / valid_to；文中说不清的时间一律留 null，不要编造。
4. confidence ∈ [0,1]：文中明确断言 ≥0.8；带犹豫 / 猜测语气 0.5–0.7；含糊提及 <0.5。
5. fact 用一句完整的中文陈述句复述该事实（包含主体名称与时间语境）。
6. 没有可抽取的事实时输出 {{"edges": []}}。

只输出一个 JSON 对象（不要 markdown 代码围栏、不要任何解释文字）：
{{"edges": [{{"src_type": "symbol", "src": "300059", "relation": "leads_theme", "dst_type": "theme", "dst": "券商", "fact": "……", "confidence": 0.8, "valid_from": "2026-03-10", "valid_to": null}}]}}"""


def build_extraction_system_prompt(reference_date: str) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(reference_date=reference_date)


def _parse_extraction_json(text: str) -> dict[str, Any] | None:
    """Parse model output into the ``{"edges": [...]}`` object; ``None`` if not.

    Strict ``json.loads`` first; on failure retry on the first-``{`` →
    last-``}`` substring (models occasionally wrap in prose / a fence). No
    json-repair mutation — still-broken output surfaces as
    ``extract_parse_failed``.
    """
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("edges"), list):
        return None
    return parsed


def _parse_iso_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value.strip()):
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _validate_candidate(obj: Any, index: int) -> tuple[dict[str, Any] | None, dict | None]:
    """One raw edge object → (candidate, None) or (None, warning)."""

    def _warn(reason: str) -> tuple[None, dict]:
        return None, {"reason": reason, "index": index, "raw": obj}

    if not isinstance(obj, dict):
        return _warn("candidate_not_object")
    relation = obj.get("relation")
    if relation not in EXTRACTION_RELATIONS:
        return _warn("unknown_relation")
    want_src, want_dst = EXTRACTION_RELATIONS[relation]
    src_type, dst_type = obj.get("src_type"), obj.get("dst_type")
    if src_type != want_src or dst_type != want_dst:
        return _warn("relation_type_mismatch")
    src = str(obj.get("src") or "").strip()
    dst = str(obj.get("dst") or "").strip()
    fact = str(obj.get("fact") or "").strip()
    if not src or not dst or not fact:
        return _warn("missing_src_dst_or_fact")
    if src_type == dst_type and src == dst:
        return _warn("self_edge")
    if dst_type == NODE_CYCLE and not _CYCLE_RE.match(dst):
        return _warn("bad_cycle_format")
    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return _warn("bad_confidence")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        return _warn("bad_confidence")
    return (
        {
            "relation": relation,
            "src_type": src_type,
            "src": src,
            "dst_type": dst_type,
            "dst": dst,
            "fact": fact,
            "confidence": confidence,
            "valid_from": _parse_iso_date(obj.get("valid_from")),
            "valid_to": _parse_iso_date(obj.get("valid_to")),
        },
        None,
    )


async def extract_graph_candidates(
    adapter: Any,
    text: str,
    *,
    reference_date: str,
) -> dict[str, Any]:
    """One model call → validated candidates. Never raises for expected input.

    Returns ``{"status": "ok", "candidates": [...], "warnings": [...]}`` or
    ``{"status": "error", "error_code": "model_error"|"extract_parse_failed",
    "message": ...}``.
    """
    body = (text or "").strip()
    if not body:
        return {"status": "ok", "candidates": [], "warnings": [],
                "note": "empty_text_nothing_to_extract"}
    request = ModelRequest(
        system_prompt=build_extraction_system_prompt(reference_date),
        user_prompt=body,
    )
    try:
        response = await asyncio.to_thread(adapter.generate, request)
    except Exception as exc:
        logger.warning(
            "kg extraction model call failed (%s): %s", type(exc).__name__, exc
        )
        return {
            "status": "error",
            "error_code": "model_error",
            "message": f"{type(exc).__name__}: {exc}",
        }
    raw_text = getattr(response, "text", None) or ""
    parsed = _parse_extraction_json(raw_text)
    if parsed is None:
        logger.warning(
            "kg extraction unparseable model output (first 200 chars): %r",
            raw_text[:200],
        )
        return {
            "status": "error",
            "error_code": "extract_parse_failed",
            "message": f"model output is not the expected JSON object: {raw_text[:500]!r}",
        }
    candidates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for index, item in enumerate(parsed["edges"]):
        candidate, warning = _validate_candidate(item, index)
        if warning is not None:
            warnings.append(warning)
            logger.info(
                "kg extraction skipping candidate reason=%s index=%d raw=%r",
                warning["reason"], index, item,
            )
            continue
        candidates.append(candidate)
    return {"status": "ok", "candidates": candidates, "warnings": warnings}


async def _resolve_symbol_value(
    value: str,
    *,
    repository: Any,
    instrument_catalog_repository: Any | None,
    cache: dict[str, tuple[str, str | None] | None],
) -> tuple[str, str | None] | None:
    """股票值 → ``(canonical_symbol, display_name)``；解析不出返回 ``None``。

    顺序：6 位代码直通 → 图内已有 symbol 节点按 display_name/name 精确
    匹配 → instrument_catalog 检索（零网络本地目录）。绝不猜代码。
    """
    if value in cache:
        return cache[value]
    resolved: tuple[str, str | None] | None = None
    if _SYMBOL_CODE_RE.match(value):
        resolved = (value, None)
    else:
        try:
            matches = await repository.find_nodes(value, limit=4)
        except Exception as exc:
            logger.warning(
                "kg extraction node lookup failed value=%r (%s): %s",
                value, type(exc).__name__, exc,
            )
            matches = []
        for node in matches:
            if node.node_type == NODE_SYMBOL and value in (node.display_name, node.name):
                resolved = (node.name, node.display_name)
                break
        if resolved is None and instrument_catalog_repository is not None:
            from doyoutrade.portfolio_import.image_extractor import _resolve_symbol

            symbol = await _resolve_symbol(value, instrument_catalog_repository)
            if symbol:
                resolved = (symbol, value)
    cache[value] = resolved
    return resolved


async def resolve_and_apply_candidates(
    repository: Any,
    candidates: list[dict[str, Any]],
    *,
    now: datetime,
    source_ref: str,
    instrument_catalog_repository: Any | None = None,
) -> dict[str, Any]:
    """Resolve entities, map to ``provenance='llm'`` specs, apply idempotently."""
    warnings: list[dict[str, Any]] = []
    nodes: dict[tuple[str, str], KnowledgeGraphNodeSpec] = {}
    edges_by_dedupe: dict[str, KnowledgeGraphEdgeSpec] = {}
    symbol_cache: dict[str, tuple[str, str | None] | None] = {}

    async def _node_key(node_type: str, value: str) -> tuple[str, str] | None:
        if node_type == NODE_SYMBOL:
            resolved = await _resolve_symbol_value(
                value,
                repository=repository,
                instrument_catalog_repository=instrument_catalog_repository,
                cache=symbol_cache,
            )
            if resolved is None:
                return None
            name, display = resolved
            key = (NODE_SYMBOL, name)
            prior = nodes.get(key)
            nodes[key] = KnowledgeGraphNodeSpec(
                node_type=NODE_SYMBOL,
                name=name,
                display_name=display or (prior.display_name if prior else None),
            )
            return key
        key = (node_type, value)
        nodes.setdefault(key, KnowledgeGraphNodeSpec(node_type=node_type, name=value))
        return key

    for candidate in candidates:
        src_key = await _node_key(candidate["src_type"], candidate["src"])
        dst_key = await _node_key(candidate["dst_type"], candidate["dst"])
        if src_key is None or dst_key is None:
            unresolved = candidate["src"] if src_key is None else candidate["dst"]
            warnings.append(
                {
                    "reason": "symbol_unresolved",
                    "value": unresolved,
                    "fact": candidate["fact"],
                    "hint": "股票名无法解析成 canonical symbol；先 stock lookup 或补 instrument catalog",
                }
            )
            logger.info(
                "kg extraction dropping edge reason=symbol_unresolved value=%r fact=%r",
                unresolved, candidate["fact"],
            )
            continue
        if src_key == dst_key:
            warnings.append(
                {"reason": "self_edge_after_resolution", "fact": candidate["fact"]}
            )
            continue
        dedupe_key = f"llm|{candidate['relation']}|{src_key[1]}|{dst_key[1]}"
        prior = edges_by_dedupe.get(dedupe_key)
        if prior is not None and (prior.confidence or 0) >= candidate["confidence"]:
            continue  # 批内重复：保留高置信版本
        edges_by_dedupe[dedupe_key] = KnowledgeGraphEdgeSpec(
            src=src_key,
            dst=dst_key,
            relation=candidate["relation"],
            fact=candidate["fact"],
            dedupe_key=dedupe_key,
            attrs={"asof": candidate["valid_from"].date().isoformat()
                   if candidate["valid_from"] else None},
            provenance="llm",
            confidence=candidate["confidence"],
            source_ref=source_ref,
            valid_at=candidate["valid_from"],
            invalid_at=candidate["valid_to"],
        )

    edges = list(edges_by_dedupe.values())
    apply_stats = await repository.apply_projection(
        sorted(nodes.values(), key=lambda n: (n.node_type, n.name)), edges, now=now
    )
    return {
        "apply": apply_stats,
        "edges_submitted": len(edges),
        "warnings": warnings,
    }


async def extract_and_apply(
    repository: Any,
    adapter: Any,
    text: str,
    *,
    reference_date: str,
    source_ref: str,
    now: datetime,
    instrument_catalog_repository: Any | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Full pipeline with a per-source content_hash watermark.

    同一 ``source_ref``（如 ``kb:journal/2026/2026-07-17.md``）文本未变时
    直接跳过（零模型成本；``force=True`` 绕过）。所有失败模式返回结构化
    ``status='error'``——调用方（daily_review 软失败步骤）转 debug event。
    """
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    if not force:
        try:
            state = await repository.get_source_state(source_ref)
        except Exception as exc:
            logger.warning(
                "kg extraction watermark read failed source=%r (%s): %s",
                source_ref, type(exc).__name__, exc,
            )
            state = None
        if state is not None and state.content_hash == digest:
            return {"status": "ok", "skipped": True, "source_ref": source_ref}

    extraction = await extract_graph_candidates(
        adapter, text, reference_date=reference_date
    )
    if extraction["status"] != "ok":
        return extraction
    candidates = extraction["candidates"]
    result: dict[str, Any] = {
        "status": "ok",
        "skipped": False,
        "source_ref": source_ref,
        "candidate_count": len(candidates),
        "warnings": list(extraction["warnings"]),
    }
    if candidates:
        try:
            applied = await resolve_and_apply_candidates(
                repository,
                candidates,
                now=now,
                source_ref=source_ref,
                instrument_catalog_repository=instrument_catalog_repository,
            )
        except Exception as exc:
            logger.warning(
                "kg extraction apply failed source=%r (%s): %s",
                source_ref, type(exc).__name__, exc,
            )
            return {
                "status": "error",
                "error_code": "kg_apply_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
        result["apply"] = applied["apply"]
        result["edges_submitted"] = applied["edges_submitted"]
        result["warnings"].extend(applied["warnings"])
    else:
        result["apply"] = None
        result["edges_submitted"] = 0

    try:
        await repository.set_source_state(
            source_ref, digest, now=now,
            stats={"candidate_count": len(candidates),
                   "edges_submitted": result["edges_submitted"]},
        )
    except Exception as exc:
        # 水位写失败只影响下次重复抽取的成本，不影响本次结果——降级为
        # warning 并显式暴露。
        logger.warning(
            "kg extraction watermark write failed source=%r (%s): %s",
            source_ref, type(exc).__name__, exc,
        )
        result["warnings"].append(
            {"reason": "watermark_write_failed", "source_ref": source_ref}
        )
    return result


__all__ = [
    "EXTRACTION_RELATIONS",
    "NODE_PLAYBOOK",
    "NODE_THEME",
    "build_extraction_system_prompt",
    "extract_and_apply",
    "extract_graph_candidates",
    "resolve_and_apply_candidates",
]
