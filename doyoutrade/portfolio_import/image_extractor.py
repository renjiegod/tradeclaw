"""Vision extraction of portfolio positions from a brokerage screenshot.

Feature 6 (docs/dsa-feature-migration.md): the user pastes a 持仓截图 and the
model extracts a structured position list, whose names are then normalised to
canonical symbols via ``search_instrument_universe`` (local catalog first,
akshare fallback). Ported from the DSA ``image_stock_extractor`` idea but
written fresh against doyoutrade's :class:`~doyoutrade.models.base.ModelAdapter`
multimodal path (no litellm, no ``json_repair`` dependency).

Failure modes are structured (never raised for expected input problems),
each with a stable ``error_code``:

- ``image_empty``          — zero-byte input.
- ``image_too_large``      — exceeds :data:`~doyoutrade.models.base.MAX_IMAGE_BYTES`.
- ``image_mime_mismatch``  — declared MIME does not match the magic bytes,
                             or the magic bytes are not a supported format.
- ``model_error``          — the adapter call raised (type + message surfaced).
- ``extract_parse_failed`` — model output is not parseable JSON (first 500
                             chars of the raw text surfaced for debugging).
- ``extract_empty``        — model returned a valid but empty position list.

Unresolvable names are NEVER silently dropped: they stay in ``positions``
flagged ``symbol_unresolved: true`` and are echoed in ``unresolved``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from doyoutrade.data.instrument_universe.service import search_instrument_universe
from doyoutrade.models.base import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    ImagePart,
    ModelAdapter,
    ModelRequest,
)

logger = logging.getLogger(__name__)

#: Cap of raw model text echoed back on parse failure (debuggability without
#: blowing up the tool result / debug event payload).
_PARSE_FAIL_ECHO_CHARS = 500

SYSTEM_PROMPT = "你是一个精确的证券持仓截图识别助手，只输出 JSON，不输出任何解释文字。"

EXTRACT_PROMPT = """\
请从这张证券账户持仓截图中提取所有持仓股票，输出一个 JSON 数组，不要输出任何其它文字（不要 markdown 代码块标记、不要解释）。

数组中每个元素是一个对象，字段如下：
- "name"（必填）：股票名称，例如 "贵州茅台"。
- "symbol"（可选）：股票代码，仅在截图里能看到时填写，例如 "600519"；看不到就省略该字段，不要编造。
- "quantity"（必填）：持仓数量（股数），数字。
- "cost_price"（可选）：成本价 / 摊薄成本，数字；看不到就省略。
- "current_price"（可选）：现价 / 最新价，数字；看不到就省略。

要求：
1. 只提取真实出现在截图中的持仓行；表头、汇总行、现金/可用资金不算持仓。
2. 数字去掉千分位逗号；无法看清的数字省略对应可选字段，quantity 无法看清时跳过整行。
3. 如果截图中没有任何持仓，输出空数组 []。

示例输出：
[{"name": "贵州茅台", "symbol": "600519", "quantity": 100, "cost_price": 1650.5, "current_price": 1701.0}]
"""

#: Magic-byte signatures → MIME type. WEBP is RIFF????WEBP (bytes 0-3 + 8-11).
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_mime(data: bytes) -> str | None:
    """Detect the image MIME type from magic bytes; ``None`` when unrecognised."""
    for magic, mime in _MAGIC_SIGNATURES:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _error(error_code: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "error",
        "error_code": error_code,
        "message": message,
    }
    out.update(extra)
    return out


def _parse_positions_json(text: str) -> list[Any] | None:
    """Parse the model output into a list; ``None`` when unparseable.

    Strict ``json.loads`` first; on failure, retry on the substring from the
    first ``[`` to the last ``]`` (models occasionally wrap the array in prose
    or a markdown fence). No ``json_repair``-style mutation — an output that
    still fails is surfaced as ``extract_parse_failed``, not "fixed".
    """
    stripped = text.strip()
    for candidate in (stripped,):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return parsed
        if parsed is not None:
            return None  # valid JSON but not a list — treated as parse failure by caller
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(parsed, list):
            return parsed
    return None


async def _resolve_symbol(
    name: str,
    instrument_catalog_repository: Any | None,
) -> str | None:
    """Resolve *name* → canonical symbol via the instrument universe, or ``None``.

    Prefers the zero-network local catalog when a repository is wired;
    otherwise falls back to akshare. Lookup errors are logged (warning, with
    exception type) and surface as "unresolved" — never raised to the caller.
    """
    source = "local_catalog" if instrument_catalog_repository is not None else "akshare_a"
    try:
        result = await search_instrument_universe(
            source=source,
            q=name,
            limit=1,
            instrument_catalog_repository=instrument_catalog_repository,
        )
    except Exception as exc:
        logger.warning(
            "portfolio_import: symbol lookup failed name=%r source=%s (%s): %s",
            name, source, type(exc).__name__, exc,
        )
        return None
    items = result.get("items") or []
    if not items:
        return None
    symbol = items[0].get("symbol")
    return str(symbol) if symbol else None


async def extract_positions_from_image(
    image_bytes: bytes,
    mime_type: str,
    *,
    adapter: ModelAdapter,
    instrument_catalog_repository: Any | None = None,
) -> dict[str, Any]:
    """Extract portfolio positions from a screenshot via a multimodal model.

    Returns ``{"status": "ok", "positions": [...], "unresolved": [...]}`` on
    success, or ``{"status": "error", "error_code": ..., "message": ...}`` for
    every expected failure mode (see module docstring). Each position carries
    ``name`` / ``quantity`` (+ optional ``symbol`` / ``cost_price`` /
    ``current_price``); positions whose name could not be resolved to a
    canonical symbol keep the raw name and are flagged
    ``symbol_unresolved: true`` (also listed in ``unresolved``).
    """
    if not image_bytes:
        logger.warning("portfolio_import: empty image payload")
        return _error("image_empty", "image payload is empty (0 bytes)")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        logger.warning(
            "portfolio_import: image too large size=%d limit=%d",
            len(image_bytes), MAX_IMAGE_BYTES,
        )
        return _error(
            "image_too_large",
            f"image is {len(image_bytes)} bytes, exceeds the {MAX_IMAGE_BYTES} byte limit",
            size_bytes=len(image_bytes),
            limit_bytes=MAX_IMAGE_BYTES,
        )

    sniffed = sniff_image_mime(image_bytes)
    if sniffed is None:
        logger.warning(
            "portfolio_import: unrecognised image magic bytes (declared mime=%s, head=%r)",
            mime_type, image_bytes[:12],
        )
        return _error(
            "image_mime_mismatch",
            "image magic bytes are not a supported format "
            f"(png/jpeg/webp/gif); declared mime_type={mime_type!r}",
            declared_mime=mime_type,
        )
    if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
        return _error(
            "image_mime_mismatch",
            f"declared mime_type={mime_type!r} is not supported; "
            f"file content looks like {sniffed}",
            declared_mime=mime_type,
            sniffed_mime=sniffed,
        )
    if sniffed != mime_type:
        logger.warning(
            "portfolio_import: mime mismatch declared=%s sniffed=%s", mime_type, sniffed
        )
        return _error(
            "image_mime_mismatch",
            f"declared mime_type={mime_type!r} does not match file content ({sniffed})",
            declared_mime=mime_type,
            sniffed_mime=sniffed,
        )

    request = ModelRequest(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=EXTRACT_PROMPT,
        image_parts=(ImagePart(data=image_bytes, mime_type=mime_type),),
    )
    try:
        # ModelAdapter.generate is synchronous — run it off the event loop.
        response = await asyncio.to_thread(adapter.generate, request)
    except Exception as exc:
        logger.warning(
            "portfolio_import: model call failed (%s): %s", type(exc).__name__, exc
        )
        return _error(
            "model_error",
            f"model call failed: {exc}",
            error_type=type(exc).__name__,
        )

    raw_text = response.text or ""
    parsed = _parse_positions_json(raw_text)
    if parsed is None:
        logger.warning(
            "portfolio_import: model output not parseable as a JSON array "
            "(len=%d, head=%r)",
            len(raw_text), raw_text[:120],
        )
        return _error(
            "extract_parse_failed",
            "model output is not a parseable JSON array of positions",
            raw_text=raw_text[:_PARSE_FAIL_ECHO_CHARS],
        )
    if not parsed:
        logger.warning("portfolio_import: model returned an empty position list")
        return _error(
            "extract_empty",
            "model found no positions in the image (empty array)",
        )

    positions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            entry = {
                "index": index,
                "reason": "item_not_object",
                "value": repr(item)[:120],
                "hint": "model emitted a non-object array element; item kept in unresolved",
            }
            unresolved.append(entry)
            logger.warning(
                "portfolio_import: position item %d is %s, not an object; kept in unresolved",
                index, type(item).__name__,
            )
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            entry = {
                "index": index,
                "reason": "missing_name",
                "value": json.dumps(item, ensure_ascii=False)[:200],
                "hint": "position row has no name; kept in unresolved",
            }
            unresolved.append(entry)
            logger.warning(
                "portfolio_import: position item %d has no name; kept in unresolved", index
            )
            continue

        position: dict[str, Any] = {"name": name}
        for field in ("quantity", "cost_price", "current_price"):
            if item.get(field) is not None:
                position[field] = item[field]

        raw_symbol = str(item.get("symbol") or "").strip()
        if raw_symbol:
            position["symbol"] = raw_symbol
        else:
            symbol = await _resolve_symbol(name, instrument_catalog_repository)
            if symbol:
                position["symbol"] = symbol
            else:
                position["symbol_unresolved"] = True
                unresolved.append(
                    {
                        "index": index,
                        "reason": "symbol_unresolved",
                        "name": name,
                        "hint": "name not found in instrument universe; verify with "
                        "`doyoutrade-cli stock lookup` before using this position",
                    }
                )
        positions.append(position)

    if not positions:
        # Every parsed item was malformed — surface that as a distinct outcome
        # rather than an "ok" result with an empty list.
        return _error(
            "extract_empty",
            "model output contained no usable position rows",
            unresolved=unresolved,
        )

    return {"status": "ok", "positions": positions, "unresolved": unresolved}


__all__ = [
    "EXTRACT_PROMPT",
    "SYSTEM_PROMPT",
    "extract_positions_from_image",
    "sniff_image_mime",
]
