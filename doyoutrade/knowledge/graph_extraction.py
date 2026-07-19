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
from dataclasses import dataclass
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

#: 受控关系词表：relation → (允许的 src 节点类型, 允许的 dst 节点类型)。这是
#: **系统默认**词表；自定义 schema（``custom.*``）的实体 / 关系类型由
#: :func:`build_extraction_vocabulary` 动态并入，无需改这里。超出最终词表的
#: LLM 输出一律进 warnings。
EXTRACTION_RELATIONS: dict[str, tuple[str, str]] = {
    "belongs_to_theme": (NODE_SYMBOL, NODE_THEME),
    "leads_theme": (NODE_SYMBOL, NODE_THEME),
    "uses_playbook": (NODE_SYMBOL, NODE_PLAYBOOK),
    "linked_with": (NODE_SYMBOL, NODE_SYMBOL),
    "observed_in": (NODE_THEME, NODE_CYCLE),
}

#: relation → prompt 里「src → dst，<说明>」的中文说明后缀。
EXTRACTION_RELATION_DESCRIPTIONS: dict[str, str] = {
    "belongs_to_theme": "该股属于某题材。",
    "leads_theme": "该股是该题材的龙头 / 核心。",
    "uses_playbook": "对该股使用了某战法。",
    "linked_with": "两股联动 / 映射关系。",
    "observed_in": "题材活跃于某个周期月。",
}

#: 系统节点类型 → prompt 里的中文说明（插入顺序即 prompt 展示顺序）。
EXTRACTION_NODE_DESCRIPTIONS: dict[str, str] = {
    NODE_SYMBOL: (
        "具体股票。值优先用 6 位代码（如 300059 或 300059.SZ）；文中只有名称时"
        "用准确的股票名称原文，不得截断或改写。"
    ),
    NODE_THEME: "题材 / 板块（如 券商、AI算力、低空经济）。",
    NODE_PLAYBOOK: "战法 / 打法名（如 首板低吸、龙头首阴）。",
    NODE_CYCLE: "情绪周期月，格式 YYYY-MM。",
}

_EXTRACTION_NODE_TYPES = frozenset(EXTRACTION_NODE_DESCRIPTIONS)


@dataclass(frozen=True)
class ExtractionVocabulary:
    """一次抽取可用的受控词表（系统默认 + 已批准的 ``custom.*`` 扩展）。

    ``relations`` 是 relation → (src_type, dst_type) 约束表，``node_types`` 是
    允许的实体类型集合；``node_lines`` / ``relation_lines`` 是渲染进 system
    prompt 的中文 bullet 行。全部字段确定性排序，保证同 schema 同 prompt。
    """

    relations: dict[str, tuple[str, str]]
    node_types: frozenset[str]
    node_lines: tuple[str, ...]
    relation_lines: tuple[str, ...]


def build_extraction_vocabulary(
    schema: dict[str, Any] | None,
) -> ExtractionVocabulary:
    """系统默认词表 + merged schema 里已激活的 ``custom.*`` 实体 / 关系。

    ``schema`` 为 :meth:`KnowledgeGraphCommandService.get_schema` 的返回结构
    （``entity_types`` / ``relation_types``，每项含 ``key`` / ``label`` /
    ``namespace`` / ``status`` / ``source_type`` / ``target_type``）。仅并入
    ``namespace=='custom'`` 且 ``status=='active'`` 的定义；自定义关系的两个
    端点类型都必须是可抽取节点类型（系统四类 + 已并入的自定义实体类型），
    否则跳过（不静默扩张出无法解析的边）。``schema=None`` 退回系统默认。
    """

    node_descriptions: dict[str, str] = dict(EXTRACTION_NODE_DESCRIPTIONS)
    relations: dict[str, tuple[str, str]] = dict(EXTRACTION_RELATIONS)
    relation_descriptions: dict[str, str] = dict(EXTRACTION_RELATION_DESCRIPTIONS)

    custom_node_order: list[str] = []
    custom_relation_order: list[str] = []
    if schema is not None:
        for item in sorted(
            schema.get("entity_types", []),
            key=lambda i: str(i.get("key", "")),
        ):
            key = str(item.get("key", ""))
            if (
                item.get("namespace") == "custom"
                and item.get("status") == "active"
                and key
                and key not in node_descriptions
            ):
                label = item.get("label") or key
                node_descriptions[key] = f"{label}（自定义类型）。"
                custom_node_order.append(key)
        for item in sorted(
            schema.get("relation_types", []),
            key=lambda i: str(i.get("key", "")),
        ):
            key = str(item.get("key", ""))
            src = str(item.get("source_type", ""))
            dst = str(item.get("target_type", ""))
            if (
                item.get("namespace") == "custom"
                and item.get("status") == "active"
                and key
                and key not in relations
                and src in node_descriptions
                and dst in node_descriptions
            ):
                relations[key] = (src, dst)
                label = item.get("label") or key
                relation_descriptions[key] = f"{label}（自定义关系）。"
                custom_relation_order.append(key)

    node_order = list(EXTRACTION_NODE_DESCRIPTIONS) + custom_node_order
    relation_order = list(EXTRACTION_RELATIONS) + custom_relation_order
    node_lines = tuple(
        f"- {node_type}：{node_descriptions[node_type]}"
        for node_type in node_order
    )
    relation_lines = tuple(
        f"- {relation}：{relations[relation][0]} → {relations[relation][1]}，"
        f"{relation_descriptions.get(relation, '')}"
        for relation in relation_order
    )
    return ExtractionVocabulary(
        relations=relations,
        node_types=frozenset(node_descriptions),
        node_lines=node_lines,
        relation_lines=relation_lines,
    )


#: 6 位代码（可带交易所后缀）——直接视为 canonical symbol，不再查目录。
_SYMBOL_CODE_RE = re.compile(r"^\d{6}(\.[A-Za-z]+)?$")
_CYCLE_RE = re.compile(r"^\d{4}-\d{2}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SYSTEM_PROMPT_TEMPLATE = """你是交易知识图谱的事实抽取器。从用户提供的复盘日记 markdown 中抽取实体之间的关系事实。

实体类型（只允许下列几种）：
{node_section}

关系（只允许下列几种，src 类型 → dst 类型固定）：
{relation_section}

规则：
1. 只抽取文中明确表述的事实，不推断、不虚构；一条事实必须涉及两个不同实体。
2. 专有名词保持原样，不泛化（"东方财富"不得写成"券商股"）。
3. REFERENCE_DATE = {reference_date}。相对时间（"今天"、"上周"）据此换算成 ISO 日期填入 valid_from / valid_to；文中说不清的时间一律留 null，不要编造。
4. confidence ∈ [0,1]：文中明确断言 ≥0.8；带犹豫 / 猜测语气 0.5–0.7；含糊提及 <0.5。
5. fact 用一句完整的中文陈述句复述该事实（包含主体名称与时间语境）。
6. 没有可抽取的事实时输出 {{"edges": []}}。

只输出一个 JSON 对象（不要 markdown 代码围栏、不要任何解释文字）：
{{"edges": [{{"src_type": "symbol", "src": "300059", "relation": "leads_theme", "dst_type": "theme", "dst": "券商", "fact": "……", "confidence": 0.8, "valid_from": "2026-03-10", "valid_to": null}}]}}"""


def build_extraction_system_prompt(
    reference_date: str,
    vocabulary: ExtractionVocabulary | None = None,
) -> str:
    vocab = vocabulary or build_extraction_vocabulary(None)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        reference_date=reference_date,
        node_section="\n".join(vocab.node_lines),
        relation_section="\n".join(vocab.relation_lines),
    )


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


def _validate_candidate(
    obj: Any,
    index: int,
    vocabulary: ExtractionVocabulary | None = None,
) -> tuple[dict[str, Any] | None, dict | None]:
    """One raw edge object → (candidate, None) or (None, warning).

    ``vocabulary`` 默认系统词表；传入 schema 构造的词表可让 ``custom.*`` 关系
    通过校验。
    """

    vocab = vocabulary or build_extraction_vocabulary(None)

    def _warn(reason: str) -> tuple[None, dict]:
        return None, {"reason": reason, "index": index, "raw": obj}

    if not isinstance(obj, dict):
        return _warn("candidate_not_object")
    relation = obj.get("relation")
    if relation not in vocab.relations:
        return _warn("unknown_relation")
    want_src, want_dst = vocab.relations[relation]
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
    vocabulary: ExtractionVocabulary | None = None,
) -> dict[str, Any]:
    """One model call → validated candidates. Never raises for expected input.

    ``vocabulary`` 控制允许的实体 / 关系词表（默认系统词表；传入由
    :func:`build_extraction_vocabulary` 从 schema 构造的词表即可让 ``custom.*``
    类型参与抽取）。Returns ``{"status": "ok", "candidates": [...],
    "warnings": [...]}`` or ``{"status": "error", "error_code":
    "model_error"|"extract_parse_failed", "message": ...}``.
    """
    vocab = vocabulary or build_extraction_vocabulary(None)
    body = (text or "").strip()
    if not body:
        return {"status": "ok", "candidates": [], "warnings": [],
                "note": "empty_text_nothing_to_extract"}
    request = ModelRequest(
        system_prompt=build_extraction_system_prompt(reference_date, vocab),
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
        candidate, warning = _validate_candidate(item, index, vocab)
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
    source_digest: str | None = None,
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
            source_key=source_ref,
            source_ref=source_ref,
            valid_at=candidate["valid_from"],
            invalid_at=candidate["valid_to"],
        )

    edges = list(edges_by_dedupe.values())
    apply_stats = await repository.apply_projection(
        sorted(nodes.values(), key=lambda n: (n.node_type, n.name)),
        edges,
        now=now,
        reconcile_source_keys={source_ref} if source_digest is not None else None,
        source_hashes=(
            {source_ref: source_digest} if source_digest is not None else None
        ),
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
    vocabulary: ExtractionVocabulary | None = None,
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

    # 未显式传词表时，尝试从图谱 schema 载入 custom.* 扩展；软路径——载入
    # 失败退回系统默认词表，绝不 raise 到 cron 链路。
    vocab = vocabulary
    if vocab is None:
        vocab = build_extraction_vocabulary(None)
        session_factory = getattr(repository, "session_factory", None)
        if session_factory is not None:
            try:
                from doyoutrade.knowledge.editing import (
                    KnowledgeGraphCommandService,
                )

                schema = await KnowledgeGraphCommandService(
                    session_factory
                ).get_schema()
                vocab = build_extraction_vocabulary(schema)
            except Exception as exc:
                logger.warning(
                    "kg extraction schema load failed source=%r (%s): %s — "
                    "falling back to system vocabulary",
                    source_ref, type(exc).__name__, exc,
                )
                vocab = build_extraction_vocabulary(None)

    extraction = await extract_graph_candidates(
        adapter, text, reference_date=reference_date, vocabulary=vocab
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
    try:
        applied = await resolve_and_apply_candidates(
            repository,
            candidates,
            now=now,
            source_ref=source_ref,
            source_digest=digest,
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
    return result


__all__ = [
    "EXTRACTION_NODE_DESCRIPTIONS",
    "EXTRACTION_RELATION_DESCRIPTIONS",
    "EXTRACTION_RELATIONS",
    "ExtractionVocabulary",
    "NODE_PLAYBOOK",
    "NODE_THEME",
    "build_extraction_system_prompt",
    "build_extraction_vocabulary",
    "extract_and_apply",
    "extract_graph_candidates",
    "resolve_and_apply_candidates",
]
